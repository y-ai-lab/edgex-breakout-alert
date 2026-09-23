import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import (
    Candle,
    Contract,
    JsonStateStore,
    RollReversalDetector,
    Settings,
    _atr,
    _manual_account_asset,
    build_risk_plan,
    format_signal,
)


def strategy_settings():
    env = {
        "EDGEX_EQUITY_USDC": "1000",
        "EDGE_X_INTERVALS": "HOUR_4,MINUTE_15",
        "EDGE_X_MONITOR_INTERVAL": "HOUR_4",
        "EDGE_X_ENTRY_INTERVAL": "MINUTE_15",
        "EDGE_X_TREND_FAST_EMA": "20",
        "EDGE_X_TREND_SLOW_EMA": "50",
        "EDGE_X_ROLL_LOOKBACK": "20",
        "EDGE_X_ROLL_MAX_AGE": "6",
        "EDGE_X_RETEST_LOOKBACK": "4",
        "EDGE_X_ATR_PERIOD": "14",
        "EDGE_X_ATR_STOP_BUFFER": "0.5",
        "EDGE_X_ATR_TARGET_BUFFER": "0.25",
        "EDGE_X_RETEST_ATR_TOLERANCE": "0.35",
        "EDGE_X_MIN_RR": "2.0",
        "EDGE_X_SPLIT_RR": "3.0",
        "EDGE_X_PULLBACK_SWING_LOOKBACK": "5",
        "EDGE_X_RISK_PER_TRADE": "0.05",
        "EDGE_X_HISTORY_SIZE": "96",
    }
    with patch.dict(os.environ, env, clear=True):
        return Settings.from_env(dry_run_override=True)


def make_contract():
    return Contract(
        contract_id="10000001",
        contract_name="BTCUSDC",
        quote_coin="USDC",
        enable_trade=True,
        enable_display=True,
        step_size=0.1,
        min_order_size=0.1,
        max_order_size=10000.0,
        max_long_leverage=50.0,
        max_short_leverage=50.0,
    )


def candle(interval, index, open_price, high, low, close, value=100000.0):
    interval_ms = 4 * 60 * 60 * 1000 if interval == "HOUR_4" else 15 * 60 * 1000
    return Candle(
        contract_id="10000001",
        contract_name="BTCUSDC",
        interval=interval,
        time_ms=(index + 1) * interval_ms,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=100.0,
        value=value,
        trades=100.0,
    )


def long_monitor():
    rows = []
    for i in range(55):
        close = 80.0 + i * 0.3
        rows.append(candle("HOUR_4", i, close - 0.1, close + 0.4, close - 0.4, close))
    rows.append(candle("HOUR_4", 55, 96.0, 102.0, 95.5, 100.0))
    rows.append(candle("HOUR_4", 56, 100.0, 103.0, 99.5, 101.0))
    rows.append(candle("HOUR_4", 57, 101.0, 104.0, 100.0, 102.0))
    rows.append(candle("HOUR_4", 58, 102.0, 105.0, 101.0, 103.0))
    rows.append(candle("HOUR_4", 59, 103.0, 106.0, 102.0, 104.0))
    return rows


def long_entries(candidate_close=98.0):
    rows = []
    for i in range(15):
        rows.append(candle("MINUTE_15", i, 100.0, 100.5, 99.5, 100.0))
    rows.append(candle("MINUTE_15", 15, 100.0, 100.2, 98.5, 99.0))
    rows.append(candle("MINUTE_15", 16, 99.0, 99.2, 96.4, 97.2))
    rows.append(candle("MINUTE_15", 17, 97.2, 97.7, 96.5, 97.0))
    rows.append(candle("MINUTE_15", 18, 97.0, 98.0, 96.8, 97.2))
    rows.append(candle("MINUTE_15", 19, candidate_close - 1.0, candidate_close + 0.4, 96.7, candidate_close))
    return rows


