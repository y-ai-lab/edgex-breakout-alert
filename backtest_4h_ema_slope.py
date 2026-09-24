#!/usr/bin/env python3
from __future__ import annotations

import backtest_4h_exit as bt

SLOPE_LAG_BARS = 3  # 12 hours on the 4H monitor timeframe
FILTER_NAME = "ema20_slope_3x4h"


def ema(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    seed = sum(values[:period]) / period
    multiplier = 2.0 / (period + 1.0)
    current = seed
    for value in values[period:]:
        current = (value - current) * multiplier + current
    return current


class EmaSlopeFilteredDetector(bt.RollReversalDetector):
    """Existing 4H roll-reversal rules + a fixed 3-bar EMA20 slope filter."""

    def detect(self, contract, monitor_candles, entry_candles, candidate):
        monitor = sorted(monitor_candles, key=lambda item: item.time_ms)
        signal = super().detect(contract, monitor, entry_candles, candidate)
        if signal is None:
            return None

        if len(monitor) <= SLOPE_LAG_BARS:
            return None

        closes_now = [c.close for c in monitor]
        closes_then = closes_now[:-SLOPE_LAG_BARS]
        ema_now = ema(closes_now, self.settings.trend_fast_ema)
        ema_then = ema(closes_then, self.settings.trend_fast_ema)
        if ema_now is None or ema_then is None:
            return None

        if signal.direction == "up" and ema_now <= ema_then:
            return None
        if signal.direction == "down" and ema_now >= ema_then:
            return None
        return signal


# backtest_4h_exit.backtest_contract instantiates the module-level detector.
bt.RollReversalDetector = EmaSlopeFilteredDetector

if __name__ == "__main__":
    bt.main()
