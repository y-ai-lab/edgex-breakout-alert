"""Run: python -m unittest analysis_terminal.test_outcomes -v."""
import ast
import json
import subprocess
import unittest
from pathlib import Path
from statistics import mean
from types import SimpleNamespace

from analysis_terminal.outcomes import evaluate_paper_signal, verified_result

INTERVAL = 900_000
NOW = 20 * INTERVAL
ROOT = Path(__file__).resolve().parent


def candle(index, high=105, low=95, close=100):
    return dict(time_ms=index * INTERVAL, open=100, high=high, low=low, close=close)


def signal(side="LONG", **overrides):
    value = dict(
        side=side, entry=100, stop=90 if side == "LONG" else 110,
        target=120 if side == "LONG" else 80, created_ms=2 * INTERVAL + 1,
        signal_candle_ms=INTERVAL,
    )
    value.update(overrides)
    return value


def evaluate(value, series):
    return evaluate_paper_signal(
        value, [SimpleNamespace(**c) for c in series], interval_ms=INTERVAL, now_ms=NOW
    )


class OutcomeTests(unittest.TestCase):
    def test_signal_and_earlier_candles_never_decide_outcome(self):
        result = evaluate(signal(), [candle(0, 150, 50), candle(1, 150, 50), candle(2)])
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["candles_checked"], 1)
        self.assertEqual(result["mfe_r"], 0.5)
        self.assertTrue(verified_result(result))

    def test_first_post_signal_candle_is_not_skipped_by_sentinel(self):
        for side, high, low in (("LONG", 120, 95), ("SHORT", 105, 80)):
            with self.subTest(side=side):
                result = evaluate(signal(side), [candle(1, 150, 50), candle(2, high, low)])
                self.assertEqual(result["status"], "TP")
                self.assertEqual(result["final_r"], 2)
                self.assertTrue(result["coverage_complete"])

    def test_exact_boundary_excludes_signal_candle_without_sentinel(self):
        value = signal(created_ms=2 * INTERVAL)
        self.assertEqual(evaluate(value, [candle(1, 150, 50), candle(2)])["status"], "OPEN")

    def test_mid_candle_event_excludes_pre_event_extremes(self):
        value = signal(created_ms=2 * INTERVAL + 1000, signal_candle_ms=None)
        result = evaluate(value, [candle(2, 150, 50), candle(3)])
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["candles_checked"], 1)
        self.assertFalse(result["coverage_complete"])
        self.assertTrue(result["partial_entry_candle_excluded"])

    def test_no_future_bars_never_falls_back_to_past_bar(self):
        result = evaluate(signal(), [candle(0, 150, 50), candle(1, 150, 50)])
        self.assertEqual(result["status"], "OPEN")
        self.assertIsNone(result["history_end_ms"])
        self.assertEqual(result["candles_checked"], 0)
        self.assertFalse(result["coverage_complete"])
        value = signal(result=result)
        resumed = evaluate(value, [candle(2)])
        self.assertTrue(resumed["coverage_complete"])

    def test_unclosed_bar_is_excluded(self):
        result = evaluate(signal(), [candle(2), candle(20, 150, 50)])
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["candles_checked"], 1)

    def test_same_candle_tp_sl_is_ambiguous_for_both_sides(self):
        for side in ("LONG", "SHORT"):
            result = evaluate(signal(side), [candle(2, 125, 75)])
            self.assertEqual(result["status"], "AMBIGUOUS")
            self.assertIsNone(result["final_r"])

    def test_tp1_sl_order_is_not_inferred(self):
        result = evaluate(signal(tp1=110), [candle(2, 115, 85)])
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertIsNone(result["final_r"])

    def test_excursions_and_cursor_stop_at_terminal_bar(self):
        for side, high, low in (("LONG", 120, 95), ("SHORT", 105, 80)):
            result = evaluate(signal(side), [candle(2, high, low), candle(3, 1000, 1)])
            self.assertEqual(result["mfe_r"], 2)
            self.assertEqual(result["mae_r"], 0.5)
            self.assertEqual(result["history_end_ms"], 2 * INTERVAL)
            self.assertEqual(result["candles_checked"], 1)

    def test_incremental_and_single_batch_results_agree(self):
        series = [candle(2), candle(3, 108, 96), candle(4, 120, 97), candle(5, 1000, 1)]
        first = evaluate(signal(), series[:2])
        resumed = evaluate(signal(result=first), series[1:])
        self.assertEqual(resumed, evaluate(signal(), series))

    def test_initial_and_internal_history_gaps_are_incomplete(self):
        for series in ([candle(3, 120, 95)], [candle(2), candle(4, 120, 95)]):
            result = evaluate(signal(), series)
            self.assertEqual(result["status"], "TP")
            self.assertFalse(verified_result(result))

    def test_incremental_gap_remains_incomplete(self):
        first = evaluate(signal(), [candle(2)])
        second = evaluate(signal(result=first), [candle(4)])
        final = evaluate(signal(result=second), [candle(5, 120, 95)])
        self.assertFalse(final["coverage_complete"])

    def test_missing_data_after_resolution_does_not_damage_coverage(self):
        result = evaluate(signal(), [candle(2, 120, 95), candle(7)])
        self.assertTrue(result["coverage_complete"])
        self.assertIs(evaluate(signal(result=result), [candle(10, 150, 50)]), result)

    def test_legacy_terminal_result_is_preserved_but_unverified(self):
        prior = dict(status="TP", final_r=2, coverage_complete=True)
        self.assertIs(evaluate(signal(result=prior), [candle(2, 125, 75)]), prior)
        self.assertFalse(verified_result(prior))

    def test_legacy_open_result_is_archived_and_recomputed(self):
        prior = dict(status="OPEN", mfe_r=100, history_end_ms=3 * INTERVAL)
        result = evaluate(signal(result=prior), [candle(2), candle(3)])
        self.assertEqual(result["mfe_r"], 0.5)
        self.assertEqual(result["legacy_result"], prior)
        self.assertTrue(verified_result(result))

    def test_invalid_trade_levels_are_not_resolved(self):
        for overrides in (dict(stop=100), dict(target=95), dict(side="UNKNOWN"), dict(entry=float("nan"))):
            self.assertEqual(evaluate(signal(**overrides), [candle(2)])["status"], "ERROR")

    def test_duplicate_and_unordered_bars_are_not_counted_twice(self):
        series = [candle(3), candle(2), candle(2)]
        result = evaluate(signal(), series)
        self.assertEqual(result["candles_checked"], 2)
        self.assertTrue(result["coverage_complete"])


