import copy
import json
import math
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal.entry_band import diagnose, history_summary, observation, valid_observation
from analysis_terminal.setups import setup_identity
from analysis_terminal.readiness_history import STEP, daily_readiness
from analysis_terminal.test_replay import CONTRACT, MONITOR, START, c
from analysis_terminal import test_storage as _storage

server = _storage.server


def row(*, stop=90, target=130, roll=100, side="LONG", breakout=START-MONITOR):
    item = dict(ticker="TESTUSDC", direction=side, breakout_time_ms=breakout,
                breakout_level=roll, stage="CONFIRMATION_WAIT", confirmed=False,
                latest_4h_time_ms=START-MONITOR, latest_15m_time_ms=START-STEP,
                entry_band=diagnose(side, stop, target, roll, 2))
    item["setup_id"] = setup_identity(item)
    return item


class EntryBandTests(unittest.TestCase):
    def test_recorded_xle_conflict_cannot_be_solved_by_waiting_for_confirmation(self):
        band = diagnose("LONG", 62.380602225593464, 63.430301112796734, 63.1, 2)
        self.assertEqual(band["status"], "NO_OVERLAP")
        self.assertAlmostEqual(band["rr_boundary"], 62.73050185466122)
        self.assertIsNone(band["compatible_entry_band"])

    def test_long_short_band_matches_direct_reward_risk_inequalities(self):
        for side, stop, target in (("LONG", 90, 130), ("SHORT", 110, 70)):
            for roll in (85, 95, 100, 105, 115):
                b = diagnose(side, stop, target, roll, 2)
                for entry in (80, 91, 95, 99, 100, 101, 103, 105, 109, 120):
                    direct = (stop < entry < target and entry > roll and (target-entry)/(entry-stop) >= 2
                              if side == "LONG" else target < entry < stop and entry < roll and (entry-target)/(stop-entry) >= 2)
                    band = b["compatible_entry_band"]
                    included = bool(band and (entry > band["lower"] if side == "LONG" else entry >= band["lower"])
                                    and (entry <= band["upper"] if side == "LONG" else entry < band["upper"]))
                    self.assertEqual(included, direct, (side, roll, entry))

    def test_roll_at_rr_boundary_is_excluded_but_rr_boundary_inside_band_is_included(self):
        for side, stop, target in (("LONG", 90, 120), ("SHORT", 110, 80)):
            at = diagnose(side, stop, target, 100, 2)
            self.assertEqual(at["status"], "NO_OVERLAP")
            inside = diagnose(side, stop, target, 99 if side == "LONG" else 101, 2)
            band = inside["compatible_entry_band"]
            self.assertEqual(inside["status"], "COMPATIBLE")
            self.assertEqual(band["upper"] if side == "LONG" else band["lower"], 100)
            self.assertTrue(band["upper_inclusive"] if side == "LONG" else band["lower_inclusive"])

    def test_inverted_structure_and_bad_inputs_never_produce_an_entry_band(self):
        for side, stop, target in (("LONG", 110, 100), ("SHORT", 90, 100), ("LONG", 100, 100)):
            self.assertEqual(diagnose(side, stop, target, 100, 2)["status"], "INVALID_STRUCTURE")
        for bad in (None, True, "90", 0, -1, math.nan, math.inf, 10**1000):
            for index in range(4):
                args = [90, 120, 100, 2]; args[index] = bad
                b = diagnose("LONG", *args)
                self.assertEqual(b["status"], "INVALID_INPUT")
                self.assertIsNone(b["compatible_entry_band"])
                json.dumps(b, allow_nan=False)
        self.assertEqual(diagnose([], 90, 120, 100, 2)["status"], "INVALID_INPUT")

    def test_configured_minimum_is_used_without_mutating_strategy(self):
        self.assertEqual(diagnose("LONG", 90, 120, 100, 1)["status"], "COMPATIBLE")
        self.assertEqual(diagnose("LONG", 90, 120, 100, 2)["status"], "NO_OVERLAP")
        self.assertEqual(server.SETTINGS.min_rr, 2)

    def test_setup_identity_dedup_and_distinct_breakout_preserve_observation_inputs(self):
        rows = [row(), row(), row(breakout=START-2*MONITOR), dict(row(), setup_id="wrong")]
        before = copy.deepcopy(rows)
        o = observation(rows, observed_ms=START)
        self.assertTrue(valid_observation(o))
        self.assertEqual((o["evaluated"], o["unidentified"]), (2, 1))
        self.assertEqual(rows, before)

    def test_old_snapshots_are_missing_and_new_zero_is_a_valid_measurement(self):
        empty = observation([], observed_ms=START)
        h = history_summary([{}, dict(entry_bands=empty)])
        self.assertEqual((h["observations"], h["missing_observations"], h["evaluated_exposures"]), (1, 1, 0))
        self.assertIsNone(h["no_overlap_pct"])
        self.assertIsNone(history_summary([{}])["counts"])

    def test_same_setup_structure_update_is_saved_as_observations_not_two_trades(self):
        first, later = row(target=120), row(target=140)
        self.assertEqual(first["setup_id"], later["setup_id"])
        snapshots = [dict(time_ms=START, entry_bands=observation([first], observed_ms=START)),
                     dict(time_ms=START+STEP, entry_bands=observation([later], observed_ms=START+STEP))]
        h = daily_readiness(snapshots, [], [], now_ms=START+STEP, days=1)["summary"]
        self.assertEqual(h["entry_bands"]["counts"]["NO_OVERLAP"], 1)
        self.assertEqual(h["entry_bands"]["counts"]["COMPATIBLE"], 1)
        self.assertEqual(h["entry_bands"]["evaluated_exposures"], 2)
        self.assertEqual(h["signals"]["current"]["signals"], 0)

    def test_malformed_counts_identity_and_band_are_missing_not_zero(self):
        good = observation([row()], observed_ms=START)
        cases = []
        for field, value in (("version", True), ("evaluated", True), ("counts", {"NO_OVERLAP":1}), ("items", [None])):
            cases.append(dict(good, **{field: value}))
        for field, value in (("setup_id", "wrong"), ("direction", []), ("entry_band", {"status":"COMPATIBLE"})):
            bad = copy.deepcopy(good); bad["items"][0][field] = value; cases.append(bad)
        for bad in cases:
            self.assertFalse(valid_observation(bad))
            self.assertIsNone(history_summary([dict(entry_bands=bad)])["counts"])


