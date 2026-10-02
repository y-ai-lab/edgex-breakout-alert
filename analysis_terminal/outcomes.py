"""Chronological, closed-candle evaluation shared by server signal trackers."""
from __future__ import annotations

import math
from typing import Any

EVALUATION_VERSION = 2
TERMINAL_STATUSES = {"TP", "SL", "AMBIGUOUS"}


def verified_result(result: dict[str, Any] | None) -> bool:
    return bool(
        result
        and result.get("evaluation_version") == EVALUATION_VERSION
        and result.get("coverage_complete") is True
    )


def evaluate_paper_signal(
    signal: dict[str, Any], candles: list[Any], *, interval_ms: int, now_ms: int
) -> dict[str, Any]:
    prior = signal.get("result") or {}
    # Preserve historical terminal results; they need a separate full-history audit.
    if prior.get("status") in TERMINAL_STATUSES:
        return prior
    legacy = prior.get("legacy_result")
    if prior and prior.get("evaluation_version") != EVALUATION_VERSION:
        legacy, prior = prior, {}

    created_ms = int(signal["created_ms"])
    source_ms = signal.get("signal_candle_ms")
    if source_ms is None:
        source_ms = signal.get("source_candle_ms")
    source_close = int(source_ms) + interval_ms if source_ms is not None else None
    # +1ms is an exclusion sentinel, not a reason to discard the next whole bar.
    if source_close is not None and created_ms == source_close + 1:
        start_ms, partial = source_close, False
    else:
        start_ms = ((created_ms + interval_ms - 1) // interval_ms) * interval_ms
        if source_close is not None:
            start_ms = max(start_ms, source_close)
        partial = start_ms != created_ms

    entries = sorted(
        {c.time_ms: c for c in candles if c.time_ms + interval_ms <= now_ms}.values(),
        key=lambda c: c.time_ms,
    )
    prior_end = prior.get("history_end_ms")
    expected_ms = max(start_ms, int(prior_end) + interval_ms) if prior_end is not None else start_ms
    relevant = [c for c in entries if c.time_ms >= expected_ms]
    coverage = not partial and (
        prior.get("coverage_complete") is True if prior_end is not None else True
    )
    result = {
        "status": prior.get("status") or "OPEN",
        "final_r": prior.get("final_r"),
        "mfe_r": float(prior.get("mfe_r") or 0),
        "mae_r": float(prior.get("mae_r") or 0),
        "tp1_time_ms": prior.get("tp1_time_ms"),
        "outcome_time_ms": prior.get("outcome_time_ms"),
        "ambiguous_reason": prior.get("ambiguous_reason"),
        "last_price": prior.get("last_price"),
        "coverage_complete": coverage,
        "history_start_ms": prior.get("history_start_ms"),
        "history_end_ms": prior_end,
        "candles_checked": int(prior.get("candles_checked") or 0),
        "evaluation_version": EVALUATION_VERSION,
        "observation_start_ms": start_ms,
        "partial_entry_candle_excluded": partial,
    }
    if legacy:
        result["legacy_result"] = legacy
    if not relevant:
        # No cursor before entry: a later refresh must still start at the first bar.
        result["coverage_complete"] = coverage if prior_end is not None else False
        return result

    side = str(signal["side"]).upper()
    entry, stop, target = (float(signal[k]) for k in ("entry", "stop", "target"))
    risk = abs(entry - stop)
    valid = stop < entry < target if side == "LONG" else target < entry < stop if side == "SHORT" else False
    if not valid or not all(math.isfinite(x) for x in (entry, stop, target, risk)) or risk <= 0:
        result.update(status="ERROR", error="invalid trade levels", coverage_complete=False)
        return result
    tp1 = float(signal["tp1"]) if signal.get("tp1") is not None else None

    for candle in relevant:
        if candle.time_ms != expected_ms:
            coverage = False
        expected_ms = candle.time_ms + interval_ms
        if result["history_start_ms"] is None:
            result["history_start_ms"] = candle.time_ms
        result["history_end_ms"] = candle.time_ms
        result["candles_checked"] += 1
        result["last_price"] = candle.close
        if side == "LONG":
            favorable, adverse = (candle.high - entry) / risk, (entry - candle.low) / risk
            stop_hit, target_hit = candle.low <= stop, candle.high >= target
            tp1_hit = tp1 is not None and candle.high >= tp1
        else:
            favorable, adverse = (entry - candle.low) / risk, (candle.high - entry) / risk
            stop_hit, target_hit = candle.high >= stop, candle.low <= target
            tp1_hit = tp1 is not None and candle.low <= tp1
        # Terminal-bar extremes are OHLC bounds; no intrabar order is inferred.
        result["mfe_r"] = max(result["mfe_r"], favorable, 0.0)
        result["mae_r"] = max(result["mae_r"], adverse, 0.0)
        if tp1_hit and result["tp1_time_ms"] is None:
            result["tp1_time_ms"] = candle.time_ms
        if stop_hit and (target_hit or (tp1_hit and result["tp1_time_ms"] == candle.time_ms)):
            result.update(
                status="AMBIGUOUS", final_r=None, outcome_time_ms=candle.time_ms,
                ambiguous_reason="SL and TP touched in the same 15M candle",
            )
            break
        if stop_hit or target_hit:
            result.update(
                status="SL" if stop_hit else "TP",
                final_r=-1.0 if stop_hit else abs(target - entry) / risk,
                outcome_time_ms=candle.time_ms,
            )
            break

    if result["status"] == "OPEN" and result["tp1_time_ms"] is not None:
        result["status"] = "TP1"
    result["coverage_complete"] = coverage
    for key in ("final_r", "mfe_r", "mae_r"):
        if result[key] is not None:
            result[key] = round(result[key], 4)
    return result