class BrowserParityTests(unittest.TestCase):
    def test_javascript_syntax_and_server_browser_parity(self):
        fixtures = [
            (signal(side), series)
            for side in ("LONG", "SHORT")
            for series in (
                [candle(1, 150, 50)], [candle(1, 150, 50), candle(2)],
                [candle(2, 125, 75)], [candle(2, 120, 95), candle(3, 1000, 1)],
                [candle(2, 105, 80), candle(3, 1000, 1)],
                [candle(3)], [candle(2), candle(4)], [candle(20, 150, 50)],
            )
        ]
        fixtures += [
            (signal(created_ms=2 * INTERVAL + 1000, signal_candle_ms=None), [candle(2, 150, 50), candle(3)]),
            (signal(tp1=110), [candle(2, 115, 85)]),
            (signal(result=evaluate(signal(), [candle(2)])), [candle(3, 120, 95), candle(4, 1000, 1)]),
            (signal(source_candle_ms=INTERVAL, signal_candle_ms=None, created_ms=2 * INTERVAL), [candle(1, 150, 50), candle(2)]),
        ]
        js = r"""
const fs=require('fs');
const html=fs.readFileSync(process.argv[1], 'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new Function(script);
const start=script.indexOf('function verifiedResult(');
const end=script.indexOf('async function updateJournalOne(', start);
eval(script.slice(start,end));
Date.now=()=>Number(process.argv[2]);
const fixtures=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(fixtures.map(([p,s])=>evaluateCandles(p,s))));
"""
        proc = subprocess.run(
            ["node", "-e", js, str(ROOT / "index.html"), str(NOW)],
            input=json.dumps(fixtures), capture_output=True, text=True, check=True,
        )
        results = json.loads(proc.stdout)
        for (value, series), browser in zip(fixtures, results):
            with self.subTest(signal=value, series=series):
                self.assertEqual(browser, evaluate(value, series))


class ShadowPromotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load the actual metrics function without starting FastAPI or push clients.
        tree = ast.parse((ROOT / "server.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_shadow_v2_metrics")
        namespace = dict(Any=object, mean=mean, verified_result=verified_result)
        exec(compile(ast.Module(body=[function], type_ignores=[]), "server.py", "exec"), namespace)
        cls.metrics = staticmethod(namespace["_shadow_v2_metrics"])

    def test_twenty_legacy_results_cannot_pass_promotion(self):
        values = [dict(result=dict(status="TP", final_r=2, coverage_complete=True)) for _ in range(20)]
        result = self.metrics(values)
        self.assertEqual(result["resolved"], 0)
        self.assertEqual(result["unverified_results"], 20)
        self.assertFalse(result["promotion_pass"])
        self.assertEqual(result["sample_status"], "INSUFFICIENT SAMPLE")

    def test_sample_and_profit_gates_remain_unchanged(self):
        win = dict(result=evaluate(signal(), [candle(2, 120, 95)]))
        loss = dict(result=evaluate(signal(), [candle(2, 105, 90)]))
        self.assertFalse(self.metrics([win] * 19)["promotion_pass"])
        self.assertFalse(self.metrics([loss] * 20)["promotion_pass"])
        self.assertTrue(self.metrics([win] * 10 + [loss] * 10)["promotion_pass"])
        incomplete = dict(result=dict(win["result"], coverage_complete=False))
        self.assertEqual(self.metrics([win] * 19 + [incomplete])["resolved"], 19)


if __name__ == "__main__":
    unittest.main()
