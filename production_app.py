"""Production entrypoint with the validated 4H trend-strength alert filter.

Keeps the core strategy implementation in app.py unchanged while applying the
current production notification rule: abs(EMA20-EMA50) >= 0.35 * 4H ATR.
Also persists filter-funnel diagnostics so quiet periods remain observable even
when live Actions logs are unavailable.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import app


MIN_TREND_GAP_ATR = float(os.getenv("EDGE_X_MIN_TREND_GAP_ATR", "0.35"))
FUNNEL_FILE = Path(os.getenv("EDGE_X_FUNNEL_FILE", "data/alert_funnel_latest.json"))
_original_detect = app.RollReversalDetector.detect
_original_format_signal = app.format_signal

_stats = {
    "detect_calls": 0,
    "core_signals": 0,
    "trend_missing": 0,
    "trend_rejected": 0,
    "final_signals": 0,
}


def _persist_funnel() -> None:
    try:
        FUNNEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "min_trend_gap_atr": MIN_TREND_GAP_ATR,
            **_stats,
        }
        temp = FUNNEL_FILE.with_suffix(FUNNEL_FILE.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(FUNNEL_FILE)
    except Exception:
        logging.getLogger(__name__).exception("Failed to persist alert funnel diagnostics")


atexit.register(_persist_funnel)


def _log_funnel(contract_symbol: str, outcome: str, trend_gap_atr: float | None = None) -> None:
    gap = "n/a" if trend_gap_atr is None else f"{trend_gap_atr:.3f}"
    logging.getLogger(__name__).info(
        "ALERT_FUNNEL contract=%s outcome=%s trend_gap_atr=%s threshold=%.2f "
        "detect_calls=%d core_signals=%d trend_missing=%d trend_rejected=%d final_signals=%d",
        contract_symbol,
        outcome,
        gap,
        MIN_TREND_GAP_ATR,
        _stats["detect_calls"],
        _stats["core_signals"],
        _stats["trend_missing"],
        _stats["trend_rejected"],
        _stats["final_signals"],
    )
    _persist_funnel()


def _filtered_detect(self, contract, monitor_candles, entry_candles, candidate):
    _stats["detect_calls"] += 1
    signal = _original_detect(self, contract, monitor_candles, entry_candles, candidate)
    symbol = str(getattr(contract, "symbol", None) or getattr(contract, "contract_name", None) or getattr(contract, "contract_id", "unknown"))
    if signal is None:
        return None

    _stats["core_signals"] += 1
    if signal.ema_fast is None or signal.ema_slow is None or signal.atr_monitor is None or signal.atr_monitor <= 0:
        _stats["trend_missing"] += 1
        _log_funnel(symbol, "trend_data_missing")
        return None

    trend_gap_atr = abs(signal.ema_fast - signal.ema_slow) / signal.atr_monitor
    if trend_gap_atr < MIN_TREND_GAP_ATR:
        _stats["trend_rejected"] += 1
        _log_funnel(symbol, "trend_rejected", trend_gap_atr)
        return None

    _stats["final_signals"] += 1
    _log_funnel(symbol, "final_signal", trend_gap_atr)
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
    logging.getLogger(__name__).info("ALERT_FUNNEL enabled threshold=%.2f file=%s", MIN_TREND_GAP_ATR, FUNNEL_FILE)
    _persist_funnel()
    app.main()
