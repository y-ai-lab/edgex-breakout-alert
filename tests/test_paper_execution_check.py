"""Execution evidence from a real recorded READY; no production DB or network."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from validation.run_paper_execution_check import load_capture, run, visible_bars

SOURCE = Path(__file__).resolve().parents[1] / "validation/fixtures/velvet_ready_execution.json"


class RecordedPaperExecutionCheckTests(unittest.TestCase):
    def test_real_ready_through_fill_stop_fees_restart_and_missing_bar_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/"check"
            report = run(SOURCE, target)
            self.assertEqual(report["verdict"], "PASS")
            self.assertEqual(report["unique_recorded_ready"], 1)
            self.assertEqual(report["public_bars"], 17)
            self.assertFalse(report["eligible_for_live_promotion"])
            self.assertEqual(report["validation_samples_added_to_production"], 0)
            order = report["nominal_order"]
            self.assertEqual(order["status"], "SL")
            self.assertEqual(order["quantity"], 10000)
            self.assertAlmostEqual(order["fill_price"], 0.077405478, places=12)
            self.assertAlmostEqual(order["net_pnl_usdc"], -57.75416082454083, places=8)
            self.assertEqual(report["checks"]["missing_bar_recovery"]["status"], "SL")
            self.assertEqual(report["checks"]["stale_120s_rejected"]["status"], "REJECTED")
            with self.assertRaises(FileExistsError):
                run(SOURCE, target)

    def test_source_rejects_missing_duplicate_wrong_identity_and_shadow(self):
        original = json.loads(SOURCE.read_text())
        broken = []
        x=copy.deepcopy(original);x["signal"]["key"]=x["signal"]["key"].replace("current:","v2:");broken.append(x)
        x=copy.deepcopy(original);x["kline_response"]["data"]["dataList"].pop(3);broken.append(x)
        x=copy.deepcopy(original);x["kline_response"]["data"]["dataList"].append(x["kline_response"]["data"]["dataList"][0]);broken.append(x)
        x=copy.deepcopy(original);x["kline_response"]["data"]["dataList"][0]["priceType"]="MARK_PRICE";broken.append(x)
        x=copy.deepcopy(original);x["contract"]["contractName"]="OTHERUSDC";broken.append(x)
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/"bad.json"
            for capture in broken:
                with self.subTest(capture=capture["signal"]["key"]):
                    p.write_text(json.dumps(capture))
                    with self.assertRaises(ValueError):
                        load_capture(p)

    def test_forming_future_bars_hidden_and_production_volume_forbidden(self):
        capture, _, bars, _, _ = load_capture(SOURCE)
        now = capture["signal"]["created_ms"]
        shown = visible_bars(bars, now)
        self.assertEqual(len(shown), 2)
        self.assertEqual(shown[-1].high, shown[-1].open)
        self.assertEqual(shown[-1].low, shown[-1].open)
        self.assertEqual(shown[-1].close, shown[-1].open)
        self.assertEqual(shown[-1].volume, 0)
        with self.assertRaises(ValueError):
            run(SOURCE, Path("/data/forbidden-execution-check"))


if __name__ == "__main__":
    unittest.main()
