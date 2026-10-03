"""Select consecutive closed bars; missing observations never imply a trade result."""
import math
from typing import Any

import app as scanner
from analysis_terminal.tracking import tracking_state

BACKFILL_BARS = 256
BACKFILL_REQUESTS = 4


def merge_candles(*groups: list[scanner.Candle]) -> list[scanner.Candle]:
    def prices(candle):
        # WS and REST may omit different auxiliary metadata. Outcomes depend on
        # the same contract/interval/time and OHLC, never volume or trade counts.
        return (candle.contract_id, candle.interval, candle.time_ms,
                candle.open, candle.high, candle.low, candle.close)
    found = {}
    for group in groups:
        for candle in group:
            old = found.get(candle.time_ms)
            if old is not None and prices(old) != prices(candle):
                raise ValueError("Conflicting closed candle revisions")
            if old is None:
                found[candle.time_ms] = candle
    return sorted(found.values(), key=lambda candle: candle.time_ms)


def consecutive_window(signal: dict[str, Any], candles: list[scanner.Candle],
                       contract: scanner.Contract, interval: str, *, now_ms: int
                       ) -> tuple[list[scanner.Candle], int | None, int]:
    step = scanner.INTERVAL_MS[interval]
    state = tracking_state(signal, interval_ms=step, now_ms=now_ms)
    if state["terminal"] or state["quality"] == "DATA_ERROR":
        raise ValueError("Invalid pending observation cursor")
    expected = state["next_expected_ms"]
    end = now_ms // step * step
    closed = [c for c in candles if expected <= c.time_ms and c.time_ms + step <= now_ms]
    prefix = []
    for candle in merge_candles(closed):
        if candle.contract_id != contract.contract_id or candle.interval != interval or candle.time_ms % step:
            raise ValueError("Invalid outcome candle identity or time grid")
        if not all(math.isfinite(v) for v in (candle.open, candle.high, candle.low, candle.close)):
            raise ValueError("Invalid outcome candle price")
        if candle.low > min(candle.open, candle.close) or candle.high < max(candle.open, candle.close):
            raise ValueError("Invalid outcome candle OHLC")
        if candle.time_ms != expected:
            break
        prefix.append(candle)
        expected += step
    return prefix, expected if expected < end else None, end
