from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import sys
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from pywebpush import WebPushException, webpush

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app as scanner
from analysis_terminal.outcomes import evaluate_paper_signal, verified_result

SETTINGS = scanner.Settings.from_env(dry_run_override=True)
CLIENT = scanner.EdgeXClient(SETTINGS)
DETECTOR = scanner.RollReversalDetector(SETTINGS)

_snapshot_cache: tuple[float, dict[tuple[str, str], list[scanner.Candle]]] | None = None
_cache_lock = asyncio.Lock()
DB_PATH = Path(os.getenv("ANALYSIS_DB_PATH", "/data/analysis_terminal.db"))
VAPID_PRIVATE_KEY = os.getenv("WEBPUSH_VAPID_PRIVATE_KEY", "").strip()
VAPID_PUBLIC_KEY = os.getenv("WEBPUSH_VAPID_PUBLIC_KEY", "").strip()
VAPID_SUBJECT = os.getenv(
    "WEBPUSH_VAPID_SUBJECT",
    "https://edgex-analysis-terminal-production.up.railway.app",
).strip()
DAILY_SUMMARY_HOUR_JST = int(os.getenv("DAILY_SUMMARY_HOUR_JST", "8"))
VAPID_KEY_PATH = DB_PATH.parent / "webpush_vapid_private.pem"
JST = ZoneInfo("Asia/Tokyo")
_background_task: asyncio.Task | None = None


def _db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                bucket_ms INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                created_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_signals (
                signal_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_ms INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                candidate_alerts INTEGER NOT NULL DEFAULT 1,
                daily_summary INTEGER NOT NULL DEFAULT 1,
                timezone TEXT NOT NULL DEFAULT 'Asia/Tokyo',
                created_ms INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL,
                last_success_ms INTEGER
            )
            """
        )
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(push_subscriptions)").fetchall()
        }
        if "snooze_until_ms" not in columns:
            conn.execute(
                "ALTER TABLE push_subscriptions ADD COLUMN snooze_until_ms INTEGER"
            )
        if "quiet_start" not in columns:
            conn.execute(
                "ALTER TABLE push_subscriptions ADD COLUMN quiet_start TEXT"
            )
        if "quiet_end" not in columns:
            conn.execute(
                "ALTER TABLE push_subscriptions ADD COLUMN quiet_end TEXT"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                url TEXT,
                tag TEXT,
                sent INTEGER NOT NULL,
                attempted INTEGER NOT NULL,
                created_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candidate_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                kind TEXT NOT NULL,
                label TEXT NOT NULL,
                stage TEXT,
                direction TEXT,
                score REAL,
                rr REAL,
                entry REAL,
                stop REAL,
                target REAL,
                created_ms INTEGER NOT NULL
            )
            """
        )
        candidate_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(candidate_events)").fetchall()
        }
        for column_name, column_type in (
            ("confirmation_color_ok", "INTEGER"),
            ("confirmation_level_ok", "INTEGER"),
            ("confirmation_body_atr", "REAL"),
            ("confirmation_roll_margin_atr", "REAL"),
        ):
            if column_name not in candidate_columns:
                conn.execute(
                    f"ALTER TABLE candidate_events ADD COLUMN {column_name} {column_type}"
                )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candidate_event_results (
                event_id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_ms INTEGER NOT NULL,
                FOREIGN KEY(event_id) REFERENCES candidate_events(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS synced_watchlists (
                sync_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL,
                ticker TEXT NOT NULL,
                condition TEXT NOT NULL,
                threshold REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_ms INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL,
                triggered_ms INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_reports (
                report_date TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS priority_snapshots (
                bucket_ms INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                created_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS priority_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket_ms INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                event_type TEXT NOT NULL,
                previous_rank INTEGER,
                current_rank INTEGER NOT NULL,
                rank_change INTEGER,
                priority_score REAL NOT NULL,
                stage TEXT,
                direction TEXT,
                rr REAL,
                created_ms INTEGER NOT NULL,
                UNIQUE(bucket_ms, ticker, event_type)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approach_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket_ms INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                previous_score REAL,
                current_score REAL NOT NULL,
                current_rank INTEGER,
                stage TEXT,
                direction TEXT,
                rr REAL,
                priority_score REAL,
                entry REAL,
                stop REAL,
                target REAL,
                created_ms INTEGER NOT NULL,
                UNIQUE(bucket_ms, ticker)
            )
            """
        )
        approach_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(approach_events)").fetchall()
        }
        for column_name, column_type in (
            ("priority_score", "REAL"),
            ("entry", "REAL"),
            ("stop", "REAL"),
            ("target", "REAL"),
        ):
            if column_name not in approach_columns:
                conn.execute(
                    f"ALTER TABLE approach_events ADD COLUMN {column_name} {column_type}"
                )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approach_event_results (
                event_id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_ms INTEGER NOT NULL,
                FOREIGN KEY(event_id) REFERENCES approach_events(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shadow_v2_signals (
                signal_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_ms INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL
            )
            """
        )
        conn.commit()

    if VAPID_PRIVATE_KEY:
        VAPID_KEY_PATH.write_text(VAPID_PRIVATE_KEY + "\n", encoding="utf-8")
        try:
            VAPID_KEY_PATH.chmod(0o600)
        except OSError:
            pass


def _save_market_snapshot(payload: dict[str, Any]) -> None:
    bucket_ms = int(payload["time_ms"])
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO market_snapshots(bucket_ms, payload, created_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(bucket_ms) DO UPDATE SET
                payload=excluded.payload,
                created_ms=excluded.created_ms
            """,
            (bucket_ms, json.dumps(payload, separators=(",", ":")), now_ms),
        )
        cutoff = now_ms - 30 * 24 * 60 * 60 * 1000
        conn.execute("DELETE FROM market_snapshots WHERE bucket_ms < ?", (cutoff,))
        conn.commit()


def _insert_paper_signal(signal: dict[str, Any]) -> bool:
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO paper_signals(signal_key, payload, created_ms, updated_ms)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(signal["key"]),
                json.dumps(signal, separators=(",", ":")),
                int(signal["created_ms"]),
                now_ms,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def _update_paper_signal(signal: dict[str, Any]) -> None:
    now_ms = int(time.time() * 1000)
    signal["updated_ms"] = now_ms
    with _db_connect() as conn:
        conn.execute(
            """
            UPDATE paper_signals
            SET payload = ?, updated_ms = ?
            WHERE signal_key = ?
            """,
            (
                json.dumps(signal, separators=(",", ":")),
                now_ms,
                str(signal["key"]),
            ),
        )
        conn.commit()


def _load_market_history(hours: int = 48) -> list[dict[str, Any]]:
    cutoff = int(time.time() * 1000) - max(1, hours) * 60 * 60 * 1000
    with _db_connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM market_snapshots WHERE bucket_ms >= ? ORDER BY bucket_ms",
            (cutoff,),
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _load_paper_signals(limit: int = 200) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM paper_signals ORDER BY created_ms DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _insert_shadow_v2_signal(signal: dict[str, Any]) -> bool:
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO shadow_v2_signals(
                signal_key, payload, created_ms, updated_ms
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                str(signal["key"]),
                json.dumps(signal, separators=(",", ":")),
                int(signal["created_ms"]),
                now_ms,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def _update_shadow_v2_signal(signal: dict[str, Any]) -> None:
    now_ms = int(time.time() * 1000)
    signal["updated_ms"] = now_ms
    with _db_connect() as conn:
        conn.execute(
            """
            UPDATE shadow_v2_signals
            SET payload = ?, updated_ms = ?
            WHERE signal_key = ?
            """,
            (
                json.dumps(signal, separators=(",", ":")),
                now_ms,
                str(signal["key"]),
            ),
        )
        conn.commit()


def _load_shadow_v2_signals(limit: int = 500) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT payload
            FROM shadow_v2_signals
            ORDER BY created_ms DESC
            LIMIT ?
            """,
            (max(1, min(limit, 2000)),),
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _persist_shadow_v2_signals(rows: list[dict[str, Any]]) -> int:
    inserted = 0
    interval_ms = scanner.INTERVAL_MS[SETTINGS.entry_interval]
    for row in rows:
        if row.get("shadow_v2_ready") is not True:
            continue
        entry = row.get("entry_reference")
        stop = row.get("shadow_stop_loss")
        target = row.get("shadow_v2_target")
        candle_ms = row.get("latest_15m_time_ms")
        if None in {entry, stop, target, candle_ms}:
            continue
        signal_close_ms = int(candle_ms) + interval_ms
        signal = {
            "key": (
                f"v2:{row.get('ticker')}:{row.get('direction')}:"
                f"{signal_close_ms}"
            ),
            "model": "measured_room_fixed_2r",
            "ticker": row.get("ticker"),
            "side": row.get("direction"),
            "entry": float(entry),
            "stop": float(stop),
            "target": float(target),
            "extension_target": row.get("shadow_v2_extension_target"),
            "room_rr": row.get("shadow_v2_room_rr"),
            "score": row.get("score"),
            "signal_candle_ms": int(candle_ms),
            # +1ms guarantees the signal candle itself cannot decide TP/SL.
            "created_ms": signal_close_ms + 1,
            "result": None,
        }
        if _insert_shadow_v2_signal(signal):
            inserted += 1
    return inserted


def _shadow_v2_metrics(
    signals: list[dict[str, Any]],
) -> dict[str, Any]:
    resolved = [
        signal for signal in signals
        if str((signal.get("result") or {}).get("status") or "")
        in {"TP", "SL"}
        and verified_result(signal.get("result"))
    ]
    tp = [
        signal for signal in resolved
        if (signal.get("result") or {}).get("status") == "TP"
    ]
    sl = [
        signal for signal in resolved
        if (signal.get("result") or {}).get("status") == "SL"
    ]
    rs = [
        float((signal.get("result") or {}).get("final_r"))
        for signal in resolved
        if (signal.get("result") or {}).get("final_r") is not None
    ]
    gross_win = sum(max(0.0, r) for r in rs)
    gross_loss = abs(sum(min(0.0, r) for r in rs))
    pf = (
        gross_win / gross_loss
        if gross_loss > 0
        else (float("inf") if gross_win > 0 else None)
    )
    avg_r = mean(rs) if rs else None
    decision_ready = len(resolved) >= 20
    promotion_pass = bool(
        decision_ready
        and avg_r is not None
        and avg_r > 0
        and pf is not None
        and pf > 1.0
    )
    return {
        "tracked": len(signals),
        "unverified_results": sum(
            1 for signal in signals
            if signal.get("result") and not verified_result(signal.get("result"))
        ),
        "sample_status": "SUFFICIENT SAMPLE" if decision_ready else "INSUFFICIENT SAMPLE",
        "open": sum(
            1 for signal in signals
            if str((signal.get("result") or {}).get("status") or "OPEN")
            not in {"TP", "SL", "AMBIGUOUS"}
        ),
        "resolved": len(resolved),
        "tp": len(tp),
        "sl": len(sl),
        "win_rate": (
            round(len(tp) / len(resolved) * 100.0, 1)
            if resolved
            else None
        ),
        "avg_r": round(avg_r, 3) if avg_r is not None else None,
        "profit_factor": (
            "INF" if pf == float("inf")
            else round(pf, 3) if pf is not None
            else None
        ),
        "decision_ready": decision_ready,
        "promotion_pass": promotion_pass,
        "minimum_resolved": 20,
    }


def _state_get(key: str) -> str | None:
    with _db_connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (key,),
        ).fetchone()
    return str(row["value"]) if row else None


def _state_set(key: str, value: str) -> None:
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO app_state(key, value, updated_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_ms=excluded.updated_ms
            """,
            (key, value, now_ms),
        )
        conn.commit()


def _push_enabled() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_KEY_PATH.exists())


def _subscription_count() -> int:
    with _db_connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM push_subscriptions").fetchone()
    return int(row["n"]) if row else 0


def _save_push_subscription(req: PushSubscriptionRequest) -> str:
    endpoint = str(req.subscription.get("endpoint") or "").strip()
    keys = req.subscription.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "Invalid PushSubscription payload")
    now_ms = int(time.time() * 1000)
    payload = json.dumps(req.subscription, separators=(",", ":"))
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions(
                endpoint, payload, candidate_alerts, daily_summary,
                timezone, created_ms, updated_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                payload=excluded.payload,
                candidate_alerts=excluded.candidate_alerts,
                daily_summary=excluded.daily_summary,
                timezone=excluded.timezone,
                updated_ms=excluded.updated_ms
            """,
            (
                endpoint,
                payload,
                int(req.candidate_alerts),
                int(req.daily_summary),
                req.timezone,
                now_ms,
                now_ms,
            ),
        )
        conn.commit()
    return endpoint


def _delete_push_subscription(endpoint: str) -> None:
    with _db_connect() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        conn.commit()


def _hhmm_minutes(value: str | None) -> int | None:
    if not value:
        return None
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _subscription_suppressed(
    subscription: sqlite3.Row,
    kind: str | None,
) -> bool:
    if kind != "candidate":
        return False
    now_ms = int(time.time() * 1000)
    snooze_until = subscription["snooze_until_ms"]
    if snooze_until is not None and int(snooze_until) > now_ms:
        return True

    start = _hhmm_minutes(subscription["quiet_start"])
    end = _hhmm_minutes(subscription["quiet_end"])
    if start is None or end is None or start == end:
        return False
    try:
        timezone = ZoneInfo(str(subscription["timezone"] or "Asia/Tokyo"))
    except Exception:
        timezone = JST
    local_now = datetime.now(timezone)
    current = local_now.hour * 60 + local_now.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _load_push_subscriptions(kind: str | None = None) -> list[sqlite3.Row]:
    where = ""
    if kind == "candidate":
        where = "WHERE candidate_alerts = 1"
    elif kind == "daily":
        where = "WHERE daily_summary = 1"
    with _db_connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM push_subscriptions {where} ORDER BY created_ms"
        ).fetchall()
    return [
        row
        for row in rows
        if not _subscription_suppressed(row, kind)
    ]


def _send_push_sync(subscription: sqlite3.Row, payload: dict[str, Any]) -> bool:
    if not _push_enabled():
        return False
    endpoint = str(subscription["endpoint"])
    try:
        webpush(
            subscription_info=json.loads(subscription["payload"]),
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            vapid_private_key=str(VAPID_KEY_PATH),
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=900,
            timeout=12,
        )
        now_ms = int(time.time() * 1000)
        with _db_connect() as conn:
            conn.execute(
                """
                UPDATE push_subscriptions
                SET last_success_ms = ?, updated_ms = ?
                WHERE endpoint = ?
                """,
                (now_ms, now_ms, endpoint),
            )
            conn.commit()
        return True
    except WebPushException as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status in {404, 410}:
            _delete_push_subscription(endpoint)
        print(f"Web Push error status={status}: {exc}", flush=True)
        return False
    except Exception as exc:
        print(f"Web Push error: {exc}", flush=True)
        return False


async def _broadcast_push(
    payload: dict[str, Any],
    *,
    kind: str,
) -> tuple[int, int]:
    subscriptions = _load_push_subscriptions(kind)
    if not subscriptions:
        _log_push_event(
            kind=kind,
            payload=payload,
            sent=0,
            attempted=0,
        )
        return 0, 0
    results = await asyncio.gather(
        *(
            asyncio.to_thread(_send_push_sync, subscription, payload)
            for subscription in subscriptions
        )
    )
    sent = sum(1 for result in results if result)
    attempted = len(results)
    _log_push_event(
        kind=kind,
        payload=payload,
        sent=sent,
        attempted=attempted,
    )
    return sent, attempted


def _update_push_preferences(req: PushPreferenceRequest) -> bool:
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        cur = conn.execute(
            """
            UPDATE push_subscriptions
            SET candidate_alerts = ?,
                daily_summary = ?,
                snooze_until_ms = ?,
                quiet_start = ?,
                quiet_end = ?,
                updated_ms = ?
            WHERE endpoint = ?
            """,
            (
                int(req.candidate_alerts),
                int(req.daily_summary),
                req.snooze_until_ms,
                req.quiet_start,
                req.quiet_end,
                now_ms,
                req.endpoint,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def _get_push_subscription(endpoint: str) -> sqlite3.Row | None:
    with _db_connect() as conn:
        return conn.execute(
            "SELECT * FROM push_subscriptions WHERE endpoint = ?",
            (endpoint,),
        ).fetchone()


def _log_push_event(
    *,
    kind: str,
    payload: dict[str, Any],
    sent: int,
    attempted: int,
) -> None:
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO push_events(
                kind, title, body, url, tag, sent, attempted, created_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                str(payload.get("title") or ""),
                str(payload.get("body") or ""),
                str(payload.get("url") or ""),
                str(payload.get("tag") or ""),
                int(sent),
                int(attempted),
                now_ms,
            ),
        )
        cutoff = now_ms - 30 * 24 * 60 * 60 * 1000
        conn.execute("DELETE FROM push_events WHERE created_ms < ?", (cutoff,))
        conn.commit()


def _load_push_events(limit: int = 50) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, kind, title, body, url, tag, sent, attempted, created_ms
            FROM push_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
    return [dict(row) for row in rows]


def _log_candidate_event(
    row: dict[str, Any],
    *,
    kind: str,
    label: str,
) -> None:
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO candidate_events(
                ticker, kind, label, stage, direction, score, rr,
                entry, stop, target, confirmation_color_ok,
                confirmation_level_ok, confirmation_body_atr,
                confirmation_roll_margin_atr, created_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(row.get("ticker") or ""),
                kind,
                label,
                row.get("stage"),
                row.get("direction"),
                row.get("score"),
                row.get("rr"),
                row.get("entry_reference"),
                row.get("stop_loss"),
                row.get("take_profit"),
                int(bool(row.get("confirmation_color_ok")))
                if row.get("confirmation_color_ok") is not None
                else None,
                int(bool(row.get("confirmation_level_ok")))
                if row.get("confirmation_level_ok") is not None
                else None,
                row.get("confirmation_body_atr"),
                row.get("confirmation_roll_margin_atr"),
                now_ms,
            ),
        )
        cutoff = now_ms - 30 * 24 * 60 * 60 * 1000
        conn.execute("DELETE FROM candidate_events WHERE created_ms < ?", (cutoff,))
        conn.commit()


def _load_candidate_events(limit: int = 100) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, ticker, kind, label, stage, direction, score, rr,
                   entry, stop, target, confirmation_color_ok,
                   confirmation_level_ok, confirmation_body_atr,
                   confirmation_roll_margin_atr, created_ms
            FROM candidate_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
    return [dict(row) for row in rows]


def _upsert_candidate_event_result(
    event_id: int,
    result: dict[str, Any],
) -> None:
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO candidate_event_results(event_id, payload, updated_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                payload=excluded.payload,
                updated_ms=excluded.updated_ms
            """,
            (
                int(event_id),
                json.dumps(result, separators=(",", ":")),
                now_ms,
            ),
        )
        conn.commit()


def _load_candidate_events_with_results(
    limit: int = 500,
) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                e.id, e.ticker, e.kind, e.label, e.stage, e.direction,
                e.score, e.rr, e.entry, e.stop, e.target,
                e.confirmation_color_ok, e.confirmation_level_ok,
                e.confirmation_body_atr, e.confirmation_roll_margin_atr,
                e.created_ms, r.payload AS result_payload,
                r.updated_ms AS result_updated_ms
            FROM candidate_events e
            LEFT JOIN candidate_event_results r ON r.event_id = e.id
            ORDER BY e.id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 1000)),),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw = item.pop("result_payload", None)
        item["result"] = json.loads(raw) if raw else None
        items.append(item)
    return items


def _normalize_sync_key(value: str) -> str:
    key = value.strip()
    if len(key) < 24 or len(key) > 128:
        raise HTTPException(400, "Invalid sync key")
    return key


def _load_synced_watchlist(sync_key: str) -> list[str]:
    key = _normalize_sync_key(sync_key)
    with _db_connect() as conn:
        row = conn.execute(
            "SELECT payload FROM synced_watchlists WHERE sync_key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return []
    try:
        values = json.loads(row["payload"])
    except json.JSONDecodeError:
        return []
    return [
        str(item).strip().upper()
        for item in values
        if str(item).strip()
    ][:200]


def _save_synced_watchlist(req: WatchlistSyncRequest) -> list[str]:
    key = _normalize_sync_key(req.sync_key)
    values = list(dict.fromkeys(
        str(item).strip().upper()
        for item in req.tickers
        if str(item).strip()
    ))[:200]
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO synced_watchlists(sync_key, payload, updated_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(sync_key) DO UPDATE SET
                payload=excluded.payload,
                updated_ms=excluded.updated_ms
            """,
            (
                key,
                json.dumps(values, separators=(",", ":")),
                now_ms,
            ),
        )
        conn.commit()
    return values


def _create_custom_alert(req: CustomAlertCreateRequest) -> dict[str, Any]:
    condition = req.condition.strip().upper()
    allowed = {"PRICE_ABOVE", "PRICE_BELOW", "ENTRY_NEAR", "RR_ABOVE"}
    if condition not in allowed:
        raise HTTPException(400, "Unsupported alert condition")
    if _get_push_subscription(req.endpoint) is None:
        raise HTTPException(404, "Push subscription not found")
    ticker = req.ticker.strip().upper()
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO custom_alerts(
                endpoint, ticker, condition, threshold, active,
                created_ms, updated_ms
            )
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (
                req.endpoint,
                ticker,
                condition,
                float(req.threshold),
                now_ms,
                now_ms,
            ),
        )
        alert_id = int(cur.lastrowid)
        conn.commit()
    return {
        "id": alert_id,
        "ticker": ticker,
        "condition": condition,
        "threshold": float(req.threshold),
        "active": True,
        "created_ms": now_ms,
        "triggered_ms": None,
    }


def _delete_custom_alert(req: CustomAlertDeleteRequest) -> bool:
    with _db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM custom_alerts WHERE id = ? AND endpoint = ?",
            (int(req.alert_id), req.endpoint),
        )
        conn.commit()
        return cur.rowcount > 0


