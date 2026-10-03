"""Missing-bar recovery never guesses outcomes or rewrites historical terminal rows."""
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

from analysis_terminal import test_tracking as _tracking
from analysis_terminal.outcome_history import BACKFILL_BARS, BACKFILL_REQUESTS, consecutive_window, merge_candles
from analysis_terminal.outcomes import verified_result

server = _tracking.server
STEP, NOW = _tracking.STEP, _tracking.NOW
candle = _tracking.candle
CONTRACT = server.scanner.Contract("1", "TESTUSDC", "USDC", True, True)


def signal(key="modern", **values):
    return _tracking.signal(key, setup_id=f"setup-v1:TESTUSDC:LONG:{STEP}:{key}", **values)


class OutcomeWindowTests(unittest.TestCase):
    def test_matching_prices_with_different_auxiliary_metadata_can_merge(self):
        ws=candle()
        rest=replace(ws, contract_name="1", volume=2, value=200, trades=1)
        self.assertEqual(merge_candles([ws], [rest]), [ws])

    def test_only_closed_post_signal_consecutive_bars_are_selected(self):
        rows = [candle(1, 150, 50), candle(2), candle(4, 120), candle(10, 150, 50)]
        prefix, gap, end = consecutive_window(signal(), rows, CONTRACT, "MINUTE_15", now_ms=NOW)
        self.assertEqual(([c.time_ms for c in prefix], gap, end), ([2*STEP], 3*STEP, NOW))

    def test_bad_grid_identity_prices_and_conflicting_revisions_fail(self):
        for row in (replace(candle(), time_ms=2*STEP+1), replace(candle(), contract_id="2"),
                    replace(candle(), high=99), replace(candle(), close=float("nan"))):
            with self.subTest(row=row), self.assertRaises(ValueError):
                consecutive_window(signal(), [row], CONTRACT, "MINUTE_15", now_ms=NOW)
        with self.assertRaises(ValueError):
            consecutive_window(signal(), [candle(), candle(high=106)], CONTRACT, "MINUTE_15", now_ms=NOW)


class OutcomeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = _tracking.TrackingStorageTests.setUp
    insert = _tracking.TrackingStorageTests.insert
    load = _tracking.TrackingStorageTests.load
    refresh = _tracking.TrackingStorageTests.refresh

    async def test_missing_first_bar_restores_earlier_sl_before_later_tp(self):
        for model in ("current", "shadow"):
            original = signal(model)
            self.insert(model, [original])
            with patch.object(server, "fetch_snapshots", AsyncMock(return_value={("1", "MINUTE_15"): [candle(1,150,50),candle(3,120)]})), patch.object(server, "fetch_history", AsyncMock(return_value=[candle(2,105,85)])) as history:
                report = await self.refresh(model)
            saved = self.load(model)[model]
            result = saved.pop("result")
            saved.pop("updated_ms", None)
            self.assertEqual(saved, original)
            self.assertEqual((result["status"],result["outcome_time_ms"],result["candles_checked"]), ("SL",2*STEP,1))
            self.assertTrue(verified_result(result))
            self.assertEqual((report["backfill_requests"],report["backfill_recovered"],report["gap_deferred"],report["status"]), (1,1,0,"OK"))
            self.assertEqual(history.call_args.args[3:5], (2*STEP, NOW))

    async def test_failed_recovery_holds_gap_and_retry_resolves_ambiguously(self):
        self.insert("shadow", [signal()])
        snapshots = {("1","MINUTE_15"): [candle(2),candle(4,120)]}
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value=snapshots)), patch.object(server,"fetch_history",AsyncMock(side_effect=RuntimeError("private-test-marker"))), patch('builtins.print') as log:
            report = await self.refresh("shadow")
        first = self.load("shadow")["modern"]["result"]
        self.assertEqual((first["status"],first["history_end_ms"],first["candles_checked"]), ("OPEN",2*STEP,1))
        self.assertTrue(verified_result(first))
        self.assertEqual((report["backfill_errors"],report["gap_deferred"],report["status"]), (1,1,"PARTIAL"))
        self.assertNotIn("private-test-marker", str(log.call_args_list))
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value=snapshots)), patch.object(server,"fetch_history",AsyncMock(return_value=[candle(3,125,75)])):
            await self.refresh("shadow")
        result = self.load("shadow")["modern"]["result"]
        self.assertEqual((result["status"],result["outcome_time_ms"],result["candles_checked"],result["final_r"]), ("AMBIGUOUS",3*STEP,2,None))
        self.assertTrue(verified_result(result))

    async def test_no_first_observation_preserves_prior_record(self):
        original = signal()
        self.insert("shadow", [original])
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value={})), patch.object(server,"fetch_history",AsyncMock(return_value=[])):
            report = await self.refresh("shadow")
        self.assertEqual(self.load("shadow")["modern"], original)
        self.assertEqual((report["gap_deferred"],report["evaluated"],report["status"]), (1,0,"ERROR"))

    async def test_observed_terminal_prefix_requires_no_backfill(self):
        self.insert("shadow", [signal()])
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value={("1","MINUTE_15"):[candle(2,120),candle(4)]})), patch.object(server,"fetch_history",AsyncMock()) as history:
            report = await self.refresh("shadow")
            history.assert_not_awaited()
        self.assertEqual(self.load("shadow")["modern"]["result"]["status"], "TP")
        self.assertEqual(report["gap_deferred"], 0)

    async def test_request_and_time_window_budget_is_bounded_and_rotates(self):
        self.insert("shadow", [signal(str(i), created_ms=(2+i)*STEP+1, signal_candle_ms=(1+i)*STEP) for i in range(BACKFILL_REQUESTS+2)])
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value={})), patch.object(server,"fetch_history",AsyncMock(return_value=[])) as history:
            report = await self.refresh("shadow")
        self.assertEqual(history.await_count, BACKFILL_REQUESTS)
        self.assertEqual(report["gap_deferred"], BACKFILL_REQUESTS+2)
        for call in history.call_args_list:
            self.assertLessEqual(call.args[4]-call.args[3], BACKFILL_BARS*STEP)
            self.assertEqual(call.kwargs["max_pages"], 1)
        first = {call.args[3] for call in history.call_args_list}
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value={})), patch.object(server,"fetch_history",AsyncMock(return_value=[])) as history, patch.object(server.time,"time",return_value=(NOW+STEP)/1000):
            await server._refresh_shadow_v2_results({"1":CONTRACT})
        self.assertTrue({call.args[3] for call in history.call_args_list} - first)

    async def test_long_downtime_fetches_only_one_bounded_window(self):
        self.insert("shadow", [signal()])
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value={})), patch.object(server,"fetch_history",AsyncMock(return_value=[])) as history, patch.object(server.time,"time",return_value=1000*STEP/1000):
            await server._refresh_shadow_v2_results({"1":CONTRACT})
        self.assertEqual(history.call_args.args[3:5], (2*STEP, (2+BACKFILL_BARS)*STEP))

    async def test_existing_terminal_and_incomplete_records_are_preserved(self):
        rows=[signal("terminal",result=dict(status="TP",final_r=2)),
              signal("incomplete",result=dict(status="OPEN",evaluation_version=2,coverage_complete=False,history_end_ms=3*STEP))]
        self.insert("shadow",rows)
        with patch.object(server,"fetch_snapshots",AsyncMock(return_value={("1","MINUTE_15"):[candle(4,120)]})), patch.object(server,"fetch_history",AsyncMock()) as history:
            report = await self.refresh("shadow")
            history.assert_not_awaited()
        self.assertEqual(self.load("shadow"), {s["key"]:s for s in rows})
        self.assertEqual(report["unverified_pending"], 1)
