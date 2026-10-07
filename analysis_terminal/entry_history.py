"""Persistent production candidate snapshots; independent of delivery and outcomes."""
import json
import math

from analysis_terminal.lifecycle import current_observation
from analysis_terminal.outcomes import verified_result
from analysis_terminal.setups import setup_identity

FIELDS = ("ticker", "direction", "stage", "reason", "rr", "score",
          "entry_reference", "stop_loss", "take_profit", "confirmation_color_ok",
          "confirmation_level_ok", "breakout_time_ms", "breakout_level",
          "latest_15m_time_ms", "latest_4h_time_ms")


def snapshot(row):
    return {key: (None if isinstance(row.get(key), float) and not math.isfinite(row[key])
                  else row.get(key)) for key in FIELDS}


def kind(row, min_rr):
    if row.get("stage") == "READY":
        return "READY"
    rr = row.get("rr")
    if (row.get("stage") in {"CONFIRMATION_WAIT", "RETEST_WAIT"}
            and isinstance(rr, (int, float)) and math.isfinite(rr) and rr >= min_rr):
        return "NEAR"
    return None


def _record(conn, row, observed_ms, candidate_kind):
    identity = row.get("setup_id")
    stored = conn.execute("SELECT payload FROM entry_candidate_history WHERE setup_id=?",
                          (identity,)).fetchone()
    if stored is None and candidate_kind is None:
        return
    item = json.loads(stored["payload"]) if stored else dict(
        setup_id=identity, ticker=row["ticker"], direction=row["direction"],
        first_near=None, first_ready=None, last_observed_ms=0, latest=None)
    frozen = dict(observed_ms=observed_ms, **snapshot(row))
    if candidate_kind:
        field = "first_ready" if candidate_kind == "READY" else "first_near"
        if item[field] is None or observed_ms < item[field]["observed_ms"]:
            item[field] = frozen
    if observed_ms >= item["last_observed_ms"]:
        item.update(last_observed_ms=observed_ms, latest=frozen)
    history_ms = max(s["observed_ms"] for s in (item["first_near"], item["first_ready"]) if s)
    conn.execute("""INSERT INTO entry_candidate_history
        (setup_id,ticker,history_ms,has_ready,has_near,payload) VALUES (?,?,?,?,?,?)
        ON CONFLICT(setup_id) DO UPDATE SET history_ms=excluded.history_ms,
        has_ready=excluded.has_ready,has_near=excluded.has_near,payload=excluded.payload""",
        (identity, item["ticker"], history_ms, int(item["first_ready"] is not None),
         int(item["first_near"] is not None), json.dumps(item, allow_nan=False)))


def initialize(conn):
    """Additive, one-time import of available identified events and current signals."""
    conn.execute("""CREATE TABLE IF NOT EXISTS entry_candidate_history (
        setup_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, history_ms INTEGER NOT NULL,
        has_ready INTEGER NOT NULL, has_near INTEGER NOT NULL, payload TEXT NOT NULL)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS entry_history_order
        ON entry_candidate_history(history_ms DESC,setup_id DESC)""")
    if conn.execute("SELECT 1 FROM app_state WHERE key='entry_history_import_v1'").fetchone():
        return
    for event in conn.execute("SELECT * FROM candidate_events WHERE setup_id IS NOT NULL ORDER BY created_ms,id"):
        if event["kind"] not in {"READY", "NEAR"} or not event["setup_id"]:
            continue
        row = dict(event)
        row.update(entry_reference=row["entry"], stop_loss=row["stop"], take_profit=row["target"])
        _record(conn, row, row["created_ms"], row["kind"])
    for signal in conn.execute("SELECT payload FROM paper_signals ORDER BY created_ms,signal_key"):
        row = json.loads(signal["payload"])
        if not row.get("setup_id"):
            continue
        row.update(stage="READY", direction=row.get("direction") or row.get("side"), entry_reference=row.get("entry"),
                   stop_loss=row.get("stop"), take_profit=row.get("target"))
        _record(conn, row, row["created_ms"], "READY")
    conn.execute("""INSERT INTO app_state(key,value,updated_ms)
        VALUES ('entry_history_import_v1','completed',0)""")


def capture(conn, rows, *, now_ms, min_rr, monitor_ms, entry_ms):
    for row in rows:
        if (not row.get("setup_id") or setup_identity(row) != row["setup_id"]
                or not current_observation(row, now_ms, monitor_ms, entry_ms)):
            continue
        _record(conn, row, now_ms, kind(row, min_rr))


def review(conn, *, now_ms, days=7, candidate_kind="ALL", ticker="", limit=30,
           before_ms=None, before_setup=None):
    conditions, params = [], []
    if days:
        conditions.append("history_ms>=?")
        params.append(now_ms-days*86_400_000)
    if candidate_kind != "ALL":
        conditions.append("has_ready=1" if candidate_kind == "READY" else "has_near=1")
    if ticker:
        conditions.append("instr(ticker,?)>0")
        params.append(ticker.strip().upper())
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    totals = dict(conn.execute("SELECT count(*) AS setups,coalesce(sum(has_ready),0) AS ready,"
                              "coalesce(sum(has_near),0) AS near FROM entry_candidate_history"+where,
                              params).fetchone())
    if before_ms is not None:
        conditions.append("(history_ms<? OR (history_ms=? AND setup_id<?))")
        params.extend([before_ms, before_ms, before_setup])
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute("SELECT setup_id,history_ms,payload FROM entry_candidate_history"+where+
                        " ORDER BY history_ms DESC,setup_id DESC LIMIT ?", params+[limit+1]).fetchall()
    items = []
    for row in rows[:limit]:
        item = json.loads(row["payload"])
        lifecycle = conn.execute("SELECT payload FROM setup_lifecycles WHERE setup_id=?",
                                 (row["setup_id"],)).fetchone()
        life = json.loads(lifecycle["payload"]) if lifecycle else {}
        item["lifecycle"] = {k: life.get(k) for k in ("status", "ended_ms", "end_reason", "last_observed_ms")}
        # An explicit setup join and FIRST signal prevent ticker reuse and duplicate outcomes.
        signal = conn.execute("""SELECT payload FROM paper_signals
            WHERE json_extract(payload,'$.setup_id')=? ORDER BY created_ms,signal_key LIMIT 1""",
            (row["setup_id"],)).fetchone()
        item["reference_outcome"] = None
        if signal:
            signal = json.loads(signal["payload"])
            result = signal.get("result") or {}
            item["reference_outcome"] = dict(
                hypothetical=True, signal_created_ms=signal["created_ms"],
                verified=verified_result(result),
                **{k: result.get(k) for k in ("status", "final_r", "mfe_r", "mae_r", "outcome_time_ms")})
        items.append(item)
    cursor = dict(before_ms=rows[limit-1]["history_ms"], before_setup=rows[limit-1]["setup_id"]) if len(rows)>limit else None
    return dict(historical=True, current_entry_status=False, totals=totals,
                items=items, next_cursor=cursor, retention="PERSISTENT",
                import_scope="Available identified events and current signals; earlier deleted history cannot be recovered")