def _load_custom_alerts(
    endpoint: str | None = None,
    *,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if endpoint:
        clauses.append("endpoint = ?")
        params.append(endpoint)
    if active_only:
        clauses.append("active = 1")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _db_connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, endpoint, ticker, condition, threshold, active,
                   created_ms, updated_ms, triggered_ms
            FROM custom_alerts
            {where}
            ORDER BY active DESC, id DESC
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def _custom_alert_match(
    alert: dict[str, Any],
    row: dict[str, Any],
) -> tuple[bool, str]:
    condition = str(alert["condition"])
    threshold = float(alert["threshold"])
    price = row.get("current_price")
    entry = row.get("entry_reference")
    rr = row.get("rr")
    if condition == "PRICE_ABOVE" and price is not None:
        matched = float(price) >= threshold
        return matched, f"価格 {float(price):.8g} ≥ {threshold:.8g}"
    if condition == "PRICE_BELOW" and price is not None:
        matched = float(price) <= threshold
        return matched, f"価格 {float(price):.8g} ≤ {threshold:.8g}"
    if condition == "RR_ABOVE" and rr is not None:
        matched = float(rr) >= threshold
        return matched, f"RR {float(rr):.2f} ≥ {threshold:.2f}"
    if condition == "ENTRY_NEAR" and price is not None and entry is not None and float(entry) > 0:
        distance_pct = abs(float(price) - float(entry)) / float(entry) * 100.0
        matched = distance_pct <= threshold
        return matched, f"Entryまで {distance_pct:.2f}% ≤ {threshold:.2f}%"
    return False, ""


async def _evaluate_custom_alerts(rows: list[dict[str, Any]]) -> None:
    alerts = _load_custom_alerts(active_only=True)
    if not alerts:
        return
    row_map = {
        str(row.get("ticker") or "").upper(): row
        for row in rows
    }
    for alert in alerts:
        row = row_map.get(str(alert["ticker"]).upper())
        if row is None:
            continue
        matched, detail = _custom_alert_match(alert, row)
        if not matched:
            continue
        subscription = _get_push_subscription(str(alert["endpoint"]))
        if subscription is None:
            continue
        if _subscription_suppressed(subscription, "candidate"):
            continue
        payload = {
            "title": f"EdgeX 条件アラート — {alert['ticker']}",
            "body": detail,
            "url": f"/?tab=analysis&ticker={alert['ticker']}",
            "tag": f"edgex-custom-{alert['id']}",
        }
        delivered = await asyncio.to_thread(
            _send_push_sync,
            subscription,
            payload,
        )
        _log_push_event(
            kind="custom",
            payload=payload,
            sent=1 if delivered else 0,
            attempted=1,
        )
        if delivered:
            now_ms = int(time.time() * 1000)
            with _db_connect() as conn:
                conn.execute(
                    """
                    UPDATE custom_alerts
                    SET active = 0, triggered_ms = ?, updated_ms = ?
                    WHERE id = ?
                    """,
                    (now_ms, now_ms, int(alert["id"])),
                )
                conn.commit()


def _jst_day_bounds(date_value) -> tuple[int, int]:
    start = datetime(
        date_value.year,
        date_value.month,
        date_value.day,
        tzinfo=JST,
    )
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _build_daily_report(report_date: str) -> dict[str, Any]:
    date_value = datetime.strptime(report_date, "%Y-%m-%d").date()
    start_ms, end_ms = _jst_day_bounds(date_value)
    with _db_connect() as conn:
        snapshot_rows = conn.execute(
            """
            SELECT payload FROM market_snapshots
            WHERE bucket_ms >= ? AND bucket_ms < ?
            ORDER BY bucket_ms
            """,
            (start_ms, end_ms),
        ).fetchall()
        candidate_rows = conn.execute(
            """
            SELECT kind, ticker, created_ms
            FROM candidate_events
            WHERE created_ms >= ? AND created_ms < ?
            ORDER BY created_ms
            """,
            (start_ms, end_ms),
        ).fetchall()
        paper_rows = conn.execute(
            """
            SELECT payload FROM paper_signals
            WHERE created_ms >= ? AND created_ms < ?
            ORDER BY created_ms
            """,
            (start_ms, end_ms),
        ).fetchall()

    snapshots = [json.loads(row["payload"]) for row in snapshot_rows]
    candidates = [dict(row) for row in candidate_rows]
    paper = [json.loads(row["payload"]) for row in paper_rows]
    ready_events = [item for item in candidates if item["kind"] == "READY"]
    near_events = [item for item in candidates if item["kind"] == "NEAR"]
    latest = snapshots[-1] if snapshots else {}
    peak_ready = max((int(item.get("ready") or 0) for item in snapshots), default=0)
    peak_qualified_near = max(
        (int(item.get("qualified_near") or 0) for item in snapshots),
        default=0,
    )
    return {
        "date": report_date,
        "snapshot_count": len(snapshots),
        "ready_events": len(ready_events),
        "near_events": len(near_events),
        "paper_signals": len(paper),
        "peak_ready": peak_ready,
        "peak_qualified_near": peak_qualified_near,
        "last_market": latest,
        "top_ready_tickers": list(dict.fromkeys(
            str(item["ticker"]) for item in ready_events
        ))[:10],
        "top_near_tickers": list(dict.fromkeys(
            str(item["ticker"]) for item in near_events
        ))[:10],
    }


def _save_daily_report(report: dict[str, Any]) -> None:
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO daily_reports(report_date, payload, created_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(report_date) DO UPDATE SET
                payload=excluded.payload,
                created_ms=excluded.created_ms
            """,
            (
                str(report["date"]),
                json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                now_ms,
            ),
        )
        cutoff_date = (datetime.now(JST).date() - timedelta(days=45)).isoformat()
        conn.execute(
            "DELETE FROM daily_reports WHERE report_date < ?",
            (cutoff_date,),
        )
        conn.commit()


def _load_daily_reports(limit: int = 30) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT payload FROM daily_reports
            ORDER BY report_date DESC
            LIMIT ?
            """,
            (max(1, min(limit, 90)),),
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _maybe_generate_daily_report() -> None:
    target_date = datetime.now(JST).date() - timedelta(days=1)
    report_date = target_date.isoformat()
    with _db_connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM daily_reports WHERE report_date = ?",
            (report_date,),
        ).fetchone()
    if exists:
        return
    _save_daily_report(_build_daily_report(report_date))


def _push_row_summary(row: dict[str, Any]) -> str:
    direction = "ロング" if row.get("direction") == "LONG" else "ショート"
    rr = row.get("rr")
    score = row.get("score")
    parts = [str(row.get("ticker") or ""), direction]
    if score is not None:
        parts.append(f"{float(score):.1f}点")
    if rr is not None:
        parts.append(f"RR {float(rr):.2f}")
    return " / ".join(parts)


def _push_trade_levels(row: dict[str, Any]) -> str:
    entry = row.get("entry_reference")
    stop = row.get("stop_loss")
    target = row.get("take_profit")
    if entry is None or stop is None or target is None:
        return ""
    return (
        f"\nEntry {float(entry):.8g} / "
        f"SL {float(stop):.8g} / TP {float(target):.8g}"
    )


async def _maybe_push_candidate_changes(rows: list[dict[str, Any]]) -> None:
    ready = [
        row for row in _sort_rows(rows)
        if row.get("stage") == "READY"
    ]
    near = [
        row for row in _sort_rows(rows)
        if row.get("stage") in {"CONFIRMATION_WAIT", "RETEST_WAIT"}
        and row.get("rr") is not None
        and float(row["rr"]) >= SETTINGS.min_rr
    ]

    current = {
        "ready": [str(row["ticker"]) for row in ready],
        "near": [str(row["ticker"]) for row in near],
    }
    previous_raw = _state_get("server_candidate_state")
    _state_set(
        "server_candidate_state",
        json.dumps(current, separators=(",", ":")),
    )
    if not previous_raw:
        return

    try:
        previous = json.loads(previous_raw)
    except json.JSONDecodeError:
        return

    prev_ready = set(previous.get("ready") or [])
    prev_near = set(previous.get("near") or [])
    for row in ready:
        ticker = str(row["ticker"])
        if ticker in prev_ready:
            continue
        label = (
            "直前候補からエントリー可能へ昇格"
            if ticker in prev_near
            else "新しくエントリー可能"
        )
        _log_candidate_event(
            row,
            kind="READY",
            label=label,
        )
        await _broadcast_push(
            {
                "title": "EdgeX エントリー候補",
                "body": (
                    f"{label}\n{_push_row_summary(row)}"
                    f"{_push_trade_levels(row)}"
                ),
                "url": f"/?tab=analysis&ticker={ticker}",
                "tag": f"edgex-ready-{ticker}",
            },
            kind="candidate",
        )

    for row in near:
        ticker = str(row["ticker"])
        if ticker in prev_near or ticker in prev_ready:
            continue
        _log_candidate_event(
            row,
            kind="NEAR",
            label="有力な直前候補に追加",
        )
        await _broadcast_push(
            {
                "title": "EdgeX 有力な直前候補",
                "body": f"15分足の条件に接近\n{_push_row_summary(row)}",
                "url": f"/?tab=analysis&ticker={ticker}",
                "tag": f"edgex-near-{ticker}",
            },
            kind="candidate",
        )


async def _maybe_push_daily_summary(rows: list[dict[str, Any]]) -> None:
    if not _load_push_subscriptions("daily"):
        return
    now = datetime.now(JST)
    if now.hour < DAILY_SUMMARY_HOUR_JST:
        return
    today = now.date().isoformat()
    if _state_get("daily_summary_date") == today:
        return

    summary = _market_summary(rows)
    regime = _market_regime(rows)
    picks = _daily_picks(rows)
    bias_ja = {
        "LONG_BIASED": "ロング優勢",
        "SHORT_BIASED": "ショート優勢",
        "BALANCED": "ほぼ均衡",
    }.get(regime["bias"], "方向不明")
    activity_ja = {
        "SIGNAL_ACTIVE": "シグナルあり",
        "SETUP_BUILDING": "セットアップ形成中",
        "QUIET": "静観相場",
        "SELECTIVE": "選別相場",
    }.get(regime["activity"], "状態不明")
    pick_text = "、".join(str(item["ticker"]) for item in picks) or "候補なし"
    payload = {
        "title": "EdgeX 朝の市場サマリー",
        "body": (
            f"{bias_ja} / {activity_ja}\n"
            f"エントリー可能 {summary['ready_count']}件 / "
            f"有力直前 {summary['qualified_near_count']}件\n"
            f"まず確認: {pick_text}"
        ),
        "url": "/?tab=dashboard",
        "tag": f"edgex-daily-{today}",
    }
    _sent, attempted = await _broadcast_push(payload, kind="daily")
    if attempted:
        _state_set("daily_summary_date", today)


async def fetch_snapshots(
    contract_ids: list[str],
    intervals: tuple[str, ...] = ("HOUR_4", "MINUTE_15"),
    timeout: float = 30.0,
) -> dict[tuple[str, str], list[scanner.Candle]]:
    expected = {(cid, interval) for cid in contract_ids for interval in intervals}
    found: dict[tuple[str, str], dict[int, scanner.Candle]] = {}

    async with websockets.connect(
        SETTINGS.ws_url,
        open_timeout=20,
        close_timeout=8,
        ping_interval=20,
        ping_timeout=20,
        max_size=8 * 1024 * 1024,
    ) as ws:
        await asyncio.wait_for(ws.recv(), timeout=20)
        for cid in contract_ids:
            for interval in intervals:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "channel": f"kline.LAST_PRICE.{cid}.{interval}",
                }))

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and set(found) != expected:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=max(0.2, deadline - time.monotonic())
                )
            except asyncio.TimeoutError:
                break

            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if str(message.get("type", "")).lower() == "ping":
                await ws.send(json.dumps({"type": "pong", "time": message.get("time")}))
                continue
            if str(message.get("type", "")).lower() != "quote-event":
                continue

            content = message.get("content") or {}
            channel = str(content.get("channel") or message.get("channel") or "")
            interval = channel.split(".")[-1].upper()
            if interval not in intervals:
                continue

            items = content.get("data") or []
            parsed: list[scanner.Candle] = []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        candle = scanner.Candle.from_payload(item, fallback_interval=interval)
                        if candle is not None:
                            parsed.append(candle)
            if not parsed:
                continue

            key = (parsed[0].contract_id, interval)
            bucket = found.setdefault(key, {})
            for candle in parsed:
                bucket[candle.time_ms] = candle

    return {
        key: sorted(bucket.values(), key=lambda candle: candle.time_ms)
        for key, bucket in found.items()
    }


