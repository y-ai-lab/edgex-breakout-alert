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
from pathlib import Path
from statistics import mean
from typing import Any

import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app as scanner

SETTINGS = scanner.Settings.from_env(dry_run_override=True)
CLIENT = scanner.EdgeXClient(SETTINGS)
DETECTOR = scanner.RollReversalDetector(SETTINGS)

_snapshot_cache: tuple[float, dict[tuple[str, str], list[scanner.Candle]]] | None = None
_cache_lock = asyncio.Lock()
DB_PATH = Path(os.getenv("ANALYSIS_DB_PATH", "/data/analysis_terminal.db"))
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
        conn.commit()


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
    entries = closed(candles, SETTINGS.entry_interval)
    prior = signal.get("result") or {}
    if prior.get("status") in {"TP", "SL", "AMBIGUOUS"}:
        return prior

    interval_ms = scanner.INTERVAL_MS[SETTINGS.entry_interval]
    created_ms = int(signal["created_ms"])
    prior_end = prior.get("history_end_ms")

    if prior_end is None:
        relevant = [
            candle
            for candle in entries
            if candle.time_ms + interval_ms >= created_ms
        ]
        coverage_now = bool(entries) and entries[0].time_ms <= created_ms
    else:
        prior_end = int(prior_end)
        relevant = [
            candle
            for candle in entries
            if candle.time_ms > prior_end
        ]
        coverage_now = bool(entries) and entries[0].time_ms <= prior_end + interval_ms

    if not relevant:
        return prior or {
            "status": "OPEN",
            "coverage_complete": coverage_now,
            "history_start_ms": entries[0].time_ms if entries else None,
            "history_end_ms": entries[-1].time_ms if entries else None,
        }

    side = str(signal["side"]).upper()
    entry = float(signal["entry"])
    stop = float(signal["stop"])
    target = float(signal["target"])
    tp1 = signal.get("tp1")
    tp1 = float(tp1) if tp1 is not None else None
    risk = abs(entry - stop)
    if risk <= 0:
        return {
            "status": "ERROR",
            "error": "invalid risk distance",
            "coverage_complete": False,
        }

    prior_mfe = float(prior.get("mfe_r") or 0.0)
    prior_mae = float(prior.get("mae_r") or 0.0)
    prior_tp1 = prior.get("tp1_time_ms")

    max_high = max(c.high for c in relevant)
    min_low = min(c.low for c in relevant)
    if side == "LONG":
        current_mfe = max(0.0, (max_high - entry) / risk)
        current_mae = max(0.0, (entry - min_low) / risk)
    else:
        current_mfe = max(0.0, (entry - min_low) / risk)
        current_mae = max(0.0, (max_high - entry) / risk)

    mfe_r = max(prior_mfe, current_mfe)
    mae_r = max(prior_mae, current_mae)
    status = str(prior.get("status") or "OPEN")
    final_r = prior.get("final_r")
    outcome_time_ms = prior.get("outcome_time_ms")
    tp1_time_ms = prior_tp1
    ambiguous_reason = prior.get("ambiguous_reason")

    for candle in relevant:
        if side == "LONG":
            stop_hit = candle.low <= stop
            target_hit = candle.high >= target
            tp1_hit = tp1 is not None and candle.high >= tp1
        else:
            stop_hit = candle.high >= stop
            target_hit = candle.low <= target
            tp1_hit = tp1 is not None and candle.low <= tp1

        if tp1_hit and tp1_time_ms is None:
            tp1_time_ms = candle.time_ms

        if stop_hit and target_hit:
            status = "AMBIGUOUS"
            outcome_time_ms = candle.time_ms
            ambiguous_reason = "SL and final TP touched in the same 15M candle"
            final_r = None
            break

        if stop_hit:
            if tp1_hit and tp1_time_ms == candle.time_ms:
                status = "AMBIGUOUS"
                outcome_time_ms = candle.time_ms
                ambiguous_reason = "SL and TP1 touched in the same 15M candle"
                final_r = None
                break
            status = "SL"
            outcome_time_ms = candle.time_ms
            final_r = -1.0
            break

        if target_hit:
            status = "TP"
            outcome_time_ms = candle.time_ms
            final_r = abs(target - entry) / risk
            break

    if status == "OPEN" and tp1_time_ms is not None:
        status = "TP1"

    prior_coverage = prior.get("coverage_complete")
    if prior_end is None:
        coverage_complete = coverage_now
    elif prior_coverage is False:
        coverage_complete = False
    else:
        coverage_complete = bool(coverage_now)

    return {
        "status": status,
        "final_r": round(float(final_r), 4) if final_r is not None else None,
        "mfe_r": round(mfe_r, 4),
        "mae_r": round(mae_r, 4),
        "tp1_time_ms": tp1_time_ms,
        "outcome_time_ms": outcome_time_ms,
        "ambiguous_reason": ambiguous_reason,
        "last_price": relevant[-1].close,
        "coverage_complete": coverage_complete,
        "history_start_ms": entries[0].time_ms if entries else None,
        "history_end_ms": relevant[-1].time_ms,
        "candles_checked": int(prior.get("candles_checked") or 0) + len(relevant),
    }


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
        confirmed = latest15.close > latest15.open and latest15.close > roll_level
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
        confirmed = latest15.close < latest15.open and latest15.close < roll_level
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
        "entry_reference": latest15.close,
        "stop_loss": stop if structure_ok else None,
        "take_profit": target if structure_ok else None,
        "tp1_2r": tp1,
        "rr": rr,
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


def _daily_picks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def pick_priority(row: dict[str, Any]) -> tuple[int, float, float]:
        stage = str(row.get("stage") or "")
        rr = row.get("rr")
        rr_value = float(rr) if rr is not None else 0.0
        if stage == "READY":
            rank = 0
        elif stage in {"CONFIRMATION_WAIT", "RETEST_WAIT"} and rr_value >= SETTINGS.min_rr:
            rank = 1
        elif stage in {"CONFIRMATION_WAIT", "RETEST_WAIT"}:
            rank = 2
        elif stage == "BREAKOUT_WAIT":
            rank = 3
        else:
            rank = 4
        return (
            rank,
            -float(row.get("score") or 0),
            -rr_value,
        )

    selected = sorted(rows, key=pick_priority)[:3]
    return [
        {
            "ticker": row.get("ticker"),
            "stage": row.get("stage"),
            "direction": row.get("direction"),
            "score": row.get("score"),
            "rr": row.get("rr"),
            "current_price": row.get("current_price"),
            "reason": row.get("reason"),
        }
        for row in selected
    ]


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
                await _refresh_paper_signal_results(contracts)
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
    version="7.3.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    history = _load_market_history(hours=48)
    paper = _load_paper_signals(limit=1000)
    return {
        "ok": True,
        "service": "edgex-analysis-terminal",
        "version": "7.3.0",
        "time_ms": int(time.time() * 1000),
        "storage": {
            "market_snapshots_48h": len(history),
            "paper_signals": len(paper),
            "db_exists": DB_PATH.exists(),
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
    items = closed(candles, interval)[-limit:]
    closes = [candle.close for candle in items]
    ema20 = _ema_series(closes, SETTINGS.trend_fast_ema)
    ema50 = _ema_series(closes, SETTINGS.trend_slow_ema)
    return [
        {
            "time_ms": candle.time_ms,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.value,
            "ema20": ema20[index],
            "ema50": ema50[index],
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
        "market_regime": _market_regime(all_rows),
        "daily_picks": _daily_picks(all_rows),
        "ready_candidates": ready_candidates,
        "results": rows[:limit],
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
