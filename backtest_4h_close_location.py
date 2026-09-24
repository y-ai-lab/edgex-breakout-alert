#!/usr/bin/env python3
"""Run the existing 4H backtest with one additional, predeclared filter.

Close-location rule:
- LONG breakout candle must close in the upper 25% of its 4H range.
- SHORT breakout candle must close in the lower 25% of its 4H range.

The baseline remains completely unchanged. The filter is applied only to the
close_location_075 variant after the common signal has been generated.
"""

import asyncio
import inspect

import backtest_4h_breakout_strength as base

base.VARIANTS = {"baseline": None, "close_location_075": None}


def close_location_accepts(signal, monitor_history):
    breakout = next(
        (c for c in monitor_history if c.time_ms == signal.breakout_time_ms),
        None,
    )
    if breakout is None:
        return False

    candle_range = float(breakout.high) - float(breakout.low)
    if candle_range <= 0:
        return False

    close_location = (float(breakout.close) - float(breakout.low)) / candle_range
    if signal.direction == "up":
        return close_location >= 0.75
    return close_location <= 0.25


_original_backtest_contract = base.backtest_contract


def backtest_contract(contract, settings, period_start_ms, period_end_ms, holdout_start_ms):
    """Run the exact baseline engine, then apply the filter only to variant 2."""
    monitor_begin = period_start_ms - 14 * base.DAY_MS
    entry_begin = period_start_ms - 2 * base.DAY_MS
    monitor = base.fetch_klines(
        contract, base.MONITOR_INTERVAL, monitor_begin, period_end_ms, base.MONITOR_CHUNK_MS
    )
    entries = base.fetch_klines(
        contract, base.ENTRY_INTERVAL, entry_begin, period_end_ms, base.ENTRY_CHUNK_MS
    )

    detector = base.RollReversalDetector(settings)
    full_trades = {name: [] for name in base.VARIANTS}
    pre_holdout_trades = {name: [] for name in base.VARIANTS}
    monitor_end = 0
    seen_setup_keys = {name: set() for name in base.VARIANTS}

    for i, candidate in enumerate(entries):
        candidate_close_ms = candidate.time_ms + base.ENTRY_MS
        if candidate.time_ms < period_start_ms or candidate_close_ms > period_end_ms:
            continue

        while (
            monitor_end < len(monitor)
            and monitor[monitor_end].time_ms + base.MONITOR_MS <= candidate_close_ms
        ):
            monitor_end += 1

        monitor_history = monitor[max(0, monitor_end - settings.history_size) : monitor_end]
        entry_history = entries[max(0, i - settings.history_size + 1) : i + 1]
        signal = detector.detect(contract, monitor_history, entry_history, candidate)
        if signal is None:
            continue

        for variant in base.VARIANTS:
            if variant == "close_location_075" and not close_location_accepts(signal, monitor_history):
                continue
            if signal.key in seen_setup_keys[variant]:
                continue
            seen_setup_keys[variant].add(signal.key)

            trade = base.simulate_trade(signal, entries, i, period_end_ms)
            trade["variant"] = variant
            full_trades[variant].append(trade)
            if candidate_close_ms < holdout_start_ms:
                pre_trade = base.simulate_trade(signal, entries, i, holdout_start_ms)
                pre_trade["variant"] = variant
                pre_holdout_trades[variant].append(pre_trade)

    coverage = {
        "symbol": contract.contract_name,
        "contract_id": contract.contract_id,
        "monitor_bars": len(monitor),
        "entry_bars": len(entries),
        "first_entry_bar_ms": entries[0].time_ms if entries else None,
        "last_entry_bar_ms": entries[-1].time_ms if entries else None,
        "signals_180d": {name: len(rows) for name, rows in full_trades.items()},
        "signals_pre_holdout": {name: len(rows) for name, rows in pre_holdout_trades.items()},
    }
    return full_trades, pre_holdout_trades, coverage


base.backtest_contract = backtest_contract


if __name__ == "__main__":
    result = base.main()
    if inspect.isawaitable(result):
        asyncio.run(result)