def closed(candles: list[scanner.Candle], interval: str) -> list[scanner.Candle]:
    cutoff = int(time.time() * 1000)
    interval_ms = scanner.INTERVAL_MS[interval]
    return [
        candle
        for candle in sorted(candles, key=lambda item: item.time_ms)
        if candle.time_ms + interval_ms <= cutoff
    ]


def _evaluate_paper_signal(
    signal: dict[str, Any],
    candles: list[scanner.Candle],
) -> dict[str, Any]:
    return evaluate_paper_signal(
        signal,
        candles,
        interval_ms=scanner.INTERVAL_MS[SETTINGS.entry_interval],
        now_ms=int(time.time() * 1000),
    )


async def _refresh_paper_signal_results(
    contracts: dict[str, scanner.Contract],
) -> None:
    signals = _load_paper_signals(limit=1000)
    pending = [
        signal
        for signal in signals
        if (signal.get("result") or {}).get("status") not in {"TP", "SL", "AMBIGUOUS"}
    ]
    if not pending:
        return

    by_name = {contract.contract_name.upper(): contract for contract in contracts.values()}
    contract_ids: list[str] = []
    signal_contract: dict[str, scanner.Contract] = {}
    for signal in pending:
        contract = by_name.get(str(signal.get("ticker") or "").upper())
        if contract is None:
            continue
        signal_contract[str(signal["key"])] = contract
        if contract.contract_id not in contract_ids:
            contract_ids.append(contract.contract_id)

    if not contract_ids:
        return

    snapshots: dict[tuple[str, str], list[scanner.Candle]] = {}
    for start in range(0, len(contract_ids), 50):
        chunk = contract_ids[start : start + 50]
        try:
            part = await fetch_snapshots(
                chunk,
                intervals=(SETTINGS.entry_interval,),
                timeout=25.0,
            )
            snapshots.update(part)
        except Exception as exc:
            print(f"Paper signal refresh chunk error: {exc}", flush=True)

    for signal in pending:
        contract = signal_contract.get(str(signal["key"]))
        if contract is None:
            continue
        candles = snapshots.get((contract.contract_id, SETTINGS.entry_interval), [])
        if not candles:
            continue
        result = _evaluate_paper_signal(signal, candles)
        signal["result"] = result
        _update_paper_signal(signal)


async def _refresh_shadow_v2_results(
    contracts: dict[str, scanner.Contract],
) -> None:
    signals = _load_shadow_v2_signals(limit=2000)
    pending = [
        signal
        for signal in signals
        if str((signal.get("result") or {}).get("status") or "")
        not in {"TP", "SL", "AMBIGUOUS"}
    ]
    if not pending:
        return

    by_name = {
        contract.contract_name.upper(): contract
        for contract in contracts.values()
    }
    contract_ids: list[str] = []
    signal_contract: dict[str, scanner.Contract] = {}
    for signal in pending:
        contract = by_name.get(str(signal.get("ticker") or "").upper())
        if contract is None:
            continue
        signal_contract[str(signal["key"])] = contract
        if contract.contract_id not in contract_ids:
            contract_ids.append(contract.contract_id)

    if not contract_ids:
        return

    snapshots: dict[tuple[str, str], list[scanner.Candle]] = {}
    for start in range(0, len(contract_ids), 50):
        chunk = contract_ids[start : start + 50]
        try:
            part = await fetch_snapshots(
                chunk,
                intervals=(SETTINGS.entry_interval,),
                timeout=25.0,
            )
            snapshots.update(part)
        except Exception as exc:
            print(f"Shadow v2 refresh chunk error: {exc}", flush=True)

    for signal in pending:
        contract = signal_contract.get(str(signal["key"]))
        if contract is None:
            continue
        candles = snapshots.get(
            (contract.contract_id, SETTINGS.entry_interval),
            [],
        )
        if not candles:
            continue
        result = _evaluate_paper_signal(signal, candles)
        signal["result"] = result
        _update_shadow_v2_signal(signal)


async def _refresh_candidate_event_results(
    contracts: dict[str, scanner.Contract],
) -> None:
    events = _load_candidate_events_with_results(limit=1000)
    trackable = [
        event
        for event in events
        if event.get("entry") is not None
        and event.get("stop") is not None
        and event.get("target") is not None
        and str(event.get("direction") or "") in {"LONG", "SHORT"}
        and (
            not event.get("result")
            or str((event.get("result") or {}).get("status") or "")
            not in {"TP", "SL", "AMBIGUOUS"}
        )
    ]
    if not trackable:
        return

    by_name = {
        contract.contract_name.upper(): contract
        for contract in contracts.values()
    }
    contract_ids: list[str] = []
    event_contract: dict[int, scanner.Contract] = {}
    for event in trackable:
        contract = by_name.get(str(event.get("ticker") or "").upper())
        if contract is None:
            continue
        event_contract[int(event["id"])] = contract
        if contract.contract_id not in contract_ids:
            contract_ids.append(contract.contract_id)

    if not contract_ids:
        return

    snapshots: dict[tuple[str, str], list[scanner.Candle]] = {}
    for start in range(0, len(contract_ids), 50):
        chunk = contract_ids[start : start + 50]
        try:
            part = await fetch_snapshots(
                chunk,
                intervals=(SETTINGS.entry_interval,),
                timeout=25.0,
            )
            snapshots.update(part)
        except Exception as exc:
            print(f"Candidate outcome refresh chunk error: {exc}", flush=True)

    for event in trackable:
        contract = event_contract.get(int(event["id"]))
        if contract is None:
            continue
        candles = snapshots.get(
            (contract.contract_id, SETTINGS.entry_interval),
            [],
        )
        if not candles:
            continue
        signal = {
            "key": f"candidate-{event['id']}",
            "ticker": event["ticker"],
            "side": event["direction"],
            "entry": event["entry"],
            "stop": event["stop"],
            "target": event["target"],
            "created_ms": event["created_ms"],
            "result": event.get("result"),
        }
        result = _evaluate_paper_signal(signal, candles)
        _upsert_candidate_event_result(int(event["id"]), result)


async def _refresh_approach_event_results(
    contracts: dict[str, scanner.Contract],
) -> None:
    events = _load_approach_events_with_results(limit=1000)
    trackable = [
        event
        for event in events
        if event.get("entry") is not None
        and event.get("stop") is not None
        and event.get("target") is not None
        and str(event.get("direction") or "") in {"LONG", "SHORT"}
        and (
            not event.get("result")
            or str((event.get("result") or {}).get("status") or "")
            not in {"TP", "SL", "AMBIGUOUS"}
        )
    ]
    if not trackable:
        return

    by_name = {
        contract.contract_name.upper(): contract
        for contract in contracts.values()
    }
    contract_ids: list[str] = []
    event_contract: dict[int, scanner.Contract] = {}
    for event in trackable:
        contract = by_name.get(str(event.get("ticker") or "").upper())
        if contract is None:
            continue
        event_contract[int(event["id"])] = contract
        if contract.contract_id not in contract_ids:
            contract_ids.append(contract.contract_id)

    if not contract_ids:
        return

    snapshots: dict[tuple[str, str], list[scanner.Candle]] = {}
    for start in range(0, len(contract_ids), 50):
        chunk = contract_ids[start : start + 50]
        try:
            part = await fetch_snapshots(
                chunk,
                intervals=(SETTINGS.entry_interval,),
                timeout=25.0,
            )
            snapshots.update(part)
        except Exception as exc:
            print(f"Approach outcome refresh chunk error: {exc}", flush=True)

    for event in trackable:
        contract = event_contract.get(int(event["id"]))
        if contract is None:
            continue
        candles = snapshots.get(
            (contract.contract_id, SETTINGS.entry_interval),
            [],
        )
        if not candles:
            continue
        signal = {
            "key": f"approach-{event['id']}",
            "ticker": event["ticker"],
            "side": event["direction"],
            "entry": event["entry"],
            "stop": event["stop"],
            "target": event["target"],
            "created_ms": event["created_ms"],
            "result": event.get("result"),
        }
        result = _evaluate_paper_signal(signal, candles)
        _upsert_approach_event_result(int(event["id"]), result)


def _approach_score_band(value: float) -> str:
    if value >= 90:
        return "90+"
    if value >= 80:
        return "80-89"
    return "70-79"


