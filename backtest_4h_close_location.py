#!/usr/bin/env python3
"""Run the existing 4H backtest with one additional, predeclared filter.

Close-location rule:
- LONG breakout candle must close in the upper 25% of its 4H range.
- SHORT breakout candle must close in the lower 25% of its 4H range.

The underlying strategy, exits, risk, RR and cost model remain unchanged.
"""

import asyncio
import inspect

import backtest_4h_breakout_strength as base


class CloseLocationDetector(base.RollReversalDetector):
    """Existing detector plus a fixed 0.75/0.25 breakout close-location filter."""

    def detect(self, contract, monitor_history, entry_history, candidate):
        signal = super().detect(contract, monitor_history, entry_history, candidate)
        if signal is None:
            return None

        breakout = next(
            (c for c in monitor_history if c.time_ms == signal.breakout_time_ms),
            None,
        )
        if breakout is None:
            return None

        candle_range = float(breakout.high) - float(breakout.low)
        if candle_range <= 0:
            return None

        close_location = (float(breakout.close) - float(breakout.low)) / candle_range
        if signal.direction == "up" and close_location < 0.75:
            return None
        if signal.direction == "down" and close_location > 0.25:
            return None
        return signal


# Reuse the exact existing backtest engine and cost analysis, but compare
# baseline against the single fixed close-location rule.
base.RollReversalDetector = CloseLocationDetector
base.VARIANTS = {"baseline": None, "close_location_075": None}


if __name__ == "__main__":
    main = getattr(base, "main")
    result = main()
    if inspect.isawaitable(result):
        asyncio.run(result)
