from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from pathlib import Path
from statistics import mean
from typing import Any

import uvicorn
import websockets
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app as scanner

SETTINGS = scanner.Settings.from_env(dry_run_override=True)
CLIENT = scanner.EdgeXClient(SETTINGS)
DETECTOR = scanner.RollReversalDetector(SETTINGS)

_snapshot_cache: tuple[float, dict[tuple[str, str], list[scanner.Candle]]] | None = None
_cache_lock = asyncio.Lock()


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
                    ws.recv(),
                    timeout=max(0.2, deadline - time.monotonic()),
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
            parsed = []
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
        candle for candle in sorted(candles, key=lambda item: item.time_ms)
        if candle.time_ms + interval_ms <= cutoff
    ]


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

    score = min(10.0, max(0.0, volume_ratio * 4.0))
    if direction is None:
        base.update(stage="TREND_WAIT", score=round(score, 1), reason="4H trend not aligned")
        return base

    score += 20.0
    raw_direction = "up" if direction == "LONG" else "down"
    breakout = DETECTOR._recent_breakout(monitor, raw_direction)
    if breakout is None:
        base.update(stage="BREAKOUT_WAIT", score=round(score, 1), reason="waiting for recent 4H breakout")
        return base

    breakout_index, roll_level = breakout
    score += 25.0
    tolerance = max(atr15 * SETTINGS.retest_atr_tolerance, roll_level * 0.001)
    retest_window = entries[-SETTINGS.retest_lookback:]

    if direction == "LONG":
        touched = any(c.low <= roll_level + tolerance for c in retest_window)
        confirmed = latest15.close > latest15.open and latest15.close > roll_level
        structural_stop = min(roll_level, min(c.low for c in monitor[breakout_index:]))
        stop = structural_stop - atr4 * SETTINGS.atr_stop_buffer
        raw_target = max(c.high for c in monitor[breakout_index:])
        target = raw_target - atr4 * SETTINGS.atr_target_buffer
        structure_ok = stop < latest15.close < target
        rr = (target - latest15.close) / (latest15.close - stop) if structure_ok else None
    else:
        touched = any(c.high >= roll_level - tolerance for c in retest_window)
        confirmed = latest15.close < latest15.open and latest15.close < roll_level
        structural_stop = max(roll_level, max(c.high for c in monitor[breakout_index:]))
        stop = structural_stop + atr4 * SETTINGS.atr_stop_buffer
        raw_target = min(c.low for c in monitor[breakout_index:])
        target = raw_target + atr4 * SETTINGS.atr_target_buffer
        structure_ok = target < latest15.close < stop
        rr = (latest15.close - target) / (stop - latest15.close) if structure_ok else None

    if touched:
        score += 20.0
    if confirmed:
        score += 15.0
    if rr is not None:
        score += min(20.0, rr / SETTINGS.min_rr * 20.0)

    if not touched:
        stage, reason = "RETEST_WAIT", "4H setup exists; waiting for 15M retest"
    elif not confirmed:
        stage, reason = "CONFIRMATION_WAIT", "retest seen; waiting for 15M confirmation"
    elif rr is None:
        stage, reason = "STRUCTURE_WAIT", "4H structural target/stop is not valid at current price"
    elif rr < SETTINGS.min_rr:
        stage, reason = "RR_WAIT", f"structural RR {rr:.2f} is below {SETTINGS.min_rr:.2f}"
    else:
        stage, reason = "READY", "trend + breakout + retest + confirmation + RR >= 2"

    stop_distance = abs(latest15.close - stop) if structure_ok else None
    tp1 = None
    if stop_distance is not None:
        tp1 = latest15.close + (2.0 * stop_distance if direction == "LONG" else -2.0 * stop_distance)

    base.update({
        "stage": stage,
        "reason": reason,
        "score": round(min(100.0, score), 1),
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
        data = await fetch_snapshots(list(contracts), timeout=40.0)
        _snapshot_cache = (time.time(), data)
        return data


class RiskRequest(BaseModel):
    equity: float = Field(gt=0)
    risk_pct: float = Field(default=5.0, gt=0, le=100)
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    target: float | None = Field(default=None, gt=0)
    step_size: float | None = Field(default=None, gt=0)
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

    max_loss = size * distance
    rr = None
    target_profit = None
    if req.target is not None:
        reward = abs(req.target - req.entry)
        rr = reward / distance
        target_profit = size * reward

    return {
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
    }


app = FastAPI(title="EdgeX Analysis Terminal", version="1.0.0")


@app.get("/health")
async def health():
    return {"ok": True, "service": "edgex-analysis-terminal", "time_ms": int(time.time() * 1000)}


@app.get("/api/contracts")
async def contracts_api():
    try:
        contracts = await CLIENT.get_contracts()
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"count": len(contracts), "contracts": [contract.__dict__ for contract in contracts.values()]}


@app.get("/api/analyze")
async def analyze_api(ticker: str = Query(min_length=2, max_length=64)):
    ticker = ticker.strip().upper()
    try:
        contracts = await CLIENT.get_contracts()
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc

    contract = next((c for c in contracts.values() if c.contract_name.upper() == ticker), None)
    if contract is None:
        bare = ticker.removesuffix("USDC")
        contract = next(
            (c for c in contracts.values() if c.contract_name.upper().removesuffix("USDC") == bare),
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


@app.get("/api/screener")
async def screener_api(
    limit: int = Query(default=20, ge=1, le=50),
    force: bool = False,
):
    try:
        contracts = await CLIENT.get_contracts()
        data = await market_snapshots(force=force)
    except Exception as exc:
        raise HTTPException(502, f"EdgeX market scan failed: {exc}") from exc

    rows = []
    for cid, contract in contracts.items():
        row = analyze_contract(
            contract,
            data.get((cid, SETTINGS.monitor_interval), []),
            data.get((cid, SETTINGS.entry_interval), []),
        )
        if row.get("current_price") is not None:
            rows.append(row)

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
    rows.sort(key=lambda row: (
        priority.get(row.get("stage"), 99),
        -float(row.get("score") or 0),
        -float(row.get("volume_24h_value") or 0),
    ))

    return {
        "scanned": len(rows),
        "snapshot_age_seconds": int(time.time() - _snapshot_cache[0]) if _snapshot_cache else None,
        "results": rows[:limit],
    }


@app.post("/api/risk")
async def risk_api(req: RiskRequest):
    return risk_plan(req)


@app.get("/", response_class=HTMLResponse)
async def index():
    return Path(__file__).with_name("index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