def _approach_validation_metrics(
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    resolved = [
        item
        for item in items
        if str((item.get("result") or {}).get("status") or "")
        in {"TP", "SL"}
        and (item.get("result") or {}).get("coverage_complete") is not False
    ]
    tp = sum(
        1
        for item in resolved
        if (item.get("result") or {}).get("status") == "TP"
    )
    sl = sum(
        1
        for item in resolved
        if (item.get("result") or {}).get("status") == "SL"
    )
    rs = [
        float((item.get("result") or {}).get("final_r"))
        for item in resolved
        if (item.get("result") or {}).get("final_r") is not None
    ]
    mfes = [
        float((item.get("result") or {}).get("mfe_r") or 0)
        for item in items
        if item.get("result")
        and (item.get("result") or {}).get("coverage_complete") is not False
    ]
    maes = [
        float((item.get("result") or {}).get("mae_r") or 0)
        for item in items
        if item.get("result")
        and (item.get("result") or {}).get("coverage_complete") is not False
    ]
    ready_items = [item for item in items if item.get("became_ready")]
    ready_minutes = [
        float(item["minutes_to_ready"])
        for item in ready_items
        if item.get("minutes_to_ready") is not None
    ]
    matured = [item for item in items if item.get("matured")]
    return {
        "tracked": len(items),
        "matured": len(matured),
        "became_ready": len(ready_items),
        "ready_rate": (
            round(len(ready_items) / len(matured) * 100.0, 1)
            if matured
            else None
        ),
        "avg_minutes_to_ready": (
            round(mean(ready_minutes), 1)
            if ready_minutes
            else None
        ),
        "resolved": len(resolved),
        "tp": tp,
        "sl": sl,
        "win_rate": (
            round(tp / len(resolved) * 100.0, 1)
            if resolved
            else None
        ),
        "avg_r": round(mean(rs), 3) if rs else None,
        "avg_mfe_r": round(mean(mfes), 3) if mfes else None,
        "avg_mae_r": round(mean(maes), 3) if maes else None,
    }


def _approach_validation(limit: int = 500) -> dict[str, Any]:
    events = _load_approach_events_with_results(limit=limit)
    now_ms = int(time.time() * 1000)
    horizon_ms = 48 * 60 * 60 * 1000

    with _db_connect() as conn:
        ready_rows = conn.execute(
            """
            SELECT ticker, created_ms
            FROM candidate_events
            WHERE kind = 'READY'
            ORDER BY created_ms
            """
        ).fetchall()

    ready_by_ticker: dict[str, list[int]] = {}
    for row in ready_rows:
        ready_by_ticker.setdefault(str(row["ticker"]), []).append(
            int(row["created_ms"])
        )

    enriched: list[dict[str, Any]] = []
    for event in events:
        if (
            event.get("entry") is None
            or event.get("stop") is None
            or event.get("target") is None
        ):
            continue
        created_ms = int(event["created_ms"])
        result = event.get("result") or {}
        outcome_time = result.get("outcome_time_ms")
        observation_end = min(
            created_ms + horizon_ms,
            int(outcome_time) if outcome_time else now_ms,
        )
        ready_time = next(
            (
                ready_ms
                for ready_ms in ready_by_ticker.get(str(event["ticker"]), [])
                if created_ms < ready_ms <= observation_end
            ),
            None,
        )
        terminal = str(result.get("status") or "") in {
            "TP",
            "SL",
            "AMBIGUOUS",
        }
        matured = (
            ready_time is not None
            or terminal
            or now_ms >= created_ms + horizon_ms
        )
        enriched_event = dict(event)
        enriched_event["became_ready"] = ready_time is not None
        enriched_event["ready_time_ms"] = ready_time
        enriched_event["minutes_to_ready"] = (
            round((ready_time - created_ms) / 60000.0, 1)
            if ready_time is not None
            else None
        )
        enriched_event["matured"] = matured
        enriched.append(enriched_event)

    groups: list[dict[str, Any]] = []
    for band in ("70-79", "80-89", "90+"):
        members = [
            item
            for item in enriched
            if _approach_score_band(float(item.get("current_score") or 0))
            == band
        ]
        groups.append({
            "label": band,
            "metrics": _approach_validation_metrics(members),
        })

    overall = _approach_validation_metrics(enriched)
    latest = sorted(
        enriched,
        key=lambda item: int(item["created_ms"]),
        reverse=True,
    )[:50]
    decision_sample = int(overall["matured"])
    if decision_sample < 10:
        assessment = "INSUFFICIENT"
    elif overall["ready_rate"] is not None and overall["ready_rate"] >= 50:
        assessment = "PROMISING"
    else:
        assessment = "MIXED"

    return {
        "threshold": 70.0,
        "observation_hours": 48,
        "overall": overall,
        "groups": groups,
        "decision_sample": decision_sample,
        "assessment": assessment,
        "latest": latest,
    }


def _opportunity_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        item
        for item in items
        if str((item.get("result") or {}).get("status") or "")
        in {"TP", "SL"}
    ]
    tp = sum(
        1
        for item in usable
        if (item.get("result") or {}).get("status") == "TP"
    )
    sl = sum(
        1
        for item in usable
        if (item.get("result") or {}).get("status") == "SL"
    )
    rs = [
        float((item.get("result") or {}).get("final_r"))
        for item in usable
        if (item.get("result") or {}).get("final_r") is not None
    ]
    mfes = [
        float((item.get("result") or {}).get("mfe_r") or 0)
        for item in items
        if item.get("result")
    ]
    maes = [
        float((item.get("result") or {}).get("mae_r") or 0)
        for item in items
        if item.get("result")
    ]
    return {
        "tracked": len(items),
        "resolved": len(usable),
        "tp": tp,
        "sl": sl,
        "win_rate": round(tp / len(usable) * 100.0, 1) if usable else None,
        "avg_r": round(mean(rs), 3) if rs else None,
        "avg_mfe_r": round(mean(mfes), 3) if mfes else None,
        "avg_mae_r": round(mean(maes), 3) if maes else None,
    }


def _confirmation_outcome_groups(
    limit: int = 1000,
) -> dict[str, Any]:
    events = _load_candidate_events_with_results(limit=limit)
    diagnosed = [
        event
        for event in events
        if event.get("kind") == "NEAR"
        and event.get("confirmation_color_ok") is not None
        and event.get("confirmation_level_ok") is not None
        and event.get("result")
    ]

    groups: list[dict[str, Any]] = []
    for key, label in (
        ("COLOR_ONLY", "ローソク足の色だけ未達"),
        ("LEVEL_ONLY", "ロール水準だけ未達"),
        ("BOTH", "両方未達"),
    ):
        members = [
            event
            for event in diagnosed
            if _confirmation_failure_type(event) == key
        ]
        groups.append({
            "key": key,
            "label": label,
            "metrics": _opportunity_metrics(members),
        })

    resolved = [
        event
        for event in diagnosed
        if str((event.get("result") or {}).get("status") or "")
        in {"TP", "SL"}
    ]
    return {
        "sample_size": len(diagnosed),
        "resolved": len(resolved),
        "groups": groups,
    }


def _opportunity_analysis(limit: int = 500) -> dict[str, Any]:
    events = _load_candidate_events_with_results(limit=limit)
    ordered = sorted(events, key=lambda item: int(item["created_ms"]))

    ready_by_ticker: dict[str, list[int]] = {}
    for event in ordered:
        if event.get("kind") == "READY":
            ready_by_ticker.setdefault(str(event["ticker"]), []).append(
                int(event["created_ms"])
            )

    def later_ready_before(
        event: dict[str, Any],
        end_ms: int | None,
    ) -> bool:
        ticker = str(event["ticker"])
        start_ms = int(event["created_ms"])
        horizon = (
            int(end_ms)
            if end_ms
            else start_ms + 48 * 60 * 60 * 1000
        )
        return any(
            start_ms < ready_ms <= horizon
            for ready_ms in ready_by_ticker.get(ticker, [])
        )

    trackable = [
        event
        for event in ordered
        if event.get("entry") is not None
        and event.get("stop") is not None
        and event.get("target") is not None
        and event.get("result")
    ]
    near = [event for event in trackable if event.get("kind") == "NEAR"]
    ready = [event for event in trackable if event.get("kind") == "READY"]

    near_tp_without_ready = 0
    near_sl_without_ready = 0
    near_became_ready = 0
    near_mfe_2r_without_ready = 0
    latest: list[dict[str, Any]] = []

    for event in reversed(ordered):
        result = event.get("result") or {}
        if not result:
            continue
        end_ms = result.get("outcome_time_ms") or result.get("history_end_ms")
        became_ready = (
            event.get("kind") == "NEAR"
            and later_ready_before(event, end_ms)
        )
        if event.get("kind") == "NEAR" and became_ready:
            near_became_ready += 1

        status = str(result.get("status") or "OPEN")
        if event.get("kind") == "NEAR" and not became_ready:
            if status == "TP":
                near_tp_without_ready += 1
            elif status == "SL":
                near_sl_without_ready += 1
            if float(result.get("mfe_r") or 0) >= 2.0:
                near_mfe_2r_without_ready += 1

        latest.append({
            "id": event["id"],
            "ticker": event["ticker"],
            "kind": event["kind"],
            "label": event["label"],
            "stage": event["stage"],
            "direction": event["direction"],
            "score": event["score"],
            "rr": event["rr"],
            "entry": event["entry"],
            "stop": event["stop"],
            "target": event["target"],
            "created_ms": event["created_ms"],
            "became_ready": became_ready,
            "result": result,
        })
        if len(latest) >= 50:
            break

    return {
        "total_events": len(events),
        "trackable": len(trackable),
        "near": _opportunity_metrics(near),
        "ready": _opportunity_metrics(ready),
        "confirmation_effect": {
            "near_tp_without_ready": near_tp_without_ready,
            "near_sl_without_ready": near_sl_without_ready,
            "near_became_ready": near_became_ready,
            "near_mfe_2r_without_ready": near_mfe_2r_without_ready,
            "sample_size": len(near),
            "decision_sample": (
                near_tp_without_ready
                + near_sl_without_ready
                + near_became_ready
            ),
        },
        "latest": latest,
    }


def _score_breakdown(
    *,
    volume_ratio: float,
    trend: bool,
    breakout: bool,
    retest: bool,
    confirmed: bool,
    rr: float | None,
) -> dict[str, float]:
    return {
        "volume": round(min(10.0, max(0.0, volume_ratio * 4.0)), 1),
        "trend": 20.0 if trend else 0.0,
        "breakout": 20.0 if breakout else 0.0,
        "retest": 20.0 if retest else 0.0,
        "confirmation": 15.0 if confirmed else 0.0,
        "rr": round(min(15.0, rr / SETTINGS.min_rr * 15.0), 1) if rr is not None else 0.0,
    }


def analyze_contract(
    contract: scanner.Contract,
    monitor_raw: list[scanner.Candle],
    entry_raw: list[scanner.Candle],
) -> dict[str, Any]:
    monitor = closed(monitor_raw, SETTINGS.monitor_interval)
    entries = closed(entry_raw, SETTINGS.entry_interval)
    base: dict[str, Any] = {
        "ticker": contract.contract_name,
        "contract_id": contract.contract_id,
        "step_size": contract.step_size,
        "min_order_size": contract.min_order_size,
        "max_order_size": contract.max_order_size,
        "max_long_leverage": contract.max_long_leverage,
        "max_short_leverage": contract.max_short_leverage,
        "stage": "DATA_WAIT",
        "score": 0.0,
        "score_breakdown": _score_breakdown(
            volume_ratio=0,
            trend=False,
            breakout=False,
            retest=False,
            confirmed=False,
            rr=None,
        ),
    }

    required_monitor = max(
        SETTINGS.trend_slow_ema,
        SETTINGS.roll_lookback + SETTINGS.roll_max_age,
        SETTINGS.atr_period + 1,
    )
    required_entry = max(SETTINGS.atr_period + 1, SETTINGS.retest_lookback)
    if len(monitor) < required_monitor or len(entries) < required_entry:
        base["reason"] = f"history insufficient: 4H={len(monitor)} 15M={len(entries)}"
        return base

    latest4 = monitor[-1]
    latest15 = entries[-1]
    now_ms = int(time.time() * 1000)
    base.update({
        "latest_4h_time_ms": latest4.time_ms,
        "latest_15m_time_ms": latest15.time_ms,
        "data_age_seconds": max(
            0,
            int(
                (
                    now_ms
                    - (latest15.time_ms + scanner.INTERVAL_MS[SETTINGS.entry_interval])
                )
                / 1000
            ),
        ),
    })

    ema_fast = scanner._ema([c.close for c in monitor], SETTINGS.trend_fast_ema)
    ema_slow = scanner._ema([c.close for c in monitor], SETTINGS.trend_slow_ema)
    atr4 = scanner._atr(monitor, SETTINGS.atr_period)
    atr15 = scanner._atr(entries, SETTINGS.atr_period)
    if None in {ema_fast, ema_slow, atr4, atr15}:
        base["reason"] = "indicator unavailable"
        return base

    assert ema_fast is not None and ema_slow is not None
    assert atr4 is not None and atr15 is not None

    direction: str | None
    trend: str
    if ema_fast > ema_slow and latest4.close > ema_fast:
        direction, trend = "LONG", "UP"
    elif ema_fast < ema_slow and latest4.close < ema_fast:
        direction, trend = "SHORT", "DOWN"
    else:
        direction, trend = None, "NEUTRAL"

    last96 = entries[-96:]
    change24 = None
    if len(last96) >= 2 and last96[0].close > 0:
        change24 = (latest15.close / last96[0].close - 1.0) * 100.0
    vol_window = entries[-20:]
    avg_value = mean(c.value for c in vol_window) if vol_window else 0.0
    volume_ratio = latest15.value / avg_value if avg_value > 0 else 1.0

    base.update({
        "current_price": latest15.close,
        "change_24h_pct": change24,
        "volume_24h_value": sum(c.value for c in last96),
        "volume_ratio": volume_ratio,
        "trend": trend,
        "direction": direction,
        "ema20_4h": ema_fast,
        "ema50_4h": ema_slow,
        "atr_4h": atr4,
        "atr_15m": atr15,
        "support_4h": min(c.low for c in monitor[-20:]),
        "resistance_4h": max(c.high for c in monitor[-20:]),
    })

    breakdown = _score_breakdown(
        volume_ratio=volume_ratio,
        trend=direction is not None,
        breakout=False,
        retest=False,
        confirmed=False,
        rr=None,
    )
    if direction is None:
        base.update(
            stage="TREND_WAIT",
            score=round(sum(breakdown.values()), 1),
            score_breakdown=breakdown,
            reason="4H trend not aligned",
        )
        return base

    raw_direction = "up" if direction == "LONG" else "down"
    breakout = DETECTOR._recent_breakout(monitor, raw_direction)
    if breakout is None:
        base.update(
            stage="BREAKOUT_WAIT",
            score=round(sum(breakdown.values()), 1),
            score_breakdown=breakdown,
            reason="waiting for recent 4H breakout",
        )
        return base

    breakout_index, roll_level = breakout
    tolerance = max(atr15 * SETTINGS.retest_atr_tolerance, roll_level * 0.001)
    retest_window = entries[-SETTINGS.retest_lookback:]

    if direction == "LONG":
        touched = any(c.low <= roll_level + tolerance for c in retest_window)
        confirmation_color_ok = latest15.close > latest15.open
        confirmation_level_ok = latest15.close > roll_level
        confirmation_body_atr = (
            (latest15.close - latest15.open) / atr15
            if atr15 > 0
            else 0.0
        )
        confirmation_roll_margin_atr = (
            (latest15.close - roll_level) / atr15
            if atr15 > 0
            else 0.0
        )
        confirmed = confirmation_color_ok and confirmation_level_ok
        structural_stop = min(
            roll_level,
            min(c.low for c in monitor[breakout_index:]),
        )
        stop = structural_stop - atr4 * SETTINGS.atr_stop_buffer
        raw_target = max(c.high for c in monitor[breakout_index:])
        target = raw_target - atr4 * SETTINGS.atr_target_buffer
        structure_ok = stop < latest15.close < target
        rr = (
            (target - latest15.close) / (latest15.close - stop)
            if structure_ok
            else None
        )
    else:
        touched = any(c.high >= roll_level - tolerance for c in retest_window)
        confirmation_color_ok = latest15.close < latest15.open
        confirmation_level_ok = latest15.close < roll_level
        confirmation_body_atr = (
            (latest15.open - latest15.close) / atr15
            if atr15 > 0
            else 0.0
        )
        confirmation_roll_margin_atr = (
            (roll_level - latest15.close) / atr15
            if atr15 > 0
            else 0.0
        )
        confirmed = confirmation_color_ok and confirmation_level_ok
        structural_stop = max(
            roll_level,
            max(c.high for c in monitor[breakout_index:]),
        )
        stop = structural_stop + atr4 * SETTINGS.atr_stop_buffer
        raw_target = min(c.low for c in monitor[breakout_index:])
        target = raw_target + atr4 * SETTINGS.atr_target_buffer
        structure_ok = target < latest15.close < stop
        rr = (
            (latest15.close - target) / (stop - latest15.close)
            if structure_ok
            else None
        )

    pre_breakout = monitor[
        max(0, breakout_index - SETTINGS.roll_lookback) : breakout_index
    ]
    stop_valid = (
        stop < latest15.close
        if direction == "LONG"
        else stop > latest15.close
    )
    stop_distance_shadow = (
        abs(latest15.close - stop)
        if stop_valid
        else None
    )
    fixed_2r_target = None
    fixed_2r_rr = None
    measured_target = None
    measured_rr = None
    if stop_distance_shadow is not None:
        fixed_2r_target = latest15.close + (
            2.0 * stop_distance_shadow
            if direction == "LONG"
            else -2.0 * stop_distance_shadow
        )
        fixed_2r_rr = 2.0
        if pre_breakout:
            if direction == "LONG":
                range_floor = min(c.low for c in pre_breakout)
                range_height = max(0.0, roll_level - range_floor)
                measured_target = roll_level + range_height
                if measured_target > latest15.close:
                    measured_rr = (
                        measured_target - latest15.close
                    ) / stop_distance_shadow
            else:
                range_ceiling = max(c.high for c in pre_breakout)
                range_height = max(0.0, range_ceiling - roll_level)
                measured_target = roll_level - range_height
                if measured_target < latest15.close:
                    measured_rr = (
                        latest15.close - measured_target
                    ) / stop_distance_shadow

    shadow_base_ok = bool(touched and confirmed and stop_valid)
    shadow_fixed_2r_ready = bool(
        shadow_base_ok
        and fixed_2r_target is not None
    )
    shadow_measured_ready = bool(
        shadow_base_ok
        and measured_rr is not None
        and measured_rr >= SETTINGS.min_rr
    )

    breakdown = _score_breakdown(
        volume_ratio=volume_ratio,
        trend=True,
        breakout=True,
        retest=touched,
        confirmed=confirmed,
        rr=rr,
    )

    if not touched:
        stage, reason = "RETEST_WAIT", "4H setup exists; waiting for 15M retest"
    elif not confirmed:
        stage, reason = "CONFIRMATION_WAIT", "retest seen; waiting for 15M confirmation"
    elif rr is None:
        stage, reason = (
            "STRUCTURE_WAIT",
            "4H structural target/stop is not valid at current price",
        )
    elif rr < SETTINGS.min_rr:
        stage, reason = (
            "RR_WAIT",
            f"structural RR {rr:.2f} is below {SETTINGS.min_rr:.2f}",
        )
    else:
        stage, reason = (
            "READY",
            "trend + breakout + retest + confirmation + RR >= 2",
        )

    stop_distance = abs(latest15.close - stop) if structure_ok else None
    tp1 = None
    if stop_distance is not None:
        tp1 = latest15.close + (
            2.0 * stop_distance if direction == "LONG" else -2.0 * stop_distance
        )

    base.update({
        "stage": stage,
        "reason": reason,
        "score": round(min(100.0, sum(breakdown.values())), 1),
        "score_breakdown": breakdown,
        "breakout_level": roll_level,
        "breakout_time_ms": monitor[breakout_index].time_ms,
        "retest_touched": touched,
        "confirmed": confirmed,
        "confirmation_color_ok": confirmation_color_ok,
        "confirmation_level_ok": confirmation_level_ok,
        "confirmation_body_atr": round(confirmation_body_atr, 4),
        "confirmation_roll_margin_atr": round(confirmation_roll_margin_atr, 4),
        "confirmation_latest_open": latest15.open,
        "confirmation_latest_close": latest15.close,
        "entry_reference": latest15.close,
        "stop_loss": stop if structure_ok else None,
        "take_profit": target if structure_ok else None,
        "tp1_2r": tp1,
        "rr": rr,
        "current_structural_target": target,
        "stop_valid": stop_valid,
        "shadow_stop_loss": stop if stop_valid else None,
        "shadow_fixed_2r_target": fixed_2r_target,
        "shadow_fixed_2r_rr": fixed_2r_rr,
        "shadow_fixed_2r_ready": shadow_fixed_2r_ready,
        "shadow_measured_target": measured_target,
        "shadow_measured_rr": measured_rr,
        "shadow_measured_ready": shadow_measured_ready,
        "shadow_v2_ready": shadow_measured_ready,
        "shadow_v2_target": fixed_2r_target if shadow_measured_ready else None,
        "shadow_v2_extension_target": measured_target if shadow_measured_ready else None,
        "shadow_v2_room_rr": measured_rr if shadow_measured_ready else None,
    })
    return base