def short_monitor():
    rows = []
    for i in range(55):
        close = 120.0 - i * 0.3
        rows.append(candle("HOUR_4", i, close + 0.1, close + 0.4, close - 0.4, close))
    rows.append(candle("HOUR_4", 55, 104.0, 104.5, 98.0, 100.0))
    rows.append(candle("HOUR_4", 56, 100.0, 101.0, 97.0, 99.0))
    rows.append(candle("HOUR_4", 57, 99.0, 100.0, 96.0, 98.0))
    rows.append(candle("HOUR_4", 58, 98.0, 99.0, 95.0, 97.0))
    rows.append(candle("HOUR_4", 59, 97.0, 98.0, 94.0, 96.0))
    return rows


def short_entries():
    rows = []
    for i in range(15):
        rows.append(candle("MINUTE_15", i, 100.0, 100.5, 99.5, 100.0))
    rows.append(candle("MINUTE_15", 15, 100.0, 101.5, 99.8, 101.0))
    rows.append(candle("MINUTE_15", 16, 101.0, 103.5, 100.8, 103.0))
    rows.append(candle("MINUTE_15", 17, 103.0, 103.6, 102.2, 103.2))
    rows.append(candle("MINUTE_15", 18, 103.2, 103.5, 102.4, 103.0))
    rows.append(candle("MINUTE_15", 19, 103.0, 103.3, 101.5, 102.0))
    return rows