class EntryBandIntegrationTests(unittest.IsolatedAsyncioTestCase):
    setUp = _storage.StorageTests.setUp

    def test_collector_preserves_full_band_and_identity_without_new_schema_or_signal(self):
        with server._db_connect() as conn:
            schema = list(conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
        before = row(); server._persist_scan_result({"1": CONTRACT}, [before])
        saved = server._load_market_history()[-1]["entry_bands"]
        self.assertTrue(valid_observation(saved))
        self.assertEqual(saved["items"][0]["entry_band"], before["entry_band"])
        self.assertEqual(saved["items"][0]["setup_id"], before["setup_id"])
        server._init_db()
        self.assertEqual(server._load_market_history()[-1]["entry_bands"], saved)
        with server._db_connect() as conn:
            self.assertEqual(schema, list(conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name")))
        self.assertEqual(server._subscription_count(), 1)
        self.assertEqual(server._load_paper_signals(), [])
        self.assertEqual(server._load_shadow_v2_signals(), [])
        self.assertEqual(server._load_push_events(), [])

    async def test_read_only_review_and_history_use_saved_diagnostics(self):
        server._persist_scan_result({"1": CONTRACT}, [row()])
        with server._db_connect() as conn: before = list(conn.iterdump())
        with patch.object(server, "_scan_market_rows", AsyncMock(return_value=({"1": CONTRACT}, [row()]))):
            async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://test") as client:
                review = (await client.get("/api/readiness-review")).json()
                self.assertEqual(review["review"]["current"]["ready_count"], 0)
                self.assertEqual(review["review"]["entry_bands"]["counts"]["COMPATIBLE"], 1)
                history = (await client.get("/api/readiness-history?days=7")).json()
                self.assertEqual(history["summary"]["entry_bands"]["observations"], 1)
        with server._db_connect() as conn: self.assertEqual(before, list(conn.iterdump()))

    def test_actual_analyzer_uses_raw_frozen_structure_and_ignores_future_candles(self):
        for side in ("LONG", "SHORT"):
            long = side == "LONG"
            m = [c(i*MONITOR, "HOUR_4", high=103 if long else 108, low=92 if long else 97,
                   close=102 if long else 98) for i in range(100)]
            e = [c(i*STEP, high=103, low=97, open=101 if long else 99,
                   close=102 if long else 98) for i in range(1500,1600)]
            with patch.object(server.scanner, "_ema", side_effect=([101,100]*2 if long else [99,100]*2)), \
                 patch.object(server.scanner, "_atr", side_effect=[2,1]*2), \
                 patch.object(server.DETECTOR, "_recent_breakout", return_value=(99,100)):
                a = server.analyze_contract(CONTRACT, m, e, as_of_ms=START)
                b = server.analyze_contract(CONTRACT, m+[c(START,"HOUR_4",high=1000,low=1)],
                                           e+[c(START,high=1000,low=1)], as_of_ms=START)
            self.assertEqual(a, b)
            self.assertEqual(a["entry_band"]["status"], "NO_OVERLAP")
            self.assertEqual(a["stage"], "RR_WAIT")
            self.assertTrue(a["confirmed"])
            self.assertTrue(valid_observation(observation([a], observed_ms=START)))


class EntryBandUITests(unittest.TestCase):
    def test_shipped_ui_explains_price_band_without_changing_entry_status(self):
        html = Path(__file__).with_name("index.html").read_text()
        payload = dict(html=html, long=diagnose("LONG",90,130,100,2),
                       short=diagnose("SHORT",110,70,100,2), blocked=diagnose("LONG",90,120,100,2))
        script = r"""
const fs=require('fs'),assert=require('assert'),x=JSON.parse(fs.readFileSync(0,'utf8'));
for(const m of x.html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new Function(m[1]);
const fmt=(n,d)=>Number(n).toFixed(d);
eval(x.html.slice(x.html.indexOf('function renderEntryBand('),x.html.indexOf('async function analyze(')));
assert.equal(renderEntryBand({}), '');
for(const [direction,band] of [['LONG',x.long],['SHORT',x.short]]){
 const r={direction,stage:'CONFIRMATION_WAIT',entry_band:band},before=JSON.stringify(r),view=renderEntryBand(r);
 assert(view.includes('両立する範囲'));assert(view.includes('RR≥2.00'));assert(view.includes('エントリー可否は上の状態'));
 assert(!view.includes('エントリー可能'));assert(!view.includes('class="good"'));assert.equal(JSON.stringify(r),before);
 assert(view.includes(direction==='LONG'?'100.00000000超':'100.00000000未満'));
}
assert(renderEntryBand({direction:'LONG',entry_band:x.blocked}).includes('両立しません'));
for(const b of [{...x.long,min_rr:null},{...x.long,status:'UNKNOWN'},{...x.long,rr_boundary:'<img>'},{...x.long,compatible_entry_band:null}]){
 const view=renderEntryBand({direction:'LONG',entry_band:b});assert(view.includes('確認'));assert(!view.includes('両立する範囲'));assert(!view.includes('<img>'));
}
"""
        subprocess.run(["node", "-e", script], input=json.dumps(payload), text=True, capture_output=True, check=True)