async def market_snapshots(force: bool = False):
    global _snapshot_cache
    if not force and _snapshot_cache and time.time() - _snapshot_cache[0] < 240:
        return _snapshot_cache[1]

    async with _cache_lock:
        if not force and _snapshot_cache and time.time() - _snapshot_cache[0] < 240:
            return _snapshot_cache[1]

        contracts = await CLIENT.get_contracts()
        contract_ids = list(contracts)
        chunks = [
            contract_ids[index : index + 50]
            for index in range(0, len(contract_ids), 50)
        ]
        parts = await asyncio.gather(
            *(fetch_snapshots(chunk, timeout=25.0) for chunk in chunks),
            return_exceptions=True,
        )

        data: dict[tuple[str, str], list[scanner.Candle]] = {}
        for part in parts:
            if isinstance(part, Exception):
                continue
            data.update(part)

        received_contracts = {contract_id for contract_id, _interval in data}
        missing = [
            contract_id
            for contract_id in contract_ids
            if contract_id not in received_contracts
        ]
        if missing:
            try:
                retry = await fetch_snapshots(missing, timeout=20.0)
                data.update(retry)
            except Exception:
                pass

        if not data:
            raise RuntimeError("EdgeX returned no market snapshots")

        _snapshot_cache = (time.time(), data)
        return data


class PushSubscriptionRequest(BaseModel):
    subscription: dict[str, Any]
    candidate_alerts: bool = True
    daily_summary: bool = True
    timezone: str = Field(default="Asia/Tokyo", max_length=64)


class PushUnsubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=10, max_length=4096)


class PushPreferenceRequest(BaseModel):
    endpoint: str = Field(min_length=10, max_length=4096)
    candidate_alerts: bool = True
    daily_summary: bool = True
    snooze_until_ms: int | None = Field(default=None, ge=0)
    quiet_start: str | None = Field(default=None, max_length=5)
    quiet_end: str | None = Field(default=None, max_length=5)


class PushTestRequest(BaseModel):
    endpoint: str = Field(min_length=10, max_length=4096)


class WatchlistSyncRequest(BaseModel):
    sync_key: str = Field(min_length=24, max_length=128)
    tickers: list[str] = Field(default_factory=list, max_length=200)


class CustomAlertCreateRequest(BaseModel):
    endpoint: str = Field(min_length=10, max_length=4096)
    ticker: str = Field(min_length=2, max_length=64)
    condition: str = Field(max_length=32)
    threshold: float = Field(gt=0)


class CustomAlertDeleteRequest(BaseModel):
    endpoint: str = Field(min_length=10, max_length=4096)
    alert_id: int = Field(gt=0)


class RiskRequest(BaseModel):
    equity: float = Field(gt=0)
    risk_pct: float = Field(default=5.0, gt=0, le=100)
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float | None = Field(default=None, gt=0)
    step_size: float | None = Field(default=None, gt=0)
    min_order_size: float | None = Field(default=None, gt=0)
    max_order_size: float | None = Field(default=None, gt=0)
    leverage: float | None = Field(default=None, gt=0)


def floor_step(value: float, step: float | None) -> float:
    if not step or step <= 0:
        return value
    return math.floor((value + step * 1e-9) / step) * step


def risk_plan(req: RiskRequest) -> dict[str, Any]:
    distance = abs(req.entry - req.stop)
    if distance <= 0:
        raise HTTPException(400, "Entry and stop must differ")

    side = "LONG" if req.stop < req.entry else "SHORT"
    if req.target is not None:
        if side == "LONG" and req.target <= req.entry:
            raise HTTPException(400, "LONG target must be above Entry")
        if side == "SHORT" and req.target >= req.entry:
            raise HTTPException(400, "SHORT target must be below Entry")

    budget = req.equity * req.risk_pct / 100.0
    theoretical = budget / distance
    size = floor_step(theoretical, req.step_size)

    max_order_capped = False
    if req.max_order_size and size > req.max_order_size:
        size = floor_step(req.max_order_size, req.step_size)
        max_order_capped = True

    margin_capped = False
    if req.leverage:
        max_by_margin = req.equity * req.leverage / req.entry
        if size > max_by_margin:
            size = floor_step(max_by_margin, req.step_size)
            margin_capped = True

    below_min_order = bool(req.min_order_size and size < req.min_order_size)
    max_loss = size * distance
    rr = None
    target_profit = None
    if req.target is not None:
        reward = abs(req.target - req.entry)
        rr = reward / distance
        target_profit = size * reward

    return {
        "side": side,
        "risk_budget": budget,
        "theoretical_size": theoretical,
        "size": size,
        "notional": size * req.entry,
        "max_loss": max_loss,
        "actual_risk_pct": max_loss / req.equity * 100.0,
        "rr": rr,
        "target_profit": target_profit,
        "max_order_capped": max_order_capped,
        "margin_capped": margin_capped,
        "below_min_order": below_min_order,
        "min_order_size": req.min_order_size,
    }


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {
        "READY": 0,
        "CONFIRMATION_WAIT": 1,
        "RETEST_WAIT": 2,
        "RR_WAIT": 3,
        "BREAKOUT_WAIT": 4,
        "TREND_WAIT": 5,
        "STRUCTURE_WAIT": 6,
        "DATA_WAIT": 7,
    }
    return sorted(
        rows,
        key=lambda row: (
            priority.get(row.get("stage"), 99),
            -float(row.get("score") or 0),
            -float(row.get("volume_24h_value") or 0),
        ),
    )


def _confirmation_failure_type(row: dict[str, Any]) -> str | None:
    if row.get("stage") != "CONFIRMATION_WAIT":
        return None
    color_raw = row.get("confirmation_color_ok")
    level_raw = row.get("confirmation_level_ok")
    if color_raw is None or level_raw is None:
        return "UNKNOWN"
    color_ok = bool(color_raw)
    level_ok = bool(level_raw)
    if color_ok and not level_ok:
        return "LEVEL_ONLY"
    if not color_ok and level_ok:
        return "COLOR_ONLY"
    if not color_ok and not level_ok:
        return "BOTH"
    return "UNKNOWN"


