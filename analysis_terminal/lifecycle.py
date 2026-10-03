"""Observed setup eligibility, independent of paper trade outcome/exit rules."""
from typing import Any

from analysis_terminal.setups import setup_identity

ENDED = {"EXPIRED", "INVALIDATED"}


def current_observation(row: dict[str, Any], now_ms: int, monitor_ms: int, entry_ms: int) -> bool:
    """Missing, delayed, or unfinished market bars cannot terminate a setup."""
    return (
        row.get("stage") not in {None, "DATA_WAIT"}
        and row.get("trend") in {"UP", "DOWN", "NEUTRAL"}
        and row.get("latest_4h_time_ms") == now_ms // monitor_ms * monitor_ms - monitor_ms
        and row.get("latest_15m_time_ms") == now_ms // entry_ms * entry_ms - entry_ms
        and isinstance(row.get("breakout_window_start_ms"), int)
        and (setup_identity(row) == row.get("setup_id"))
    )


def new_setup(row: dict[str, Any], observed_ms: int) -> dict[str, Any]:
    return {
        "setup_id": setup_identity(row), "ticker": row["ticker"], "direction": row["direction"],
        "breakout_time_ms": int(row["breakout_time_ms"]), "breakout_level": row["breakout_level"],
        "status": "OPEN", "first_seen_ms": observed_ms, "last_observed_ms": observed_ms,
        "last_stage": None, "first_ready_ms": None, "first_shadow_ready_ms": None,
        "market_observed_ms": None,
        "ended_ms": None, "end_reason": None, "end_evidence_4h_ms": None,
    }


def end_reason(setup: dict[str, Any], row: dict[str, Any]) -> tuple[str, str] | None:
    # Exactly the detector's last-N-bars search window, not a new timeout.
    if int(setup["breakout_time_ms"]) < int(row["breakout_window_start_ms"]):
        return "EXPIRED", "BREAKOUT_WINDOW_EXPIRED"
    if row.get("direction") != setup["direction"]:
        return "INVALIDATED", "TREND_INVALIDATED"
    if row.get("setup_id") != setup["setup_id"]:
        return "INVALIDATED", "SUPERSEDED" if row.get("setup_id") else "BREAKOUT_NOT_SELECTED"
    if row.get("stop_valid") is False:
        return "INVALIDATED", "STRUCTURAL_STOP_INVALID"
    # An invalid structural target does not invalidate measured-move room.
    return None


def observe_setup(setup: dict[str, Any], row: dict[str, Any], observed_ms: int) -> tuple[dict[str, Any], str | None]:
    if observed_ms < int(setup["last_observed_ms"]):
        return setup, None
    updated = dict(setup, last_observed_ms=observed_ms, market_observed_ms=observed_ms)
    ending = end_reason(setup, row)
    if ending:
        if setup["status"] in ENDED:
            return updated, None  # Preserve the first observed termination and its reason.
        updated.update(status=ending[0], end_reason=ending[1], ended_ms=observed_ms,
                       end_evidence_4h_ms=row["latest_4h_time_ms"])
        return updated, ending[1]
    updated["last_stage"] = row["stage"]
    if row.get("stage") == "READY" and updated["first_ready_ms"] is None:
        updated["first_ready_ms"] = observed_ms
    if row.get("shadow_v2_ready") and updated["first_shadow_ready_ms"] is None:
        updated["first_shadow_ready_ms"] = observed_ms
    updated["status"] = "READY" if updated["first_ready_ms"] is not None else "OPEN"
    updated.update(ended_ms=None, end_reason=None, end_evidence_4h_ms=None)
    # Existing eligibility may return; reopening is an observation, never a new entry.
    if setup["status"] in ENDED:
        reason = "REACTIVATED"
    elif setup["status"] != updated["status"]:
        reason = "READY_OBSERVED"
    elif setup["last_stage"] != updated["last_stage"]:
        reason = "STAGE_CHANGED"
    elif setup["first_shadow_ready_ms"] != updated["first_shadow_ready_ms"]:
        reason = "SHADOW_READY_OBSERVED"
    else:
        reason = None
    return updated, reason
