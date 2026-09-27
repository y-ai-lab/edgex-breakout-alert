"""Production alert entrypoint plus persistent funnel diagnostics."""
from __future__ import annotations

import atexit
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import app

MIN_TREND_GAP_ATR = float(os.getenv("EDGE_X_MIN_TREND_GAP_ATR", "0.35"))
FUNNEL_FILE = Path(os.getenv("EDGE_X_FUNNEL_FILE", "data/alert_funnel_latest.json"))
_original_detect = app.RollReversalDetector.detect
_original_format_signal = app.format_signal

_stats = {
    "detect_calls": 0,
    "core_signals": 0,
    "reject_input_or_history": 0,
    "reject_indicator": 0,
    "reject_trend_direction": 0,
    "reject_recent_breakout": 0,
    "reject_retest_or_confirmation": 0,
    "reject_structure_target": 0,
    "reject_min_rr": 0,
    "reject_unclassified": 0,
    "trend_missing": 0,
    "trend_rejected": 0,
    "final_signals": 0,
}


def _persist_funnel() -> None:
    try:
        FUNNEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at_utc": datetime.now(timezone.utc).isoformat(), "min_trend_gap_atr": MIN_TREND_GAP_ATR, **_stats}
        temp = FUNNEL_FILE.with_suffix(FUNNEL_FILE.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(FUNNEL_FILE)
    except Exception:
        logging.getLogger(__name__).exception("Failed to persist alert funnel diagnostics")


atexit.register(_persist_funnel)


def _classify_core_rejection(self, monitor_candles, entry_candles, candidate) -> str:
    """Mirror core gates only to identify the first failed stage; never creates a signal."""
    try:
        monitor = sorted(monitor_candles, key=lambda x: x.time_ms)
        entries = sorted((c for c in entry_candles if c.time_ms <= candidate.time_ms), key=lambda x: x.time_ms)
        if not monitor or not entries or entries[-1].time_ms != candidate.time_ms:
            return "reject_input_or_history"
        required_monitor = max(self.settings.trend_slow_ema, self.settings.roll_lookback + self.settings.roll_max_age, self.settings.atr_period + 1)
        required_entry = max(self.settings.atr_period + 1, self.settings.retest_lookback, self.settings.pullback_swing_lookback)
        if len(monitor) < required_monitor or len(entries) < required_entry:
            return "reject_input_or_history"
        closes = [c.close for c in monitor]
        ema_fast = app._ema(closes, self.settings.trend_fast_ema)
        ema_slow = app._ema(closes, self.settings.trend_slow_ema)
        atr_monitor = app._atr(monitor, self.settings.atr_period)
        atr_entry = app._atr(entries, self.settings.atr_period)
        if None in {ema_fast, ema_slow, atr_monitor, atr_entry} or atr_monitor <= 0 or atr_entry <= 0:
            return "reject_indicator"
        latest = monitor[-1]
        if ema_fast > ema_slow and latest.close > ema_fast:
            direction = "up"
        elif ema_fast < ema_slow and latest.close < ema_fast:
            direction = "down"
        else:
            return "reject_trend_direction"
        breakout = self._recent_breakout(monitor, direction)
        if breakout is None:
            return "reject_recent_breakout"
        breakout_index, roll_level = breakout
        tolerance = max(atr_entry * self.settings.retest_atr_tolerance, roll_level * 0.001)
        retest_window = entries[-self.settings.retest_lookback:]
        if direction == "up":
            touched = any(c.low <= roll_level + tolerance for c in retest_window)
            confirmed = candidate.close > candidate.open and candidate.close > roll_level
        else:
            touched = any(c.high >= roll_level - tolerance for c in retest_window)
            confirmed = candidate.close < candidate.open and candidate.close < roll_level
        if not touched or not confirmed:
            return "reject_retest_or_confirmation"
        structure = monitor[breakout_index:]
        if direction == "up":
            structural_stop = min(roll_level, min(c.low for c in structure))
            stop = structural_stop - atr_monitor * self.settings.atr_stop_buffer
            raw_target = max(c.high for c in structure)
            target = raw_target - atr_monitor * self.settings.atr_target_buffer
            if stop >= candidate.close or target <= candidate.close:
                return "reject_structure_target"
            rr = (target - candidate.close) / (candidate.close - stop)
        else:
            structural_stop = max(roll_level, max(c.high for c in structure))
            stop = structural_stop + atr_monitor * self.settings.atr_stop_buffer
            raw_target = min(c.low for c in structure)
            target = raw_target + atr_monitor * self.settings.atr_target_buffer
            if stop <= candidate.close or target >= candidate.close:
                return "reject_structure_target"
            rr = (candidate.close - target) / (stop - candidate.close)
        if rr < self.settings.min_rr:
            return "reject_min_rr"
    except Exception:
        logging.getLogger(__name__).exception("Core rejection diagnosis failed")
    return "reject_unclassified"


def _filtered_detect(self, contract, monitor_candles, entry_candles, candidate):
    _stats["detect_calls"] += 1
    monitor_candles = list(monitor_candles)
    entry_candles = list(entry_candles)
    signal = _original_detect(self, contract, monitor_candles, entry_candles, candidate)
    if signal is None:
        stage = _classify_core_rejection(self, monitor_candles, entry_candles, candidate)
        _stats[stage] += 1
        _persist_funnel()
        return None

    _stats["core_signals"] += 1
    if signal.ema_fast is None or signal.ema_slow is None or signal.atr_monitor is None or signal.atr_monitor <= 0:
        _stats["trend_missing"] += 1
        _persist_funnel()
        return None
    trend_gap_atr = abs(signal.ema_fast - signal.ema_slow) / signal.atr_monitor
    if trend_gap_atr < MIN_TREND_GAP_ATR:
        _stats["trend_rejected"] += 1
        _persist_funnel()
        return None
    _stats["final_signals"] += 1
    _persist_funnel()
    return signal


def _format_signal_with_filter(signal, timezone_name, risk_plan=None, risk_note=None):
    text = _original_format_signal(signal, timezone_name, risk_plan, risk_note)
    if not signal.strategy_name or signal.ema_fast is None or signal.ema_slow is None or signal.atr_monitor in (None, 0):
        return text
    gap = abs(signal.ema_fast - signal.ema_slow) / signal.atr_monitor
    marker = f"4Hトレンド強度: {gap:.2f} ATR（通知基準 >= {MIN_TREND_GAP_ATR:.2f} ATR）"
    lines = text.splitlines()
    insert_at = next((i + 1 for i, line in enumerate(lines) if line.startswith("4Hトレンド:")), 6)
    lines.insert(insert_at, marker)
    return "\n".join(lines)


app.RollReversalDetector.detect = _filtered_detect
app.format_signal = _format_signal_with_filter

if __name__ == "__main__":
    logging.getLogger(__name__).info("ALERT_FUNNEL core-stage diagnostics enabled threshold=%.2f file=%s", MIN_TREND_GAP_ATR, FUNNEL_FILE)
    _persist_funnel()
    app.main()