def _readiness_review(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    touched_confirmed = [
        row for row in rows
        if row.get("retest_touched") is True
        and row.get("confirmed") is True
    ]
    current_ready = [
        row for row in rows
        if row.get("stage") == "READY"
    ]
    measured_ready = [
        row for row in rows
        if row.get("shadow_measured_ready") is True
    ]
    fixed_ready = [
        row for row in rows
        if row.get("shadow_fixed_2r_ready") is True
    ]

    def pack(items: list[dict[str, Any]], rr_key: str, target_key: str):
        return [
            {
                "ticker": row.get("ticker"),
                "direction": row.get("direction"),
                "score": row.get("score"),
                "entry": row.get("entry_reference"),
                "stop": row.get("shadow_stop_loss")
                if rr_key.startswith("shadow_")
                else row.get("stop_loss"),
                "target": row.get(target_key),
                "rr": row.get(rr_key),
                "current_stage": row.get("stage"),
                "current_rr": row.get("rr"),
                "reason": row.get("reason"),
            }
            for row in sorted(
                items,
                key=lambda x: (
                    -float(x.get(rr_key) or 0),
                    -float(x.get("score") or 0),
                ),
            )
        ]

    return {
        "confirmed_after_retest": len(touched_confirmed),
        "current": {
            "ready_count": len(current_ready),
            "items": pack(
                current_ready,
                "rr",
                "current_structural_target",
            ),
        },
        "measured_move": {
            "ready_count": len(measured_ready),
            "items": pack(
                measured_ready,
                "shadow_measured_rr",
                "shadow_measured_target",
            ),
        },
        "fixed_2r": {
            "ready_count": len(fixed_ready),
            "items": pack(
                fixed_ready,
                "shadow_fixed_2r_rr",
                "shadow_fixed_2r_target",
            ),
        },
        "proposed_v2": {
            "rule": "measured-move room >= 2R; first target = 2R",
            "ready_count": len(measured_ready),
            "items": [
                {
                    "ticker": row.get("ticker"),
                    "direction": row.get("direction"),
                    "score": row.get("score"),
                    "entry": row.get("entry_reference"),
                    "stop": row.get("shadow_stop_loss"),
                    "target_2r": row.get("shadow_v2_target"),
                    "extension_target": row.get("shadow_v2_extension_target"),
                    "room_rr": row.get("shadow_v2_room_rr"),
                    "current_stage": row.get("stage"),
                    "current_rr": row.get("rr"),
                }
                for row in measured_ready
            ],
        },
    }


def _confirmation_diagnostics(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    touched = [
        row
        for row in rows
        if row.get("retest_touched") is True
        and row.get("direction") in {"LONG", "SHORT"}
    ]
    confirmed = [row for row in touched if row.get("confirmed") is True]
    waiting = [
        row for row in touched
        if row.get("stage") == "CONFIRMATION_WAIT"
    ]
    failures = Counter(
        _confirmation_failure_type(row) or "UNKNOWN"
        for row in waiting
    )

    def avg_field(name: str) -> float | None:
        vals = [
            float(row[name])
            for row in waiting
            if row.get(name) is not None
        ]
        return round(mean(vals), 4) if vals else None

    closest = sorted(
        waiting,
        key=lambda row: (
            max(0.0, -float(row.get("confirmation_body_atr") or 0.0))
            + max(
                0.0,
                -float(row.get("confirmation_roll_margin_atr") or 0.0),
            ),
            -float(row.get("score") or 0),
        ),
    )[:10]

    return {
        "touched_count": len(touched),
        "confirmed_count": len(confirmed),
        "confirmation_wait_count": len(waiting),
        "pass_rate_pct": (
            round(len(confirmed) / len(touched) * 100.0, 1)
            if touched
            else None
        ),
        "failure_counts": dict(failures),
        "avg_body_atr": avg_field("confirmation_body_atr"),
        "avg_roll_margin_atr": avg_field("confirmation_roll_margin_atr"),
        "closest": [
            {
                "ticker": row.get("ticker"),
                "direction": row.get("direction"),
                "score": row.get("score"),
                "rr": row.get("rr"),
                "color_ok": row.get("confirmation_color_ok"),
                "level_ok": row.get("confirmation_level_ok"),
                "body_atr": row.get("confirmation_body_atr"),
                "roll_margin_atr": row.get(
                    "confirmation_roll_margin_atr"
                ),
                "current_price": row.get("current_price"),
                "breakout_level": row.get("breakout_level"),
            }
            for row in closest
        ],
    }


def _market_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    stages = Counter(str(row.get("stage") or "UNKNOWN") for row in rows)
    directions = Counter(str(row.get("direction") or "NEUTRAL") for row in rows)
    ready = [row for row in rows if row.get("stage") == "READY"]
    near = [
        row
        for row in rows
        if row.get("stage") in {"CONFIRMATION_WAIT", "RETEST_WAIT"}
    ]
    qualified_near = [
        row
        for row in near
        if row.get("rr") is not None
        and float(row["rr"]) >= SETTINGS.min_rr
    ]
    return {
        "stage_counts": dict(stages),
        "direction_counts": dict(directions),
        "ready_count": len(ready),
        "near_signal_count": len(near),
        "qualified_near_count": len(qualified_near),
        "average_score": (
            round(mean(float(row.get("score") or 0) for row in rows), 1)
            if rows
            else 0.0
        ),
        "top_ready": [row["ticker"] for row in _sort_rows(ready)[:5]],
        "top_near": [row["ticker"] for row in _sort_rows(near)[:5]],
        "top_qualified_near": [
            row["ticker"] for row in _sort_rows(qualified_near)[:5]
        ],
    }


def _score_band(value: float) -> str:
    if value >= 90:
        return "90+"
    if value >= 80:
        return "80-89"
    if value >= 70:
        return "70-79"
    if value >= 60:
        return "60-69"
    return "<60"


def _priority_evidence_index() -> dict[tuple[str, str], dict[str, Any]]:
    events = _load_candidate_events_with_results(limit=1000)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        result = event.get("result")
        if not result:
            continue
        direction = str(event.get("direction") or "")
        if direction not in {"LONG", "SHORT"}:
            continue
        key = (
            direction,
            _score_band(float(event.get("score") or 0)),
        )
        grouped.setdefault(key, []).append(event)

    index: dict[tuple[str, str], dict[str, Any]] = {}
    for key, items in grouped.items():
        mfes = [
            float((item.get("result") or {}).get("mfe_r") or 0)
            for item in items
        ]
        maes = [
            float((item.get("result") or {}).get("mae_r") or 0)
            for item in items
        ]
        resolved = [
            item
            for item in items
            if str((item.get("result") or {}).get("status") or "")
            in {"TP", "SL"}
        ]
        final_rs = [
            float((item.get("result") or {}).get("final_r"))
            for item in resolved
            if (item.get("result") or {}).get("final_r") is not None
        ]
        index[key] = {
            "n": len(items),
            "resolved_n": len(resolved),
            "avg_mfe_r": round(mean(mfes), 3) if mfes else None,
            "avg_mae_r": round(mean(maes), 3) if maes else None,
            "avg_r": round(mean(final_rs), 3) if final_rs else None,
        }
    return index


def _priority_stage_points(stage: str) -> float:
    return {
        "READY": 35.0,
        "CONFIRMATION_WAIT": 31.0,
        "RETEST_WAIT": 26.0,
        "BREAKOUT_WAIT": 18.0,
        "RR_WAIT": 12.0,
        "STRUCTURE_WAIT": 8.0,
        "TREND_WAIT": 4.0,
        "DATA_WAIT": 0.0,
    }.get(stage, 0.0)


def _priority_freshness_points(age_seconds: float | None) -> float:
    if age_seconds is None:
        return 0.0
    age = max(0.0, float(age_seconds))
    if age <= 90:
        return 10.0
    if age <= 300:
        return 8.0
    if age <= 900:
        return 5.0
    if age <= 3600:
        return 2.0
    return 0.0


def _priority_evidence_points(
    row: dict[str, Any],
    evidence_index: dict[tuple[str, str], dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    direction = str(row.get("direction") or "")
    band = _score_band(float(row.get("score") or 0))
    evidence = evidence_index.get((direction, band))
    if not evidence:
        return 5.0, {
            "n": 0,
            "resolved_n": 0,
            "confidence": 0.0,
            "confidence_label": "NO_SAMPLE",
            "avg_mfe_r": None,
            "avg_mae_r": None,
            "avg_r": None,
        }

    n = int(evidence.get("n") or 0)
    confidence = min(1.0, n / 30.0)
    avg_mfe = float(evidence.get("avg_mfe_r") or 0)
    avg_mae = float(evidence.get("avg_mae_r") or 0)
    edge_proxy = max(-1.0, min(1.0, (avg_mfe - avg_mae - 0.5) / 1.5))
    points = max(0.0, min(10.0, 5.0 + 5.0 * edge_proxy * confidence))
    label = "LOW" if n < 10 else "EARLY" if n < 30 else "OK"
    detail = dict(evidence)
    detail["confidence"] = round(confidence, 3)
    detail["confidence_label"] = label
    return round(points, 1), detail


def _priority_ranking(
    rows: list[dict[str, Any]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    evidence_index = _priority_evidence_index()
    ranked: list[dict[str, Any]] = []
    for row in rows:
        stage = str(row.get("stage") or "")
        stage_points = _priority_stage_points(stage)
        setup_points = min(
            30.0,
            max(0.0, float(row.get("score") or 0) * 0.30),
        )
        rr = row.get("rr")
        rr_points = (
            min(
                15.0,
                max(
                    0.0,
                    float(rr) / max(float(SETTINGS.min_rr), 0.01) * 10.0,
                ),
            )
            if rr is not None
            else 0.0
        )
        freshness_points = _priority_freshness_points(
            row.get("data_age_seconds")
        )
        evidence_points, evidence = _priority_evidence_points(
            row,
            evidence_index,
        )
        total = (
            stage_points
            + setup_points
            + rr_points
            + freshness_points
            + evidence_points
        )
        ranked.append({
            "ticker": row.get("ticker"),
            "stage": stage,
            "direction": row.get("direction"),
            "score": row.get("score"),
            "rr": row.get("rr"),
            "current_price": row.get("current_price"),
            "data_age_seconds": row.get("data_age_seconds"),
            "entry_reference": row.get("entry_reference"),
            "stop_loss": row.get("stop_loss"),
            "take_profit": row.get("take_profit"),
            "reason": row.get("reason"),
            "priority_score": round(total, 1),
            "priority_breakdown": {
                "stage": round(stage_points, 1),
                "setup": round(setup_points, 1),
                "rr": round(rr_points, 1),
                "freshness": round(freshness_points, 1),
                "evidence": round(evidence_points, 1),
            },
            "evidence": evidence,
        })

    ranked.sort(
        key=lambda item: (
            -float(item["priority_score"]),
            -float(item.get("score") or 0),
            -float(item.get("rr") or 0),
        )
    )
    return ranked[: max(1, min(limit, 50))]


def _daily_picks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _priority_ranking(rows, limit=3)


def _save_priority_snapshot(
    bucket_ms: int,
    ranking: list[dict[str, Any]],
) -> None:
    now_ms = int(time.time() * 1000)
    payload = [
        {
            "ticker": item.get("ticker"),
            "rank": index + 1,
            "priority_score": item.get("priority_score"),
            "approach_score": item.get("approach_score"),
            "stage": item.get("stage"),
            "direction": item.get("direction"),
            "rr": item.get("rr"),
        }
        for index, item in enumerate(ranking)
    ]
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO priority_snapshots(bucket_ms, payload, created_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(bucket_ms) DO UPDATE SET
                payload=excluded.payload,
                created_ms=excluded.created_ms
            """,
            (
                int(bucket_ms),
                json.dumps(payload, separators=(",", ":")),
                now_ms,
            ),
        )
        cutoff = now_ms - 30 * 24 * 60 * 60 * 1000
        conn.execute(
            "DELETE FROM priority_snapshots WHERE bucket_ms < ?",
            (cutoff,),
        )
        conn.execute(
            "DELETE FROM priority_events WHERE created_ms < ?",
            (cutoff,),
        )
        conn.commit()


def _load_previous_priority_snapshot(
    bucket_ms: int,
) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        row = conn.execute(
            """
            SELECT payload
            FROM priority_snapshots
            WHERE bucket_ms < ?
            ORDER BY bucket_ms DESC
            LIMIT 1
            """,
            (int(bucket_ms),),
        ).fetchone()
    if row is None:
        return []
    try:
        value = json.loads(row["payload"])
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _log_priority_event(
    *,
    bucket_ms: int,
    ticker: str,
    event_type: str,
    previous_rank: int | None,
    current_rank: int,
    priority_score: float,
    stage: str,
    direction: str | None,
    rr: float | None,
) -> None:
    now_ms = int(time.time() * 1000)
    rank_change = (
        int(previous_rank) - int(current_rank)
        if previous_rank is not None
        else None
    )
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO priority_events(
                bucket_ms, ticker, event_type, previous_rank, current_rank,
                rank_change, priority_score, stage, direction, rr, created_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(bucket_ms),
                ticker,
                event_type,
                previous_rank,
                int(current_rank),
                rank_change,
                float(priority_score),
                stage,
                direction,
                rr,
                now_ms,
            ),
        )
        conn.commit()


def _load_priority_events(limit: int = 100) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, bucket_ms, ticker, event_type, previous_rank,
                   current_rank, rank_change, priority_score, stage,
                   direction, rr, created_ms
            FROM priority_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
    return [dict(row) for row in rows]


def _priority_event_stage_ja(stage: str) -> str:
    return {
        "READY": "エントリー可能",
        "CONFIRMATION_WAIT": "15分足確認待ち",
        "RETEST_WAIT": "リテスト待ち",
    }.get(stage, stage)


def _approach_readiness_points(stage: str) -> float:
    return {
        "READY": 30.0,
        "CONFIRMATION_WAIT": 28.0,
        "RETEST_WAIT": 22.0,
        "BREAKOUT_WAIT": 12.0,
        "RR_WAIT": 8.0,
        "STRUCTURE_WAIT": 6.0,
        "TREND_WAIT": 2.0,
        "DATA_WAIT": 0.0,
    }.get(stage, 0.0)


def _approach_score(
    item: dict[str, Any],
    *,
    current_rank: int,
    previous_rank: int | None,
) -> dict[str, Any]:
    stage = str(item.get("stage") or "")
    readiness = _approach_readiness_points(stage)

    rank_change = (
        int(previous_rank) - int(current_rank)
        if previous_rank is not None
        else 0
    )
    velocity = min(25.0, max(0.0, float(rank_change) * 5.0))

    rr = item.get("rr")
    rr_points = (
        min(
            15.0,
            max(
                0.0,
                float(rr) / max(float(SETTINGS.min_rr), 0.01) * 10.0,
            ),
        )
        if rr is not None
        else 0.0
    )

    evidence = item.get("evidence") or {}
    n = int(evidence.get("n") or 0)
    confidence = min(1.0, n / 30.0)
    avg_mfe = evidence.get("avg_mfe_r")
    if avg_mfe is None:
        mfe_points = 10.0
    else:
        normalized = min(1.0, max(0.0, float(avg_mfe) / 2.0))
        mfe_points = 10.0 + 10.0 * normalized * confidence

    freshness = min(
        10.0,
        max(
            0.0,
            _priority_freshness_points(item.get("data_age_seconds")),
        ),
    )

    total = readiness + velocity + rr_points + mfe_points + freshness
    return {
        "approach_score": round(min(100.0, total), 1),
        "approach_breakdown": {
            "readiness": round(readiness, 1),
            "velocity": round(velocity, 1),
            "rr": round(rr_points, 1),
            "mfe": round(mfe_points, 1),
            "freshness": round(freshness, 1),
        },
        "rank_change": rank_change if previous_rank is not None else None,
        "previous_rank": previous_rank,
        "current_rank": current_rank,
    }


def _log_approach_event(
    *,
    bucket_ms: int,
    ticker: str,
    previous_score: float | None,
    current_score: float,
    current_rank: int,
    stage: str,
    direction: str | None,
    rr: float | None,
    priority_score: float | None,
    entry: float | None,
    stop: float | None,
    target: float | None,
) -> None:
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO approach_events(
                bucket_ms, ticker, previous_score, current_score,
                current_rank, stage, direction, rr, priority_score,
                entry, stop, target, created_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(bucket_ms),
                ticker,
                previous_score,
                float(current_score),
                int(current_rank),
                stage,
                direction,
                rr,
                priority_score,
                entry,
                stop,
                target,
                now_ms,
            ),
        )
        cutoff = now_ms - 30 * 24 * 60 * 60 * 1000
        conn.execute(
            "DELETE FROM approach_events WHERE created_ms < ?",
            (cutoff,),
        )
        conn.commit()


def _load_approach_events(limit: int = 100) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, bucket_ms, ticker, previous_score, current_score,
                   current_rank, stage, direction, rr, priority_score,
                   entry, stop, target, created_ms
            FROM approach_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
    return [dict(row) for row in rows]


def _upsert_approach_event_result(
    event_id: int,
    result: dict[str, Any],
) -> None:
    now_ms = int(time.time() * 1000)
    with _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO approach_event_results(event_id, payload, updated_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                payload=excluded.payload,
                updated_ms=excluded.updated_ms
            """,
            (
                int(event_id),
                json.dumps(result, separators=(",", ":")),
                now_ms,
            ),
        )
        conn.commit()


def _load_approach_events_with_results(
    limit: int = 500,
) -> list[dict[str, Any]]:
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                e.id, e.bucket_ms, e.ticker, e.previous_score,
                e.current_score, e.current_rank, e.stage, e.direction,
                e.rr, e.priority_score, e.entry, e.stop, e.target,
                e.created_ms, r.payload AS result_payload,
                r.updated_ms AS result_updated_ms
            FROM approach_events e
            LEFT JOIN approach_event_results r ON r.event_id = e.id
            ORDER BY e.id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 1000)),),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw = item.pop("result_payload", None)
        item["result"] = json.loads(raw) if raw else None
        items.append(item)
    return items


async def _process_priority_changes(
    rows: list[dict[str, Any]],
    bucket_ms: int,
) -> None:
    ranking = _priority_ranking(rows, limit=20)
    previous = _load_previous_priority_snapshot(bucket_ms)
    previous_map = {
        str(item.get("ticker") or ""): item
        for item in previous
        if item.get("ticker")
    }

    enriched_ranking: list[dict[str, Any]] = []
    for current_rank, item in enumerate(ranking, start=1):
        ticker = str(item.get("ticker") or "")
        previous_item = previous_map.get(ticker) or {}
        previous_rank = (
            int(previous_item.get("rank"))
            if previous_item.get("rank") is not None
            else None
        )
        approach = _approach_score(
            item,
            current_rank=current_rank,
            previous_rank=previous_rank,
        )
        enriched = dict(item)
        enriched.update(approach)
        enriched_ranking.append(enriched)

    if not previous:
        _save_priority_snapshot(bucket_ms, enriched_ranking)
        return

    for current_rank, item in enumerate(enriched_ranking, start=1):
        ticker = str(item.get("ticker") or "")
        stage = str(item.get("stage") or "")
        priority_score = float(item.get("priority_score") or 0)
        approach_score = float(item.get("approach_score") or 0)
        rr = item.get("rr")
        previous_item = previous_map.get(ticker) or {}
        previous_rank = (
            int(previous_item.get("rank"))
            if previous_item.get("rank") is not None
            else None
        )
        previous_approach = previous_item.get("approach_score")
        if previous_approach is not None:
            previous_approach = float(previous_approach)

        actionable = stage in {
            "READY",
            "CONFIRMATION_WAIT",
            "RETEST_WAIT",
        }
        rr_ok = (
            stage == "READY"
            or (
                rr is not None
                and float(rr) >= float(SETTINGS.min_rr)
            )
        )

        event_type: str | None = None
        if actionable and rr_ok and priority_score >= 65.0:
            if current_rank <= 3 and (
                previous_rank is None
                or previous_rank > 3
            ):
                event_type = "TOP3_ENTRY"
            elif (
                previous_rank is not None
                and previous_rank - current_rank >= 5
                and current_rank <= 10
            ):
                event_type = "RANK_SURGE"

        if event_type is not None:
            _log_priority_event(
                bucket_ms=bucket_ms,
                ticker=ticker,
                event_type=event_type,
                previous_rank=previous_rank,
                current_rank=current_rank,
                priority_score=priority_score,
                stage=stage,
                direction=item.get("direction"),
                rr=float(rr) if rr is not None else None,
            )

            if event_type == "TOP3_ENTRY":
                movement = (
                    f"{previous_rank}位→{current_rank}位"
                    if previous_rank is not None
                    else f"新規TOP{current_rank}"
                )
                title = "EdgeX TOP3入り"
            else:
                movement = f"{previous_rank}位→{current_rank}位"
                title = "EdgeX ランキング急上昇"

            await _broadcast_push(
                {
                    "title": title,
                    "body": (
                        f"{ticker} {movement}\n"
                        f"{_priority_event_stage_ja(stage)} / "
                        f"優先度 {priority_score:.1f}点"
                        + (
                            f" / RR {float(rr):.2f}"
                            if rr is not None
                            else ""
                        )
                    ),
                    "url": f"/?tab=analysis&ticker={ticker}",
                    "tag": f"edgex-rank-{ticker}-{bucket_ms}",
                },
                kind="candidate",
            )

        approach_cross = (
            actionable
            and rr_ok
            and current_rank > 3
            and current_rank <= 15
            and previous_approach is not None
            and previous_approach < 70.0
            and approach_score >= 70.0
        )
        if approach_cross:
            _log_approach_event(
                bucket_ms=bucket_ms,
                ticker=ticker,
                previous_score=previous_approach,
                current_score=approach_score,
                current_rank=current_rank,
                stage=stage,
                direction=item.get("direction"),
                rr=float(rr) if rr is not None else None,
                priority_score=priority_score,
                entry=item.get("entry_reference"),
                stop=item.get("stop_loss"),
                target=item.get("take_profit"),
            )
            await _broadcast_push(
                {
                    "title": "EdgeX 急接近候補",
                    "body": (
                        f"{ticker} 急接近度 "
                        f"{previous_approach:.1f}→{approach_score:.1f}点\n"
                        f"現在{current_rank}位 / "
                        f"{_priority_event_stage_ja(stage)}"
                        + (
                            f" / RR {float(rr):.2f}"
                            if rr is not None
                            else ""
                        )
                    ),
                    "url": f"/?tab=analysis&ticker={ticker}",
                    "tag": f"edgex-approach-{ticker}-{bucket_ms}",
                },
                kind="candidate",
            )

    _save_priority_snapshot(bucket_ms, enriched_ranking)


def _priority_rank_changes(
    ranking: list[dict[str, Any]],
    bucket_ms: int,
) -> list[dict[str, Any]]:
    previous = _load_previous_priority_snapshot(bucket_ms)
    previous_map = {
        str(item.get("ticker") or ""): item
        for item in previous
        if item.get("ticker")
    }
    output: list[dict[str, Any]] = []
    for current_rank, item in enumerate(ranking, start=1):
        enriched = dict(item)
        previous_item = previous_map.get(str(item.get("ticker") or "")) or {}
        previous_rank = (
            int(previous_item.get("rank"))
            if previous_item.get("rank") is not None
            else None
        )
        enriched.update(
            _approach_score(
                item,
                current_rank=current_rank,
                previous_rank=previous_rank,
            )
        )
        enriched["previous_approach_score"] = (
            float(previous_item.get("approach_score"))
            if previous_item.get("approach_score") is not None
            else None
        )
        output.append(enriched)
    return output


def _early_watchlist(
    ranking: list[dict[str, Any]],
    limit: int = 8,
) -> list[dict[str, Any]]:
    candidates = [
        item
        for item in ranking
        if str(item.get("stage") or "")
        in {"CONFIRMATION_WAIT", "RETEST_WAIT"}
        and item.get("rr") is not None
        and float(item["rr"]) >= float(SETTINGS.min_rr)
        and int(item.get("current_rank") or 999) > 3
    ]
    candidates.sort(
        key=lambda item: (
            -float(item.get("approach_score") or 0),
            int(item.get("current_rank") or 999),
            -float(item.get("priority_score") or 0),
        )
    )
    return candidates[: max(1, min(limit, 20))]




def _market_regime(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _market_summary(rows)
    directions = summary["direction_counts"]
    long_count = int(directions.get("LONG", 0))
    short_count = int(directions.get("SHORT", 0))
    directional = long_count + short_count
    bias = ((long_count - short_count) / directional) if directional else 0.0
    if bias >= 0.15:
        bias_code = "LONG_BIASED"
    elif bias <= -0.15:
        bias_code = "SHORT_BIASED"
    else:
        bias_code = "BALANCED"

    if summary["ready_count"] > 0:
        activity_code = "SIGNAL_ACTIVE"
    elif summary["qualified_near_count"] >= 3:
        activity_code = "SETUP_BUILDING"
    elif summary["average_score"] < 25:
        activity_code = "QUIET"
    else:
        activity_code = "SELECTIVE"

    return {
        "bias": bias_code,
        "activity": activity_code,
        "bias_pct": round(bias * 100.0, 1),
        "ready_count": summary["ready_count"],
        "qualified_near_count": summary["qualified_near_count"],
        "average_score": summary["average_score"],
    }


async def _scan_market_rows(
    force: bool = False,
) -> tuple[dict[str, scanner.Contract], list[dict[str, Any]]]:
    contracts = await CLIENT.get_contracts()
    data = await market_snapshots(force=force)
    rows: list[dict[str, Any]] = []
    for cid, contract in contracts.items():
        row = analyze_contract(
            contract,
            data.get((cid, SETTINGS.monitor_interval), []),
            data.get((cid, SETTINGS.entry_interval), []),
        )
        if row.get("current_price") is not None:
            rows.append(row)
    return contracts, rows


def _persist_scan_result(
    contracts: dict[str, scanner.Contract],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    bucket_ms = (now_ms // scanner.INTERVAL_MS[SETTINGS.entry_interval]) * scanner.INTERVAL_MS[SETTINGS.entry_interval]
    summary = _market_summary(rows)
    confirmation = _confirmation_diagnostics(rows)
    payload = {
        "time_ms": bucket_ms,
        "universe": len(contracts),
        "scanned": len(rows),
        "coverage_pct": round(len(rows) / len(contracts) * 100.0, 1) if contracts else 0.0,
        "ready": summary["ready_count"],
        "near": summary["near_signal_count"],
        "qualified_near": summary["qualified_near_count"],
        "avg_score": summary["average_score"],
        "long": summary["direction_counts"].get("LONG", 0),
        "short": summary["direction_counts"].get("SHORT", 0),
        "neutral": summary["direction_counts"].get("NEUTRAL", 0),
        "stages": summary["stage_counts"],
        "top_ready": summary["top_ready"],
        "top_near": summary["top_near"],
        "top_qualified_near": summary["top_qualified_near"],
        "confirmation": {
            "touched_count": confirmation["touched_count"],
            "confirmed_count": confirmation["confirmed_count"],
            "confirmation_wait_count": confirmation["confirmation_wait_count"],
            "pass_rate_pct": confirmation["pass_rate_pct"],
            "failure_counts": confirmation["failure_counts"],
            "avg_body_atr": confirmation["avg_body_atr"],
            "avg_roll_margin_atr": confirmation["avg_roll_margin_atr"],
        },
    }
    _save_market_snapshot(payload)

    for row in rows:
        if row.get("stage") != "READY":
            continue
        source_ms = int(row.get("latest_15m_time_ms") or 0)
        direction = str(row.get("direction") or "")
        if not source_ms or direction not in {"LONG", "SHORT"}:
            continue
        signal = {
            "key": f"{row['ticker']}|{source_ms}|{direction}",
            "ticker": row["ticker"],
            "side": direction,
            "action": "AUTO ENTER",
            "stage": "READY",
            "entry": row.get("entry_reference"),
            "stop": row.get("stop_loss"),
            "tp1": row.get("tp1_2r"),
            "target": row.get("take_profit"),
            "rr": row.get("rr"),
            "score": row.get("score"),
            "source_candle_ms": source_ms,
            "created_ms": source_ms + scanner.INTERVAL_MS[SETTINGS.entry_interval] + 1,
            "eligible": True,
            "auto": True,
            "source": "server-background",
        }
        if signal["entry"] is not None and signal["stop"] is not None and signal["target"] is not None:
            _insert_paper_signal(signal)
    return payload


async def _background_collector() -> None:
    last_bucket: int | None = None
    await asyncio.sleep(5)
    while True:
        now_ms = int(time.time() * 1000)
        bucket = (now_ms // scanner.INTERVAL_MS[SETTINGS.entry_interval]) * scanner.INTERVAL_MS[SETTINGS.entry_interval]
        if bucket != last_bucket:
            try:
                contracts, rows = await _scan_market_rows(force=True)
                _persist_scan_result(contracts, rows)
                _persist_shadow_v2_signals(rows)
                await _process_priority_changes(rows, bucket)
                await _maybe_push_candidate_changes(rows)
                await _evaluate_custom_alerts(rows)
                await _refresh_paper_signal_results(contracts)
                await _refresh_shadow_v2_results(contracts)
                await _refresh_candidate_event_results(contracts)
                await _refresh_approach_event_results(contracts)
                _maybe_generate_daily_report()
                await _maybe_push_daily_summary(rows)
                last_bucket = bucket
            except Exception as exc:
                print(f"Background analysis collector error: {exc}", flush=True)
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _background_task
    _init_db()
    _background_task = asyncio.create_task(_background_collector())
    try:
        yield
    finally:
        if _background_task is not None:
            _background_task.cancel()
            try:
                await _background_task
            except asyncio.CancelledError:
                pass
            _background_task = None


app = FastAPI(
    title="EdgeX Analysis Terminal",
    version="19.0.1",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    history = _load_market_history(hours=48)
    paper = _load_paper_signals(limit=1000)
    return {
        "ok": True,
        "service": "edgex-analysis-terminal",
        "version": "19.0.1",
        "time_ms": int(time.time() * 1000),
        "storage": {
            "market_snapshots_48h": len(history),
            "paper_signals": len(paper),
            "db_exists": DB_PATH.exists(),
        },
        "push": {
            "enabled": _push_enabled(),
            "subscribers": _subscription_count(),
            "daily_summary_hour_jst": DAILY_SUMMARY_HOUR_JST,
        },
    }


@app.get("/api/contracts")
async def contracts_api():
    try:
        contracts = await CLIENT.get_contracts()
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        "count": len(contracts),
        "contracts": [contract.__dict__ for contract in contracts.values()],
    }


@app.get("/api/analyze")
async def analyze_api(ticker: str = Query(min_length=2, max_length=64)):
    ticker = ticker.strip().upper()
    try:
        contracts = await CLIENT.get_contracts()
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc

    contract = next(
        (c for c in contracts.values() if c.contract_name.upper() == ticker),
        None,
    )
    if contract is None:
        bare = ticker.removesuffix("USDC")
        contract = next(
            (
                c
                for c in contracts.values()
                if c.contract_name.upper().removesuffix("USDC") == bare
            ),
            None,
        )
    if contract is None:
        raise HTTPException(404, f"Ticker not found on EdgeX: {ticker}")

    try:
        data = await fetch_snapshots([contract.contract_id], timeout=20.0)
    except Exception as exc:
        raise HTTPException(502, f"EdgeX WebSocket failed: {exc}") from exc

    return analyze_contract(
        contract,
        data.get((contract.contract_id, SETTINGS.monitor_interval), []),
        data.get((contract.contract_id, SETTINGS.entry_interval), []),
    )


def _resolve_contract(
    contracts: dict[str, scanner.Contract],
    ticker: str,
) -> scanner.Contract | None:
    ticker = ticker.strip().upper()
    bare = ticker.removesuffix("USDC")
    return next(
        (
            contract
            for contract in contracts.values()
            if contract.contract_name.upper() == ticker
            or contract.contract_name.upper().removesuffix("USDC") == bare
        ),
        None,
    )


def _ema_series(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        return [None for _ in values]
    alpha = 2.0 / (period + 1.0)
    result: list[float | None] = []
    ema: float | None = None
    for index, value in enumerate(values):
        if index + 1 < period:
            result.append(None)
            continue
        if ema is None:
            ema = mean(values[index + 1 - period : index + 1])
        else:
            ema = value * alpha + ema * (1.0 - alpha)
        result.append(ema)
    return result


def _chart_candles(
    candles: list[scanner.Candle],
    interval: str,
    limit: int,
) -> list[dict[str, Any]]:
    all_items = closed(candles, interval)
    closes = [candle.close for candle in all_items]
    ema20_all = _ema_series(closes, SETTINGS.trend_fast_ema)
    ema50_all = _ema_series(closes, SETTINGS.trend_slow_ema)
    start = max(0, len(all_items) - limit)
    items = all_items[start:]
    return [
        {
            "time_ms": candle.time_ms,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.value,
            "ema20": ema20_all[start + index],
            "ema50": ema50_all[start + index],
        }
        for index, candle in enumerate(items)
    ]


def _action_for_stage(stage: str) -> str:
    return {
        "READY": "ENTER",
        "CONFIRMATION_WAIT": "WAIT FOR 15M CLOSE",
        "RETEST_WAIT": "WAIT FOR RETEST",
        "BREAKOUT_WAIT": "WAIT FOR BREAKOUT",
        "RR_WAIT": "SKIP",
        "STRUCTURE_WAIT": "SKIP",
        "TREND_WAIT": "SKIP",
        "DATA_WAIT": "SKIP",
    }.get(stage, "SKIP")


@app.get("/api/chart")
async def chart_api(
    ticker: str = Query(min_length=2, max_length=64),
    limit_4h: int = Query(default=80, ge=30, le=160),
    limit_15m: int = Query(default=120, ge=40, le=240),
):
    try:
        contracts = await CLIENT.get_contracts()
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc

    contract = _resolve_contract(contracts, ticker)
    if contract is None:
        raise HTTPException(404, f"Ticker not found on EdgeX: {ticker.strip().upper()}")

    try:
        data = await fetch_snapshots([contract.contract_id], timeout=22.0)
    except Exception as exc:
        raise HTTPException(502, f"EdgeX WebSocket failed: {exc}") from exc

    monitor_raw = data.get((contract.contract_id, SETTINGS.monitor_interval), [])
    entry_raw = data.get((contract.contract_id, SETTINGS.entry_interval), [])
    analysis = analyze_contract(contract, monitor_raw, entry_raw)
    analysis["action"] = _action_for_stage(str(analysis.get("stage") or ""))

    return {
        "ticker": contract.contract_name,
        "analysis": analysis,
        "series": {
            "HOUR_4": _chart_candles(
                monitor_raw,
                SETTINGS.monitor_interval,
                limit_4h,
            ),
            "MINUTE_15": _chart_candles(
                entry_raw,
                SETTINGS.entry_interval,
                limit_15m,
            ),
        },
        "levels": {
            "breakout": analysis.get("breakout_level"),
            "entry": analysis.get("entry_reference"),
            "stop": analysis.get("stop_loss"),
            "tp1": analysis.get("tp1_2r"),
            "target": analysis.get("take_profit"),
            "support": analysis.get("support_4h"),
            "resistance": analysis.get("resistance_4h"),
        },
    }


@app.get("/api/compare")
async def compare_api(tickers: str = Query(min_length=2, max_length=400)):
    requested = [
        item.strip().upper()
        for item in tickers.split(",")
        if item.strip()
    ]
    requested = list(dict.fromkeys(requested))[:6]
    if len(requested) < 2:
        raise HTTPException(400, "Select at least two tickers")

    try:
        contracts = await CLIENT.get_contracts()
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc

    resolved: list[scanner.Contract] = []
    missing: list[str] = []
    for ticker in requested:
        bare = ticker.removesuffix("USDC")
        contract = next(
            (
                c
                for c in contracts.values()
                if c.contract_name.upper() == ticker
                or c.contract_name.upper().removesuffix("USDC") == bare
            ),
            None,
        )
        if contract is None:
            missing.append(ticker)
        else:
            resolved.append(contract)
    if missing:
        raise HTTPException(
            404,
            f"Ticker not found on EdgeX: {', '.join(missing)}",
        )

    try:
        data = await fetch_snapshots(
            [c.contract_id for c in resolved],
            timeout=25.0,
        )
    except Exception as exc:
        raise HTTPException(502, f"EdgeX WebSocket failed: {exc}") from exc

    rows = [
        analyze_contract(
            contract,
            data.get((contract.contract_id, SETTINGS.monitor_interval), []),
            data.get((contract.contract_id, SETTINGS.entry_interval), []),
        )
        for contract in resolved
    ]
    return {"results": _sort_rows(rows)}


@app.get("/api/screener")
async def screener_api(
    limit: int = Query(default=100, ge=1, le=200),
    force: bool = False,
    stage: str | None = None,
    direction: str | None = None,
    min_score: float = Query(default=0.0, ge=0, le=100),
    min_rr: float | None = Query(default=None, ge=0),
    q: str | None = Query(default=None, max_length=64),
    watch: str | None = Query(default=None, max_length=1000),
):
    try:
        contracts, all_rows = await _scan_market_rows(force=force)
    except Exception as exc:
        raise HTTPException(
            502,
            f"EdgeX market scan failed: {exc}",
        ) from exc

    rows = all_rows
    if stage and stage.upper() != "ALL":
        requested_stage = stage.upper()
        if requested_stage == "NEAR":
            rows = [
                row
                for row in rows
                if row.get("stage") in {"CONFIRMATION_WAIT", "RETEST_WAIT"}
                and row.get("rr") is not None
                and float(row["rr"]) >= SETTINGS.min_rr
            ]
        else:
            rows = [row for row in rows if row.get("stage") == requested_stage]
    if direction and direction.upper() != "ALL":
        target = direction.upper()
        rows = [
            row
            for row in rows
            if (row.get("direction") or "NEUTRAL") == target
        ]
    if min_score > 0:
        rows = [
            row
            for row in rows
            if float(row.get("score") or 0) >= min_score
        ]
    if min_rr is not None:
        rows = [
            row
            for row in rows
            if row.get("rr") is not None
            and float(row["rr"]) >= min_rr
        ]
    if q:
        needle = q.strip().upper()
        rows = [
            row
            for row in rows
            if needle in str(row.get("ticker", "")).upper()
        ]

    rows = _sort_rows(rows)
    ready_candidates = [
        row for row in _sort_rows(all_rows)
        if row.get("stage") == "READY"
    ][:20]
    qualified_near_candidates = [
        row for row in _sort_rows(all_rows)
        if row.get("stage") in {"CONFIRMATION_WAIT", "RETEST_WAIT"}
        and row.get("rr") is not None
        and float(row["rr"]) >= SETTINGS.min_rr
    ][:50]
    watch_names = {
        item.strip().upper()
        for item in (watch or "").split(",")
        if item.strip()
    }
    watch_status = [
        row
        for row in _sort_rows(all_rows)
        if str(row.get("ticker") or "").upper() in watch_names
    ]

    now_ms = int(time.time() * 1000)
    current_bucket = (
        now_ms // scanner.INTERVAL_MS[SETTINGS.entry_interval]
    ) * scanner.INTERVAL_MS[SETTINGS.entry_interval]
    priority_ranking_full = _priority_rank_changes(
        _priority_ranking(all_rows, limit=20),
        current_bucket,
    )
    priority_ranking = priority_ranking_full[:10]
    early_watch = _early_watchlist(priority_ranking_full, limit=8)

    return {
        "universe": len(contracts),
        "scanned": len(all_rows),
        "coverage_pct": round(len(all_rows) / len(contracts) * 100.0, 1) if contracts else 0.0,
        "matched": len(rows),
        "snapshot_age_seconds": (
            int(time.time() - _snapshot_cache[0])
            if _snapshot_cache
            else None
        ),
        "summary": _market_summary(all_rows),
        "confirmation_diagnostics": _confirmation_diagnostics(all_rows),
        "readiness_review": _readiness_review(all_rows),
        "market_regime": _market_regime(all_rows),
        "daily_picks": priority_ranking[:3],
        "priority_ranking": priority_ranking,
        "early_watch": early_watch,
        "ready_candidates": ready_candidates,
        "qualified_near_candidates": qualified_near_candidates,
        "watch_status": watch_status,
        "results": rows[:limit],
    }


@app.get("/api/shadow-v2")
async def shadow_v2_api(
    limit: int = Query(default=500, ge=1, le=2000),
):
    signals = _load_shadow_v2_signals(limit=limit)
    try:
        _contracts, rows = await _scan_market_rows(force=False)
        current = _readiness_review(rows)["proposed_v2"]
    except Exception:
        current = {
            "rule": "measured-move room >= 2R; first target = 2R",
            "ready_count": 0,
            "items": [],
        }
    return {
        "model": "measured_room_fixed_2r",
        "rule": (
            "4H structural stop; measured-move room must support >=2R; "
            "first target fixed at 2R"
        ),
        "current": current,
        "metrics": _shadow_v2_metrics(signals),
        "latest": signals[:50],
    }


@app.get("/api/readiness-review")
async def readiness_review_api(
    force: bool = False,
):
    try:
        contracts, rows = await _scan_market_rows(force=force)
    except Exception as exc:
        raise HTTPException(
            502,
            f"EdgeX market scan failed: {exc}",
        ) from exc
    return {
        "universe": len(contracts),
        "scanned": len(rows),
        "review": _readiness_review(rows),
    }


@app.get("/api/confirmation-diagnostics")
async def confirmation_diagnostics_api(
    force: bool = False,
):
    try:
        contracts, rows = await _scan_market_rows(force=force)
    except Exception as exc:
        raise HTTPException(
            502,
            f"EdgeX market scan failed: {exc}",
        ) from exc
    return {
        "universe": len(contracts),
        "scanned": len(rows),
        "current": _confirmation_diagnostics(rows),
        "history": _confirmation_outcome_groups(limit=1000),
    }


@app.get("/api/approach-validation")
async def approach_validation_api(
    limit: int = Query(default=500, ge=1, le=1000),
):
    return _approach_validation(limit=limit)


@app.get("/api/approach-events")
async def approach_events_api(
    limit: int = Query(default=50, ge=1, le=500),
):
    events = _load_approach_events(limit=limit)
    return {
        "count": len(events),
        "events": events,
    }


@app.get("/api/priority-events")
async def priority_events_api(
    limit: int = Query(default=50, ge=1, le=500),
):
    events = _load_priority_events(limit=limit)
    return {
        "count": len(events),
        "events": events,
    }


@app.get("/api/watchlist")
async def synced_watchlist_get_api(
    sync_key: str = Query(min_length=24, max_length=128),
):
    return {
        "tickers": _load_synced_watchlist(sync_key),
    }


@app.post("/api/watchlist")
async def synced_watchlist_save_api(req: WatchlistSyncRequest):
    return {
        "ok": True,
        "tickers": _save_synced_watchlist(req),
    }


@app.get("/api/custom-alerts")
async def custom_alerts_get_api(
    endpoint: str = Query(min_length=10, max_length=4096),
):
    return {
        "alerts": _load_custom_alerts(endpoint=endpoint),
    }


@app.post("/api/custom-alerts")
async def custom_alerts_create_api(req: CustomAlertCreateRequest):
    return {
        "ok": True,
        "alert": _create_custom_alert(req),
    }


@app.post("/api/custom-alerts/delete")
async def custom_alerts_delete_api(req: CustomAlertDeleteRequest):
    return {
        "ok": _delete_custom_alert(req),
    }


@app.get("/api/daily-reports")
async def daily_reports_api(
    limit: int = Query(default=30, ge=1, le=90),
):
    _maybe_generate_daily_report()
    return {
        "reports": _load_daily_reports(limit=limit),
    }


@app.get("/api/push/config")
async def push_config_api():
    return {
        "enabled": _push_enabled(),
        "public_key": VAPID_PUBLIC_KEY if _push_enabled() else None,
        "subscribers": _subscription_count(),
        "daily_summary_hour_jst": DAILY_SUMMARY_HOUR_JST,
    }


@app.post("/api/push/subscribe")
async def push_subscribe_api(req: PushSubscriptionRequest):
    if not _push_enabled():
        raise HTTPException(503, "Web Push is not configured")
    endpoint = _save_push_subscription(req)
    subscriptions = [
        row for row in _load_push_subscriptions()
        if str(row["endpoint"]) == endpoint
    ]
    delivered = False
    if subscriptions:
        delivered = await asyncio.to_thread(
            _send_push_sync,
            subscriptions[0],
            {
                "title": "EdgeX バックグラウンド通知",
                "body": (
                    "有効になりました。候補の変化と毎朝の市場サマリーを"
                    "アプリを閉じていても通知します。"
                ),
                "url": "/?tab=dashboard",
                "tag": "edgex-push-enabled",
            },
        )
    return {
        "ok": True,
        "test_delivered": delivered,
        "subscribers": _subscription_count(),
    }


@app.post("/api/push/unsubscribe")
async def push_unsubscribe_api(req: PushUnsubscribeRequest):
    _delete_push_subscription(req.endpoint)
    return {
        "ok": True,
        "subscribers": _subscription_count(),
    }


@app.get("/api/push/preferences")
async def push_preferences_get_api(
    endpoint: str = Query(min_length=10, max_length=4096),
):
    row = _get_push_subscription(endpoint)
    if row is None:
        raise HTTPException(404, "Push subscription not found")
    return {
        "candidate_alerts": bool(row["candidate_alerts"]),
        "daily_summary": bool(row["daily_summary"]),
        "timezone": str(row["timezone"]),
        "last_success_ms": row["last_success_ms"],
        "snooze_until_ms": row["snooze_until_ms"],
        "quiet_start": row["quiet_start"],
        "quiet_end": row["quiet_end"],
    }


@app.post("/api/push/preferences")
async def push_preferences_api(req: PushPreferenceRequest):
    if not _update_push_preferences(req):
        raise HTTPException(404, "Push subscription not found")
    row = _get_push_subscription(req.endpoint)
    return {
        "ok": True,
        "candidate_alerts": bool(row["candidate_alerts"]) if row else False,
        "daily_summary": bool(row["daily_summary"]) if row else False,
        "snooze_until_ms": row["snooze_until_ms"] if row else None,
        "quiet_start": row["quiet_start"] if row else None,
        "quiet_end": row["quiet_end"] if row else None,
    }


@app.post("/api/push/test")
async def push_test_api(req: PushTestRequest):
    row = _get_push_subscription(req.endpoint)
    if row is None:
        raise HTTPException(404, "Push subscription not found")
    payload = {
        "title": "EdgeX テスト通知",
        "body": "バックグラウンド通知は正常です。",
        "url": "/?tab=dashboard",
        "tag": "edgex-test",
    }
    delivered = await asyncio.to_thread(_send_push_sync, row, payload)
    _log_push_event(
        kind="test",
        payload=payload,
        sent=1 if delivered else 0,
        attempted=1,
    )
    return {
        "ok": True,
        "delivered": delivered,
    }


@app.get("/api/push/events")
async def push_events_api(
    limit: int = Query(default=50, ge=1, le=200),
):
    events = _load_push_events(limit=limit)
    return {
        "count": len(events),
        "events": events,
    }


@app.get("/api/opportunity-analysis")
async def opportunity_analysis_api(
    limit: int = Query(default=500, ge=1, le=1000),
):
    return _opportunity_analysis(limit=limit)


@app.get("/api/candidate-events")
async def candidate_events_api(
    limit: int = Query(default=100, ge=1, le=500),
):
    events = _load_candidate_events(limit=limit)
    return {
        "count": len(events),
        "events": events,
    }


@app.get("/api/server-history")
async def server_history_api(
    hours: int = Query(default=48, ge=1, le=720),
):
    return {
        "hours": hours,
        "snapshots": _load_market_history(hours=hours),
    }


@app.get("/api/server-paper-signals")
async def server_paper_signals_api(
    limit: int = Query(default=200, ge=1, le=1000),
):
    signals = _load_paper_signals(limit=limit)
    return {
        "count": len(signals),
        "signals": signals,
    }


@app.post("/api/risk")
async def risk_api(req: RiskRequest):
    return risk_plan(req)


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(
        Path(__file__).with_name("manifest.webmanifest"),
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        Path(__file__).with_name("sw.js"),
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


@app.get("/app-icon.svg")
async def app_icon():
    return FileResponse(
        Path(__file__).with_name("app-icon.svg"),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(
        Path(__file__).with_name("index.html").read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
