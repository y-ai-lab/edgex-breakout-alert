"""Exercise the real public history adapter, rather than mocking fetch_history."""
import asyncio
import copy
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
