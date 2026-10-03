"""Reconcile saved live signals against public candles without seeding saved outcomes.

No database, notification, collector or promotion writes. Audits are evidence at
the saved cursor, not additional signals or resolved promotion samples.
"""
from collections import Counter
from copy import deepcopy
import math

import app as scanner
from analysis_terminal.outcome_history import consecutive_window
from analysis_terminal.outcomes import evaluate_paper_signal, verified_result
from analysis_terminal.setups import setup_identity

DATASET = "LIVE_RECORD_RECONCILIATION"
FIELDS = ("status", "final_r", "mfe_r", "mae_r", "tp1_time_ms", "outcome_time_ms",
          "last_price", "coverage_complete", "history_start_ms", "history_end_ms",
          "candles_checked", "evaluation_version", "observation_start_ms",
          "partial_entry_candle_excluded", "ambiguous_reason")


def audit_signal(signal, candles, contract, *, interval="MINUTE_15"):
    saved = deepcopy(signal.get("result") or {})
    row = dict(key=signal.get("key"), setup_id=signal.get("setup_id"), ticker=signal.get("ticker"),
               side=signal.get("side"), created_ms=signal.get("created_ms"), status=None,
               saved_status=saved.get("status"), saved_history_end_ms=saved.get("history_end_ms"),
               differences=[], first_gap_ms=None, recomputed=None)
    step = scanner.INTERVAL_MS[interval]
    identity = setup_identity(dict(signal, direction=signal.get("side")))
    if not identity or identity != signal.get("setup_id") or contract.contract_name != signal.get("ticker"):
        row["status"] = "INVALID_IDENTITY"
        return row
    try:
        source_ms = signal.get("signal_candle_ms")
        source = int(source_ms if source_ms is not None else signal["source_candle_ms"])
        created = int(signal["created_ms"])
        end = int(saved["history_end_ms"])
        if source % step or created != source + step + 1 or end < source + step or end % step:
            raise ValueError("Invalid signal/cursor chronology")
    except (KeyError, TypeError, ValueError, OverflowError):
        row["status"] = "NO_SAVED_HISTORY" if saved.get("history_end_ms") is None else "INVALID_CHRONOLOGY"
        return row
    # Do not pass prior results: the production evaluator preserves terminals
    # and carries OPEN cursors, which would defeat full-history reconciliation.
    fresh = {key: value for key, value in signal.items() if key != "result"}
    try:
        prefix, gap, _ = consecutive_window(fresh, candles, contract, interval, now_ms=end + step)
        recomputed = evaluate_paper_signal(fresh, prefix, interval_ms=step, now_ms=end + step)
    except (KeyError, TypeError, ValueError, OverflowError):
        row["status"] = "INVALID_CANDLES"
        return row
    row.update(first_gap_ms=gap, recomputed=recomputed)
    if gap is not None and recomputed["status"] not in {"TP", "SL", "AMBIGUOUS"}:
        row["status"] = "HISTORY_GAP"
        return row
    if not verified_result(saved):
        row["status"] = "UNVERIFIED_SAVED_RESULT"
        return row

    def equal(a, b):
        if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
            return math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
        return a == b

    row["differences"] = [key for key in FIELDS if not equal(saved.get(key), recomputed.get(key))]
    row["status"] = "MISMATCH" if row["differences"] else "MATCH"
    return row


def audit_summary(rows):
    counts = dict(sorted(Counter(row["status"] for row in rows).items()))
    return dict(records=len(rows), matched=counts.get("MATCH", 0),
                mismatches=counts.get("MISMATCH", 0),
                incomplete_or_invalid=len(rows)-counts.get("MATCH", 0)-counts.get("MISMATCH", 0),
                status_counts=counts)