class RollReversalStrategyTests(unittest.TestCase):
    def setUp(self):
        self.settings = strategy_settings()
        self.contract = make_contract()
        self.detector = RollReversalDetector(self.settings)

    def test_long_roll_reversal_requires_rr_at_least_two(self):
        entries = long_entries(98.0)
        signal = self.detector.detect(self.contract, long_monitor(), entries, entries[-1])
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.direction, "up")
        self.assertGreaterEqual(signal.rr, 2.0)
        self.assertLess(signal.stop_loss_override, signal.candle.close)
        self.assertGreater(signal.take_profit_override, signal.candle.close)
        monitor = long_monitor()
        breakout_index = next(
            index for index, item in enumerate(monitor)
            if item.time_ms == signal.breakout_time_ms
        )
        expected_structural_stop = min(
            signal.breakout_level,
            min(item.low for item in monitor[breakout_index:]),
        )
        self.assertAlmostEqual(
            signal.stop_loss_override,
            expected_structural_stop - signal.atr_monitor * self.settings.atr_stop_buffer,
        )

    def test_short_roll_reversal_supports_rally_sell(self):
        entries = short_entries()
        signal = self.detector.detect(self.contract, short_monitor(), entries, entries[-1])
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.direction, "down")
        self.assertGreaterEqual(signal.rr, 2.0)
        self.assertGreater(signal.stop_loss_override, signal.candle.close)
        self.assertLess(signal.take_profit_override, signal.candle.close)
        monitor = short_monitor()
        breakout_index = next(
            index for index, item in enumerate(monitor)
            if item.time_ms == signal.breakout_time_ms
        )
        expected_structural_stop = max(
            signal.breakout_level,
            max(item.high for item in monitor[breakout_index:]),
        )
        self.assertAlmostEqual(
            signal.stop_loss_override,
            expected_structural_stop + signal.atr_monitor * self.settings.atr_stop_buffer,
        )

    def test_15m_noise_does_not_move_4h_stop(self):
        entries = long_entries(98.0)
        first = self.detector.detect(self.contract, long_monitor(), entries, entries[-1])
        self.assertIsNotNone(first)
        assert first is not None

        noisier_entries = list(entries)
        noisier_entries[-2] = replace(noisier_entries[-2], low=90.0)
        second = self.detector.detect(
            self.contract, long_monitor(), noisier_entries, noisier_entries[-1]
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.assertNotAlmostEqual(first.atr_entry, second.atr_entry)
        self.assertAlmostEqual(first.stop_loss_override, second.stop_loss_override)

    def test_same_roll_reversal_uses_same_persistent_alert_key(self):
        entries = long_entries(98.0)
        first = self.detector.detect(self.contract, long_monitor(), entries, entries[-1])
        self.assertIsNotNone(first)
        assert first is not None
        later_candle = replace(
            first.candle,
            time_ms=first.candle.time_ms + 15 * 60 * 1000,
            open=97.5,
            high=99.0,
            low=97.0,
            close=98.5,
        )
        second = replace(first, candle=later_candle)
        self.assertEqual(first.breakout_time_ms, second.breakout_time_ms)
        self.assertNotEqual(first.candle.time_ms, second.candle.time_ms)
        self.assertEqual(first.key, second.key)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = JsonStateStore(path)
            store.save_alert(first, 1_800_000_000_000)
            self.assertTrue(store.alert_exists(second.key))
            store.close()

            loaded = JsonStateStore(path)
            self.assertTrue(loaded.alert_exists(second.key))
            record = loaded.state["alerts"][first.key]
            self.assertEqual(record["breakout_time_ms"], first.breakout_time_ms)
            self.assertEqual(record["breakout_level"], first.breakout_level)

    def test_existing_pre_fix_alert_suppresses_same_current_setup(self):
        entries = long_entries(98.0)
        signal = self.detector.detect(self.contract, long_monitor(), entries, entries[-1])
        self.assertIsNotNone(signal)
        assert signal is not None
        legacy_signal = replace(
            signal,
            candle=replace(
                signal.candle,
                time_ms=signal.breakout_time_ms + 15 * 60 * 1000,
            ),
            strategy_name=None,
            breakout_time_ms=None,
        )
        self.assertNotEqual(signal.key, legacy_signal.key)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = JsonStateStore(path)
            store.save_alert(legacy_signal, 1_800_000_000_000)
            self.assertFalse(store.alert_exists(signal.key))
            self.assertTrue(store.setup_alert_exists(signal))

    def test_new_4h_breakout_gets_new_alert_key(self):
        entries = long_entries(98.0)
        first = self.detector.detect(self.contract, long_monitor(), entries, entries[-1])
        self.assertIsNotNone(first)
        assert first is not None
        second = replace(
            first,
            breakout_time_ms=first.breakout_time_ms + 4 * 60 * 60 * 1000,
        )
        self.assertNotEqual(first.key, second.key)

    def test_rr_below_two_is_filtered_out(self):
        entries = long_entries(102.0)
        signal = self.detector.detect(self.contract, long_monitor(), entries, entries[-1])
        self.assertIsNone(signal)

    def test_five_percent_position_size_uses_4h_structure_stop(self):
        entries = long_entries(98.0)
        signal = self.detector.detect(self.contract, long_monitor(), entries, entries[-1])
        self.assertIsNotNone(signal)
        assert signal is not None
        plan = build_risk_plan(signal, _manual_account_asset(self.settings), self.settings)
        self.assertAlmostEqual(plan.risk_budget, 50.0)
        self.assertLessEqual(plan.max_loss, 50.0)
        self.assertAlmostEqual(plan.stop_loss, signal.stop_loss_override)
        self.assertAlmostEqual(plan.tp_target, signal.take_profit_override)
        self.assertGreaterEqual(plan.tp_r_multiple, 2.0)

    def test_rr_three_or_more_is_equal_two_way_take_profit(self):
        entries = long_entries(97.0)
        signal = self.detector.detect(self.contract, long_monitor(), entries, entries[-1])
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertGreaterEqual(signal.rr, 3.0)
        self.assertEqual(len(signal.split_targets), 2)
        self.assertEqual(signal.split_targets[0][0], 0.5)
        self.assertEqual(signal.split_targets[1][0], 0.5)
        plan = build_risk_plan(signal, _manual_account_asset(self.settings), self.settings)
        message = format_signal(signal, "Asia/Tokyo", risk_plan=plan)
        self.assertIn("2分割（50% / 50%）", message)
        self.assertIn("RR: 1:", message)
        self.assertIn("4H→15M", message)


if __name__ == "__main__":
    unittest.main()
