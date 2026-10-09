"""Geometry, provenance, evidence immutability and diagnostic-only boundaries."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import entry_feasibility_audit as audit
from analysis_terminal import entry_band
from analysis_terminal.setups import setup_identity


def item(side="LONG", stop=90, target=120, roll=99, stage="RR_WAIT", confirmed=True):
    row = dict(ticker="SYNTHETICUSDC", direction=side, breakout_time_ms=900000,
               breakout_level=roll, latest_4h_time_ms=900000, latest_15m_time_ms=900000,
               stage=stage, confirmed=confirmed, entry_band=entry_band.diagnose(side, stop, target, roll, 2))
    row["setup_id"] = setup_identity(row)
    return row


def observation(*rows):
    return entry_band.observation(rows, observed_ms=1800000)


def snapshot(root, observed):
    path = root / "readiness_review.json"
    path.write_text(json.dumps(dict(review=dict(entry_bands=observed))))
    (root / "manifest.json").write_text(json.dumps([
        dict(endpoint="readiness_review", status=200, fetched_ms=1800001,
             sha256=hashlib.sha256(path.read_bytes()).hexdigest())]))


class EntryFeasibilityAuditTests(unittest.TestCase):
    def test_net_band_matches_direct_fee_and_slippage_inequalities_both_sides(self):
        for side, stop, target in (("LONG", 90, 120), ("SHORT", 110, 80)):
            for scale in (.001, 1, 100000):
                for roll in (85, 95, 100, 105, 115):
                    band = audit.diagnose(entry_band.diagnose(side, stop*scale, target*scale, roll*scale, 2))
                    for reference in (80, 90, 91, 95, 99, 99.9, 100, 100.1, 101, 105, 109, 110, 120):
                        nominal = reference * scale
                        direction = 1 if side == "LONG" else -1
                        entry = nominal*(1+direction*.0002)
                        exit_stop = stop*scale*(1-direction*.0002)
                        exit_tp = target*scale*(1-direction*.0002)
                        risk = direction*(entry-exit_stop)+.0005*(entry+exit_stop)
                        reward = direction*(exit_tp-entry)-.0005*(entry+exit_tp)
                        structural = min(stop, target)*scale < nominal < max(stop, target)*scale
                        confirmation = reference > roll if side == "LONG" else reference < roll
                        expected = structural and confirmation and risk > 0 and reward/risk >= 2
                        combined = band["compatible_band"]
                        included = bool(combined and
                            (nominal > combined["lower"] if side == "LONG" else nominal >= combined["lower"]) and
                            (nominal <= combined["upper"] if side == "LONG" else nominal < combined["upper"]))
                        self.assertEqual(included, expected, (side, scale, roll, reference))

    def test_costs_remove_narrow_gross_compatible_band_in_both_directions(self):
        for side, stop, target, roll in (("LONG", 90, 120, 99.93), ("SHORT", 110, 80, 100.07)):
            source = entry_band.diagnose(side, stop, target, roll, 2)
            self.assertEqual(source["status"], "COMPATIBLE")
            self.assertEqual(audit.diagnose(source)["status"], "NET_NO_OVERLAP")
            report = audit.summarize(observation(item(side, stop, target, roll)))
            self.assertEqual(report["cost_only_lost_gross_compatible"], 1)

    def test_strict_roll_at_net_boundary_is_excluded_and_net_rr_boundary_is_inclusive(self):
        for side, stop, target in (("LONG", 90, 120), ("SHORT", 110, 80)):
            reference = audit.frozen.pullback_trigger(stop, target, side)
            self.assertAlmostEqual(audit.frozen.cost_levels(reference, stop, target, side)["net_rr"], 2)
            self.assertEqual(audit.diagnose(entry_band.diagnose(side, stop, target, reference, 2))["status"],
                             "NET_NO_OVERLAP")
            roll = reference - 1 if side == "LONG" else reference + 1
            band = audit.diagnose(entry_band.diagnose(side, stop, target, roll, 2))["compatible_band"]
            self.assertEqual(band["upper"] if side == "LONG" else band["lower"], reference)
            self.assertTrue(band["upper_inclusive"] if side == "LONG" else band["lower_inclusive"])

    def test_costs_cannot_create_compatible_band_from_gross_conflict(self):
        for side, stop, target in (("LONG", 90, 120), ("SHORT", 110, 80)):
            report = audit.summarize(observation(item(side, stop, target, 100)))
            self.assertEqual(report["gross_counts"]["NO_OVERLAP"], 1)
            self.assertEqual(report["net_counts"]["NET_NO_OVERLAP"], 1)
            self.assertEqual(report["cost_only_lost_gross_compatible"], 0)

    def test_narrow_structure_has_no_nominal_net_two_r_price(self):
        for side, stop, target in (("LONG", 100, 100.01), ("SHORT", 100, 99.99)):
            self.assertEqual(audit.diagnose(entry_band.diagnose(side, stop, target, 100, 2))["status"],
                             "NET_NO_VALID_PRICE")

    def test_missing_and_inverted_structure_stay_not_evaluable(self):
        for row in (item(stop=None), item(stop=130, target=120)):
            result = audit.summarize(observation(row))
            self.assertEqual(result["net_counts"]["NOT_EVALUABLE"], 1)
            self.assertEqual(result["net_counts"]["NET_NO_OVERLAP"], 0)

    def test_stage_counts_use_only_one_deduplicated_observation_and_preserve_input(self):
        rows = (item(), item(), item(roll=101, stage="CONFIRMATION_WAIT", confirmed=False))
        saved = observation(*rows)
        before = deepcopy(saved)
        result = audit.summarize(saved)
        self.assertEqual(result["identified_setups"], 2)
        self.assertEqual(sum(sum(x.values()) for x in result["by_stage"].values()), 2)
        self.assertEqual(sum(sum(x.values()) for x in result["confirmed_by_stage"].values()), 1)
        self.assertEqual(saved, before)

    def test_malformed_duplicate_counts_geometry_or_identity_fail_closed(self):
        good = observation(item())
        for kind in ("duplicate", "counts", "geometry", "identity"):
            changed = deepcopy(good)
            if kind == "duplicate":
                changed["items"].append(deepcopy(changed["items"][0]))
                changed["evaluated"] += 1; changed["counts"]["COMPATIBLE"] += 1
            elif kind == "counts": changed["counts"]["COMPATIBLE"] += 1
            elif kind == "geometry": changed["items"][0]["entry_band"]["rr_boundary"] += 1
            else: changed["items"][0]["setup_id"] = "tampered"
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                audit.summarize(changed)

    def test_unknown_stage_confirmation_or_time_is_blocked(self):
        good = observation(item())
        for name, value in (("stage", "UNKNOWN"), ("confirmed", None), ("confirmed", 1),
                            ("latest_15m_time_ms", 1800001), ("latest_4h_time_ms", None)):
            changed = deepcopy(good); changed["items"][0][name] = value
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                audit.summarize(changed)

    def test_other_rr_or_observation_basis_is_not_silently_substituted(self):
        changed = observation(item())
        changed["items"][0]["entry_band"] = entry_band.diagnose("LONG", 90, 120, 99, 1)
        with self.assertRaises(ValueError): audit.summarize(changed)
        changed = observation(item()); changed["basis"] = "independent_trades"
        with self.assertRaises(ValueError): audit.summarize(changed)

    def test_valid_empty_observation_is_distinct_from_missing(self):
        self.assertEqual(audit.summarize(observation())["identified_setups"], 0)
        with self.assertRaises(ValueError): audit.summarize(None)

    def test_source_hash_duplicate_fetch_clock_and_status_fail_closed(self):
        for kind in ("hash", "duplicate", "clock", "status"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); snapshot(root, observation(item()))
                manifest = json.loads((root / "manifest.json").read_text())
                if kind == "hash": (root / "readiness_review.json").write_text("{}")
                elif kind == "duplicate": manifest.append(deepcopy(manifest[0]))
                elif kind == "clock": manifest[0]["fetched_ms"] = 1799999
                else: manifest[0]["status"] = 500
                (root / "manifest.json").write_text(json.dumps(manifest))
                with self.assertRaises(ValueError): audit.build(root)

    def test_aggregates_never_export_symbol_prices_identity_fills_or_roi(self):
        result = audit.summarize(observation(item()))
        text = json.dumps(result)
        self.assertNotIn("SYNTHETICUSDC", text)
        for key in ("items", "setup_id", "boundary", "compatible_band", "structural_stop", "structural_target"):
            self.assertNotIn(key, result)
        for key in ("counterfactual_fills", "counterfactual_win_rate", "counterfactual_avg_r", "portfolio_roi_pct"):
            self.assertIsNone(result[key])
        self.assertEqual(result["new_trade_samples_added"], 0)
        self.assertFalse(result["actual_execution_evidence"])

    def test_cli_only_writes_new_report_never_overwrites_source_or_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); snapshot(root, observation(item()))
            sources = {p: p.read_bytes() for p in root.iterdir()}
            output = root / "new-report.json"
            command = [sys.executable, str(Path(audit.__file__)), "--snapshot", str(root), "--output", str(output)]
            subprocess.run(command, check=True, capture_output=True)
            first = output.read_bytes()
            run = subprocess.run(command, capture_output=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(output.read_bytes(), first)
            command[-1] = str(root / "readiness_review.json")
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            for path, raw in sources.items(): self.assertEqual(path.read_bytes(), raw)
