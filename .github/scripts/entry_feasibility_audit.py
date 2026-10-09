"""Offline price geometry only; never changes READY, candidates or orders."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis_terminal import entry_band, pending_entry_replay as frozen

STATUSES = ("NET_COMPATIBLE", "NET_NO_OVERLAP", "NET_NO_VALID_PRICE", "NOT_EVALUABLE")
STAGES = {"READY", "CONFIRMATION_WAIT", "RR_WAIT", "STRUCTURE_WAIT", "RETEST_WAIT",
          "BREAKOUT_WAIT", "TREND_WAIT", "DATA_WAIT"}


def diagnose(band):
    """Use one nominal price for strict roll confirmation and hypothetical entry.

    This intersection is NOT the pending-limit entry gate. Confirmation and a
    subsequent pullback fill have different prices and times in that model.
    Tick sizes, candle colour, liquidity and actual execution are not evaluated.
    """
    if (frozen.FEE, frozen.SLIP, frozen.MIN_NET_RR) != (.0005, .0002, 2):
        raise ValueError("Frozen diagnostic costs/minimum changed")
    if not isinstance(band, dict) or band != entry_band.diagnose(
            band.get("direction"), band.get("structural_stop"),
            band.get("structural_target"), band.get("confirmation_level"), band.get("min_rr")):
        raise ValueError("Original gross band does not reproduce")
    result = dict(status="NOT_EVALUABLE", boundary=None, compatible_band=None)
    if band["status"] in {"INVALID_INPUT", "INVALID_STRUCTURE"}:
        return result
    if band["min_rr"] != 2:
        raise ValueError("Only the frozen 2R rule is supported")
    side, stop, target, roll = (band[k] for k in (
        "direction", "structural_stop", "structural_target", "confirmation_level"))
    boundary = frozen.pullback_trigger(stop, target, side)
    # Keep nominal structural validity as in the original close-price band.
    # Do not broaden it using slippage-adjusted stop/target coordinates.
    if boundary is None or not (min(stop, target) < boundary < max(stop, target)):
        result["status"] = "NET_NO_VALID_PRICE"
        return result
    result["boundary"] = boundary
    if side == "LONG":
        combined = dict(lower=max(stop, roll), upper=boundary,
                        lower_inclusive=False, upper_inclusive=True)
    else:
        combined = dict(lower=boundary, upper=min(stop, roll),
                        lower_inclusive=True, upper_inclusive=False)
    compatible = combined["lower"] < combined["upper"]
    result["status"] = "NET_COMPATIBLE" if compatible else "NET_NO_OVERLAP"
    result["compatible_band"] = combined if compatible else None
    if compatible and band["status"] != "COMPATIBLE":
        raise ValueError("Cost band unexpectedly broadened the original band")
    return result


def summarize(observed):
    if not entry_band.valid_observation(observed):
        raise ValueError("Invalid, duplicate or inconsistent setup observation")
    if observed.get("basis") != "identified_setup_per_recorded_bucket":
        raise ValueError("Unsupported observation basis")
    net, stages, confirmed_stages = Counter(), {}, {}
    cost_only_lost = 0
    for item in observed["items"]:
        stage = item.get("stage")
        if not isinstance(stage, str) or stage not in STAGES or type(item.get("confirmed")) is not bool:
            raise ValueError("Missing or invalid stage/confirmation evidence")
        for name in ("breakout_time_ms", "latest_4h_time_ms", "latest_15m_time_ms"):
            timestamp = item.get(name)
            if type(timestamp) is not int or not 0 <= timestamp <= observed["observed_ms"]:
                raise ValueError("Missing or future source timestamp")
        status = diagnose(item["entry_band"])["status"]
        net[status] += 1
        stages.setdefault(stage, Counter())[status] += 1
        if item["confirmed"]:
            confirmed_stages.setdefault(stage, Counter())[status] += 1
        cost_only_lost += (item["entry_band"]["status"] == "COMPATIBLE"
                          and status in {"NET_NO_OVERLAP", "NET_NO_VALID_PRICE"})
    return dict(
        scope="SINGLE_SAVED_OBSERVATION_PRICE_GEOMETRY_ONLY",
        price_basis="SAME_NOMINAL_CONFIRMATION_AND_HYPOTHETICAL_ENTRY_PRICE",
        observed_ms=observed["observed_ms"], identified_setups=observed["evaluated"],
        unidentified=observed["unidentified"], gross_counts=dict(observed["counts"]),
        net_counts={k: net[k] for k in STATUSES}, cost_only_lost_gross_compatible=cost_only_lost,
        by_stage={stage: {k: counts[k] for k in STATUSES} for stage, counts in sorted(stages.items())},
        confirmed_by_stage={stage: {k: counts[k] for k in STATUSES}
                            for stage, counts in sorted(confirmed_stages.items())},
        cost_assumption=dict(one_way_fee_bps=5, one_way_slippage_bps=2, min_net_rr=2,
                             net_rr_basis="FEE_INCLUSIVE_STOP_RISK"),
        new_trade_samples_added=0, actual_execution_evidence=False,
        counterfactual_fills=None, counterfactual_win_rate=None,
        counterfactual_avg_r=None, portfolio_roi_pct=None,
        limitations=[
            "Previously observed data; this is not a preregistered strategy experiment.",
            "A compatible price band is not a fill, READY signal or profitability evidence.",
            "Pending-entry confirms first and fills later; this intersection is not its eligibility gate.",
            "Unidentified and invalid structures are not fabricated as valid no-entry observations.",
            "Do not sum repeated setup observations as independent trades.",
            "No candle colour, tick/size rounding, order book or actual execution is evaluated.",
            "Price-distance R, net stop-risk R and account ROI are different units."])


def build(snapshot):
    snapshot = Path(snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    sources = [x for x in manifest if x.get("endpoint") == "readiness_review"]
    if len(sources) != 1 or sources[0].get("status") != 200:
        raise ValueError("Missing or duplicate successful readiness source")
    raw = (snapshot / "readiness_review.json").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != sources[0].get("sha256"):
        raise ValueError("Readiness source hash mismatch")
    payload = json.loads(raw)
    observed = payload.get("review", {}).get("entry_bands")
    result = summarize(observed)
    fetched = sources[0].get("fetched_ms")
    if type(fetched) is not int or fetched < result["observed_ms"]:
        raise ValueError("Readiness observation is newer than its fetch")
    return dict(result, source_sha256=digest, source_fetched_ms=fetched)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.snapshot)
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print("Entry feasibility diagnostic verified; no strategy or production writes.")


if __name__ == "__main__":
    main()
