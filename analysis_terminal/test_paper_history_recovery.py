"""Exercise the real public history adapter, rather than mocking fetch_history."""
import asyncio
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analysis_terminal import test_storage
from analysis_terminal import paper_execution as paper
from analysis_terminal.history import PATH
from validation.run_paper_execution_check import Runner, load_capture, visible_bars

server = test_storage.server
SOURCE = Path(__file__).resolve().parents[1] / "validation/fixtures/velvet_ready_execution.json"


class RealPublicPaperRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Only the fixture's executor dispatch is synchronous; run the real history
        # adapter and its callable, bounds, identity and price validation unchanged.
        async def fixture_transport(call,*args,**kwargs):
            return call(*args,**kwargs)
        context=patch("analysis_terminal.history.asyncio.to_thread",fixture_transport)
        context.start()
        self.addCleanup(context.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.capture, self.contract, self.bars, self.row, _ = load_capture(SOURCE)
        self.runner = Runner(Path(self.directory.name)/"check", self.capture, self.contract, self.row, server_hook=True)
        self.addCleanup(self.runner.close)
        detected = self.capture["signal"]["created_ms"]
        self.runner.tick(detected,visible_bars(self.bars,detected),rows=[self.row])
        self.fill_ms = (detected//paper.STEP+1)*paper.STEP
        self.runner.tick(self.fill_ms+1,visible_bars(self.bars,self.fill_ms+1))
        self.now = self.bars[-1].time_ms+paper.STEP+1
        self.gapped = visible_bars(self.bars,self.now,missing=self.fill_ms)
        self.runner.tick(self.now,self.gapped)
        order = self.runner.report(self.now)["latest"][0]
        self.assertEqual((order["status"],order["quality"],order["next_candle_ms"]),("OPEN","HISTORY_GAP",self.fill_ms))

    def response(self,path,params):
        self.assertEqual(path,PATH)
        self.assertEqual(params["contractId"],self.contract.contract_id)
        self.assertEqual(params["klineType"],"MINUTE_15")
        self.assertEqual(params["priceType"],"LAST_PRICE")
        self.assertEqual(params["size"],"256")
        start,end=int(params["filterBeginKlineTimeInclusive"]),int(params["filterEndKlineTimeExclusive"])
        self.assertEqual(start,self.fill_ms)
        self.assertGreater(start,self.capture["signal"]["source_candle_ms"]+paper.STEP)
        self.assertEqual(end,self.now//paper.STEP*paper.STEP)
        payload = copy.deepcopy(self.capture["kline_response"])
        payload["data"]["dataList"] = [v for v in payload["data"]["dataList"] if start <= int(v["klineTime"]) < end]
        return payload

    def recover(self,side_effect):
        cache = (self.now/1000,{(self.contract.contract_id,"MINUTE_15"):self.gapped})
        with patch.object(server,"_snapshot_cache",cache), patch.object(server.time,"time",return_value=self.now/1000), \
             patch.object(server.CLIENT,"_get_json_sync",side_effect=side_effect) as public, \
             patch.object(server.CLIENT,"_get_private_json_sync",side_effect=AssertionError("No private transport")) as private:
            asyncio.run(server._recover_simulation_history({self.contract.contract_id:self.contract}))
            private.assert_not_called()
        return public

    def test_recorded_market_gap_uses_real_adapter_and_resolves_exact_sl_once(self):
        public = self.recover(self.response)
        after = self.runner.report(self.now)
        order = after["latest"][0]
        self.assertEqual(order["status"],"SL")
        public.assert_called_once()
        expected=json.loads((SOURCE.parents[1]/"paper_execution_check_latest.json").read_text())
        self.assertEqual(order,expected["nominal_order"])
        self.assertAlmostEqual(after["account"]["cash_usdc"],expected["independent_decimal"]["final_cash_usdc"],places=8)
        events = self.runner.events()
        self.assertEqual([e["status"] for e in events],["PENDING","OPEN","OPEN","SL"])
        self.assertEqual(events[2]["quality"],"HISTORY_GAP")
        unused=self.recover(lambda *args: self.fail("Terminal order fetched twice"))
        unused.assert_not_called()
        self.assertEqual(self.runner.events(),events)
        self.assertEqual(self.runner.report(self.now)["account"]["cash_usdc"],after["account"]["cash_usdc"])

    def test_failed_or_invalid_public_recovery_preserves_gap_and_sanitizes_logs(self):
        def wrong_price_type(path,params):
            p=self.response(path,params)
            p["data"]["dataList"][0]["priceType"]="MARK_PRICE"
            return p
        def unavailable(*args):
            raise RuntimeError("private-test-marker")
        before=self.runner.report(self.now)
        events=self.runner.events()
        for response in (wrong_price_type,unavailable):
            with self.subTest(response=response.__name__),patch("builtins.print") as logs:
                public=self.recover(response)
                public.assert_called_once()
                self.assertNotIn("private-test-marker",str(logs.call_args_list))
            after=self.runner.report(self.now)
            self.assertEqual(after["latest"],before["latest"])
            self.assertEqual(after["metrics"],before["metrics"])
            self.assertEqual(after["account"]["cash_usdc"],before["account"]["cash_usdc"])
            self.assertFalse(after["account"]["paused"])
            self.assertEqual(self.runner.events(),events)


class PendingPublicPaperRecoveryTests(unittest.TestCase):
    response = RealPublicPaperRecoveryTests.response
    recover = RealPublicPaperRecoveryTests.recover

    def setUp(self):
        async def fixture_transport(call, *args, **kwargs):
            return call(*args, **kwargs)
        context = patch("analysis_terminal.history.asyncio.to_thread", fixture_transport)
        context.start()
        self.addCleanup(context.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.capture, self.contract, self.bars, self.row, _ = load_capture(SOURCE)
        self.runner = Runner(Path(self.directory.name)/"check", self.capture, self.contract, self.row, server_hook=True)
        self.addCleanup(self.runner.close)
        detected = self.capture["signal"]["created_ms"]
        self.runner.tick(detected, visible_bars(self.bars, detected), rows=[self.row])
        self.fill_ms = (detected//paper.STEP+1)*paper.STEP
        # The first missed fill foot is now closed, exactly at the original TTL.
        self.now = detected + paper.POLICY["pending_ttl_ms"]
        self.gapped = visible_bars(self.bars, self.now, missing=self.fill_ms)
        self.runner.tick(self.now, self.gapped)
        order = self.runner.report(self.now)["latest"][0]
        self.assertEqual((order["status"], order["quality"]), ("PENDING", "MISSING_FILL_CANDLE"))
        self.assertEqual(order["expires_ms"], self.now)

    def test_closed_fill_recovered_with_original_ttl_and_observed_time(self):
        self.recover(self.response).assert_called_once()
        after = self.runner.report(self.now)
        order = after["latest"][0]
        self.assertEqual((order["status"], order["quality"]), ("OPEN", "TRACKING"))
        self.assertEqual(order["filled_ms"], self.fill_ms)
        self.assertEqual(order["fill_observed_ms"], self.now)
        self.assertEqual(order["next_candle_ms"], self.fill_ms+paper.STEP)
        self.assertEqual(order["expires_ms"], self.now)
        events = self.runner.events()
        self.assertEqual([e["status"] for e in events], ["PENDING", "PENDING", "OPEN"])
        self.assertEqual((events[-1]["market_ms"], events[-1]["observed_ms"]), (self.fill_ms, self.now))
        self.recover(lambda *args: self.fail("Recovered fill fetched twice")).assert_not_called()
        self.assertEqual(self.runner.events(), events)
        self.assertEqual(self.runner.report(self.now)["account"]["cash_usdc"], after["account"]["cash_usdc"])
        final_now = self.bars[-1].time_ms+paper.STEP+1
        self.runner.tick(final_now, visible_bars(self.bars, final_now))
        final = self.runner.report(final_now)
        expected = json.loads((SOURCE.parents[1]/"paper_execution_check_latest.json").read_text())
        expected["nominal_order"]["fill_observed_ms"] = self.now
        self.assertEqual(final["latest"][0], expected["nominal_order"])
        self.assertAlmostEqual(final["account"]["cash_usdc"], expected["independent_decimal"]["final_cash_usdc"], places=8)

    def test_forming_fill_is_not_recovered_and_expired_intent_stays_unfilled(self):
        before = self.runner.report(self.now)["latest"]
        events = self.runner.events()
        for now in (self.fill_ms-1, self.fill_ms+paper.STEP-1, self.now+1):
            self.now = now
            self.recover(lambda *args: self.fail("Forming or expired fill requested")).assert_not_called()
            self.assertEqual(self.runner.report(now)["latest"], before)
            self.assertEqual(self.runner.events(), events)
        self.runner.tick(self.now, visible_bars(self.bars, self.now))
        final = self.runner.report(self.now)
        self.assertEqual(final["latest"][0]["status"], "EXPIRED")
        self.assertNotIn("fill_price", final["latest"][0])
        self.assertEqual(final["account"]["cash_usdc"], 10000)

    def test_unavailable_or_invalid_fill_keeps_intent_and_cash(self):
        def unavailable(*args):
            raise RuntimeError("private-fill-test-marker")
        def empty(path, params):
            payload = self.response(path, params)
            payload["data"]["dataList"] = []
            return payload
        def wrong_price_type(path, params):
            payload = self.response(path, params)
            payload["data"]["dataList"][0]["priceType"] = "MARK_PRICE"
            return payload
        before = self.runner.report(self.now)
        events = self.runner.events()
        for response in (unavailable, empty, wrong_price_type):
            with self.subTest(response=response.__name__), patch("builtins.print") as logs:
                self.recover(response).assert_called_once()
                self.assertNotIn("private-fill-test-marker", str(logs.call_args_list))
            after = self.runner.report(self.now)
            self.assertEqual(after["latest"], before["latest"])
            self.assertEqual(after["metrics"], before["metrics"])
            self.assertEqual(after["account"]["cash_usdc"], 10000)
            self.assertFalse(after["account"]["paused"])
            self.assertEqual(self.runner.events(), events)

    def test_bad_cached_fill_recovers_through_validated_public_history(self):
        foot = next(c for c in self.bars if c.time_ms == self.fill_ms)
        self.runner.tick(self.now, self.gapped+[replace(foot, high=foot.low/2)])
        before = self.runner.report(self.now)["latest"][0]
        self.assertEqual((before["status"], before["quality"]), ("PENDING", "DATA_ERROR"))
        self.recover(self.response).assert_called_once()
        after = self.runner.report(self.now)["latest"][0]
        self.assertEqual((after["status"], after["quality"]), ("OPEN", "TRACKING"))
        self.assertEqual(after["execute_ms"], before["execute_ms"])
        self.assertEqual(after["expires_ms"], before["expires_ms"])

    def test_synthetic_gap_open_still_rejects_original_invalid_levels(self):
        def below_stop(path, params):
            payload = self.response(path, params)
            price = str(self.row["stop_loss"]*0.9)
            for raw in payload["data"]["dataList"]:
                if int(raw["klineTime"]) == self.fill_ms:
                    for name in ("open", "high", "low", "close"):
                        raw[name] = price
            return payload
        self.recover(below_stop).assert_called_once()
        after = self.runner.report(self.now)
        order = after["latest"][0]
        self.assertEqual((order["status"], order["reason"]), ("REJECTED", "LEVELS_OR_RR_INVALID_AT_FILL"))
        self.assertNotIn("fill_price", order)
        self.assertEqual(order["stop"], self.row["stop_loss"])
        self.assertEqual(order["target"], self.row["take_profit"])
        self.assertEqual(after["account"]["cash_usdc"], 10000)
        self.assertEqual(self.runner.events()[-1]["status"], "REJECTED")
