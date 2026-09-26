"""Production entrypoint with the validated 4H trend-strength alert filter.

Keeps the core strategy implementation in app.py unchanged while applying the
current production notification rule: abs(EMA20-EMA50) >= 0.35 * 4H ATR.
"""

from __future__ import annotations

import os

import app


MIN_TREND_GAP_ATR = float(os.getenv("EDGE_X_MIN_TREND_GAP_ATR", "0.35"))
_original_detect = app.RollReversalDetector.detect
_original_format_signal = app.format_signal


def _filtered_detect(self, contract, monitor_candles, entry_candles, candidate):
    signal = _original_detect(self, contract, monitor_candles, entry_candles, candidate)
    if signal is None:
        return None
    if signal.ema_fast is None or signal.ema_slow is None or signal.atr_monitor is None:
        return None
    if signal.atr_monitor <= 0:
        return None
    trend_gap_atr = abs(signal.ema_fast - signal.ema_slow) / signal.atr_monitor
    if trend_gap_atr < MIN_TREND_GAP_ATR:
        return None
    return signal


def _format_signal_with_filter(signal, timezone_name, risk_plan=None, risk_note=None):
    text = _original_format_signal(signal, timezone_name, risk_plan, risk_note)
    if not signal.strategy_name:
        return text
    if signal.ema_fast is None or signal.ema_slow is None or signal.atr_monitor in (None, 0):
        return text
    trend_gap_atr = abs(signal.ema_fast - signal.ema_slow) / signal.atr_monitor
    marker = f"4Hトレンド強度: {trend_gap_atr:.2f} ATR（通知基準 >= {MIN_TREND_GAP_ATR:.2f} ATR）"
    lines = text.splitlines()
    insert_at = next((i + 1 for i, line in enumerate(lines) if line.startswith("4Hトレンド:")), 6)
    lines.insert(insert_at, marker)
    return "\n".join(lines)


app.RollReversalDetector.detect = _filtered_detect
app.format_signal = _format_signal_with_filter


if __name__ == "__main__":
    app.main()
