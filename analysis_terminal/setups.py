"""Stable identities for observed 4H breakout setups, without legacy inference."""
from decimal import Decimal, InvalidOperation
from typing import Any


def setup_identity(row: dict[str, Any]) -> str | None:
    ticker = str(row.get("ticker") or "").strip()
    direction = row.get("direction")
    try:
        timestamp = int(row["breakout_time_ms"])
        level = Decimal(str(row["breakout_level"]))
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        return None
    if not ticker or direction not in {"LONG", "SHORT"}:
        return None
    if timestamp <= 0 or not level.is_finite() or level <= 0:
        return None
    # Decimal avoids float display variations such as 100 / 100.0 / 1e2.
    normalized = format(level, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return f"setup-v1:{ticker}:{direction}:{timestamp}:{normalized}"


def first_per_setup(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use the first recorded entry, including when a duplicate resolves sooner."""
    found: dict[str, dict[str, Any]] = {}
    for signal in sorted(signals, key=lambda item: int(item.get("created_ms") or 0)):
        if signal.get("setup_id"):
            found.setdefault(str(signal["setup_id"]), signal)
    return list(found.values())


def ready_times_by_setup(events: list[dict[str, Any]]) -> dict[str, list[int]]:
    times: dict[str, list[int]] = {}
    for event in events:
        if event.get("kind") == "READY" and event.get("setup_id"):
            times.setdefault(str(event["setup_id"]), []).append(int(event["created_ms"]))
    return {key: sorted(values) for key, values in times.items()}


def later_ready_time(event: dict[str, Any], times: dict[str, list[int]], end_ms: int) -> int | None:
    if not event.get("setup_id"):
        return None
    start = int(event["created_ms"])
    return next((value for value in times.get(str(event["setup_id"]), []) if start < value <= end_ms), None)
