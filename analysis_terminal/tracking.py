"""Outcome collection health, independent of entry rules and promotion gates."""
from collections import Counter
from typing import Any

from analysis_terminal.outcomes import EVALUATION_VERSION, TERMINAL_STATUSES, verified_result


def tracking_state(signal: dict[str, Any], *, interval_ms: int, now_ms: int) -> dict[str, Any]:
    result = signal.get("result") or {}
    terminal = result.get("status") in TERMINAL_STATUSES
    item = dict(key=signal.get("key"), setup_id=signal.get("setup_id"), ticker=signal.get("ticker"),
                status=result.get("status") or "OPEN", terminal=terminal,
                quality=None, coverage_complete=verified_result(result), next_expected_ms=None,
                history_end_ms=result.get("history_end_ms"), bars_due=0, overdue=False, age_hours=None)
    try:
        created = int(signal["created_ms"])
        if created <= 0 or interval_ms <= 0:
            raise ValueError("invalid observation time")
        item["age_hours"] = round(max(0, now_ms-created)/3_600_000, 2)
        if terminal:
            item["quality"] = "VERIFIED_TERMINAL" if verified_result(result) else "UNVERIFIED_TERMINAL"
            return item
        source = signal.get("signal_candle_ms")
        if source is None:
            source = signal.get("source_candle_ms")
        source_close = int(source)+interval_ms if source is not None else None
        if source_close is not None and created == source_close+1:
            start = source_close
        else:
            start = ((created+interval_ms-1)//interval_ms)*interval_ms
            if source_close is not None:
                start = max(start, source_close)
        current_evaluation = result.get("evaluation_version") == EVALUATION_VERSION
        end = result.get("history_end_ms") if current_evaluation else None
        if end is not None:
            end = int(end)
            if end < start or end % interval_ms or end+interval_ms > now_ms:
                raise ValueError("invalid evaluation cursor")
        next_ms = max(start, end+interval_ms) if end is not None else start
        latest_closed = (now_ms//interval_ms)*interval_ms-interval_ms
        item["next_expected_ms"] = next_ms
        item["bars_due"] = max(0, (latest_closed-next_ms)//interval_ms+1)
        # One newly closed bar is normal collector latency; two bars need review.
        item["overdue"] = item["bars_due"] >= 2
        if result.get("status") == "ERROR":
            item["quality"] = "DATA_ERROR"
        elif end is not None:
            item["quality"] = "TRACKING" if verified_result(result) else "INCOMPLETE_HISTORY"
        elif not item["bars_due"]:
            item["quality"] = "WAITING_FIRST_CLOSED_CANDLE"
        else:
            item["quality"] = "UNOBSERVED" if not result or current_evaluation else "LEGACY_PENDING"
    except (KeyError, TypeError, ValueError, OverflowError):
        item["quality"] = "DATA_ERROR"
    return item


def tracking_summary(signals: list[dict[str, Any]], *, interval_ms: int, now_ms: int,
                     limit: int = 50) -> dict[str, Any]:
    states = [tracking_state(s, interval_ms=interval_ms, now_ms=now_ms) for s in signals]
    pending = [s for s in states if not s["terminal"]]
    pending.sort(key=lambda s: (-int(s["overdue"]), -s["bars_due"], str(s["key"] or "")))
    return dict(records=len(states), pending=len(pending),
                quality_counts=dict(sorted(Counter(s["quality"] for s in states).items())),
                overdue=sum(s["overdue"] for s in pending),
                incomplete_history=sum(s["quality"] == "INCOMPLETE_HISTORY" for s in pending),
                max_bars_due=max((s["bars_due"] for s in pending), default=0),
                oldest_pending_hours=max((s["age_hours"] for s in pending if s["age_hours"] is not None), default=None),
                pending_items=pending[:limit])
