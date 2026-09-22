#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import Candle, Contract, EdgeXClient, INTERVAL_MS, RollReversalDetector, Settings

DAY_MS = 24 * 60 * 60 * 1000
ENTRY_INTERVAL = "MINUTE_15"
MONITOR_INTERVAL = "HOUR_4"
ENTRY_MS = INTERVAL_MS[ENTRY_INTERVAL]
MONITOR_MS = INTERVAL_MS[MONITOR_INTERVAL]
BASE_URL = "https://edgex-prod-v2.edgex.exchange"
PAGE_SIZE = 640
ENTRY_CHUNK_MS = 6 * DAY_MS
MONITOR_CHUNK_MS = 40 * DAY_MS


def fetch_json(path: str, params: dict[str, str], retries: int = 5) -> dict[str, Any]:
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    delay = 1.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "edgex-roll-reversal-backtest/1.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or payload.get("code") != "SUCCESS":
                raise RuntimeError(f"EdgeX API error: {payload}")
            return payload
        except Exception:
            if attempt + 1 >= retries:
                raise
            time.sleep(delay)
            delay = min(delay * 2.0, 8.0)
    raise RuntimeError("unreachable")


def fetch_klines(
    contract: Contract,
    interval: str,
    begin_ms: int,
    end_ms: int,
    chunk_ms: int,
) -> list[Candle]:
    by_time: dict[int, Candle] = {}
    cursor = begin_ms
    while cursor < end_ms:
        chunk_end = min(cursor + chunk_ms, end_ms)
        payload = fetch_json(
            "/api/v2/public/quote/getKline",
            {
                "contractId": contract.contract_id,
                "klineType": interval,
                "priceType": "LAST_PRICE",
                "size": str(PAGE_SIZE),
                "filterBeginKlineTimeInclusive": str(cursor),
                "filterEndKlineTimeExclusive": str(chunk_end),
            },
        )
        data = payload.get("data") or {}
        rows = data.get("dataList") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            candle = Candle.from_payload(row, fallback_interval=interval)
            if candle is not None and begin_ms <= candle.time_ms < end_ms:
                by_time[candle.time_ms] = candle
        next_offset = data.get("nextPageOffsetData")
        if next_offset:
            # Chunk widths are intentionally below the observed 640-row API cap.
            # A next-page token here means the data density exceeded our assumption.
            offset = str(next_offset)
            seen_offsets: set[str] = set()
            while offset and offset not in seen_offsets:
                seen_offsets.add(offset)
                page = fetch_json(
                    "/api/v2/public/quote/getKline",
                    {
                        "contractId": contract.contract_id,
                        "klineType": interval,
                        "priceType": "LAST_PRICE",
                        "size": str(PAGE_SIZE),
                        "filterBeginKlineTimeInclusive": str(cursor),
                        "filterEndKlineTimeExclusive": str(chunk_end),
                        "offsetData": offset,
                    },
                )
                page_data = page.get("data") or {}
                for row in page_data.get("dataList") or []:
                    if isinstance(row, dict):
                        candle = Candle.from_payload(row, fallback_interval=interval)
                        if candle is not None and begin_ms <= candle.time_ms < end_ms:
                            by_time[candle.time_ms] = candle
                offset = str(page_data.get("nextPageOffsetData") or "")
        cursor = chunk_end
    return sorted(by_time.values(), key=lambda candle: candle.time_ms)


def hit(candle: Candle, price: float, direction: str, kind: str) -> bool:
    if direction == "up":
        return candle.low <= price if kind == "stop" else candle.high >= price
    return candle.high >= price if kind == "stop" else candle.low <= price


def mark_r(direction: str, entry: float, stop: float, price: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    signed = price - entry if direction == "up" else entry - price
    return signed / risk


def simulate_trade(signal, entries: list[Candle], entry_index: int, end_ms: int) -> dict[str, Any]:
    entry = signal.candle.close
    stop = float(signal.stop_loss_override)
    target = float(signal.take_profit_override)
    rr = float(signal.rr)
    split = len(signal.split_targets) >= 2
    tp1 = signal.split_targets[0][1] if split else None
    tp1_done = False
    last_close = entry

    result = {
        "contract_id": signal.contract.contract_id,
        "symbol": signal.contract.contract_name,
        "direction": "LONG" if signal.direction == "up" else "SHORT",
        "entry_time_ms": signal.candle.time_ms + ENTRY_MS,
        "entry": entry,
        "stop": stop,
        "target": target,
        "rr_planned": rr,
        "split": split,
        "roll_level": signal.breakout_level,
        "atr_15m": signal.atr_entry,
        "atr_4h": signal.atr_monitor,
        "exit_time_ms": None,
        "exit_reason": "OPEN",
        "r_result": None,
        "mark_r": 0.0,
    }

    for candle in entries[entry_index + 1 :]:
        if candle.time_ms >= end_ms:
            break
        last_close = candle.close
        stop_hit = hit(candle, stop, signal.direction, "stop")

        if not split:
            target_hit = hit(candle, target, signal.direction, "target")
            if stop_hit:
                result.update(
                    exit_time_ms=candle.time_ms + ENTRY_MS,
                    exit_reason="SL" if not target_hit else "SL_BOTH_HIT",
                    r_result=-1.0,
                )
                return result
            if target_hit:
                result.update(
                    exit_time_ms=candle.time_ms + ENTRY_MS,
                    exit_reason="TP",
                    r_result=rr,
                )
                return result
            continue

        assert tp1 is not None
        if not tp1_done:
            tp1_hit = hit(candle, tp1, signal.direction, "target")
            final_hit = hit(candle, target, signal.direction, "target")
            if stop_hit:
                result.update(
                    exit_time_ms=candle.time_ms + ENTRY_MS,
                    exit_reason="SL" if not (tp1_hit or final_hit) else "SL_BOTH_HIT",
                    r_result=-1.0,
                )
                return result
            if final_hit:
                result.update(
                    exit_time_ms=candle.time_ms + ENTRY_MS,
                    exit_reason="TP1_TP2_SAME_CANDLE",
                    r_result=1.0 + 0.5 * rr,
                )
                return result
            if tp1_hit:
                tp1_done = True
            continue

        final_hit = hit(candle, target, signal.direction, "target")
        if stop_hit:
            result.update(
                exit_time_ms=candle.time_ms + ENTRY_MS,
                exit_reason="TP1_THEN_SL" if not final_hit else "TP1_THEN_SL_BOTH_HIT",
                r_result=0.5,
            )
            return result
        if final_hit:
            result.update(
                exit_time_ms=candle.time_ms + ENTRY_MS,
                exit_reason="TP1_THEN_TP2",
                r_result=1.0 + 0.5 * rr,
            )
            return result

    m = mark_r(signal.direction, entry, stop, last_close)
    if split and tp1_done:
        # 50% realized at +2R => +1R plus mark-to-market on the remaining half.
        m = 1.0 + 0.5 * m
    result["mark_r"] = m
    return result


def backtest_contract(
    contract: Contract,
    settings: Settings,
    period_start_ms: int,
    period_end_ms: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    monitor_begin = period_start_ms - 14 * DAY_MS
    entry_begin = period_start_ms - 2 * DAY_MS
    monitor = fetch_klines(contract, MONITOR_INTERVAL, monitor_begin, period_end_ms, MONITOR_CHUNK_MS)
    entries = fetch_klines(contract, ENTRY_INTERVAL, entry_begin, period_end_ms, ENTRY_CHUNK_MS)

    detector = RollReversalDetector(settings)
    trades: list[dict[str, Any]] = []
    monitor_end = 0
    seen_keys: set[str] = set()

    for i, candidate in enumerate(entries):
        candidate_close_ms = candidate.time_ms + ENTRY_MS
        if candidate.time_ms < period_start_ms or candidate_close_ms > period_end_ms:
            continue

        while (
            monitor_end < len(monitor)
            and monitor[monitor_end].time_ms + MONITOR_MS <= candidate_close_ms
        ):
            monitor_end += 1

        monitor_history = monitor[max(0, monitor_end - settings.history_size) : monitor_end]
        entry_history = entries[max(0, i - settings.history_size + 1) : i + 1]
        signal = detector.detect(contract, monitor_history, entry_history, candidate)
        if signal is None or signal.key in seen_keys:
            continue
        seen_keys.add(signal.key)
        trades.append(simulate_trade(signal, entries, i, period_end_ms))

    coverage = {
        "symbol": contract.contract_name,
        "contract_id": contract.contract_id,
        "monitor_bars": len(monitor),
        "entry_bars": len(entries),
        "first_entry_bar_ms": entries[0].time_ms if entries else None,
        "last_entry_bar_ms": entries[-1].time_ms if entries else None,
        "signals": len(trades),
    }
    return trades, coverage


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def summarize(trades: list[dict[str, Any]], starting_equity: float, risk_fraction: float) -> dict[str, Any]:
    closed = [trade for trade in trades if trade["r_result"] is not None]
    open_trades = [trade for trade in trades if trade["r_result"] is None]
    wins = [trade for trade in closed if trade["r_result"] > 0]
    losses = [trade for trade in closed if trade["r_result"] < 0]
    breakeven = [trade for trade in closed if trade["r_result"] == 0]
    gross_profit_r = sum(trade["r_result"] for trade in wins)
    gross_loss_r = -sum(trade["r_result"] for trade in losses)
    net_r = sum(trade["r_result"] for trade in closed)
    profit_factor = gross_profit_r / gross_loss_r if gross_loss_r > 0 else None
    expectancy_r = net_r / len(closed) if closed else None

    # Indicative sequential compounding: each completed signal risks 5% of the
    # then-current equity. It does not impose portfolio margin/exposure caps.
    equity = starting_equity
    peak = equity
    max_drawdown = 0.0
    equity_path = []
    for trade in sorted(closed, key=lambda item: (item["entry_time_ms"], item["symbol"])):
        pnl = equity * risk_fraction * trade["r_result"]
        equity += pnl
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_drawdown = max(max_drawdown, dd)
        equity_path.append({"time_ms": trade["entry_time_ms"], "equity": equity})

    return {
        "signals_total": len(trades),
        "closed_trades": len(closed),
        "open_trades": len(open_trades),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate_pct": (len(wins) / len(closed) * 100.0) if closed else None,
        "net_r": net_r,
        "avg_r": expectancy_r,
        "gross_profit_r": gross_profit_r,
        "gross_loss_r": gross_loss_r,
        "profit_factor": profit_factor,
        "avg_planned_rr": (
            sum(trade["rr_planned"] for trade in trades) / len(trades) if trades else None
        ),
        "split_trade_count": sum(1 for trade in trades if trade["split"]),
        "starting_equity_usdc": starting_equity,
        "indicative_ending_equity_usdc": equity,
        "indicative_return_pct": (equity / starting_equity - 1.0) * 100.0,
        "indicative_max_drawdown_pct": max_drawdown * 100.0,
        "open_mark_r": sum(trade["mark_r"] for trade in open_trades),
        "equity_path": equity_path,
    }


def main() -> None:
    os.environ.setdefault("DRY_RUN", "true")
    os.environ.setdefault("EDGEX_EQUITY_USDC", "24")
    os.environ.setdefault("EDGE_X_MONITOR_INTERVAL", MONITOR_INTERVAL)
    os.environ.setdefault("EDGE_X_ENTRY_INTERVAL", ENTRY_INTERVAL)
    os.environ.setdefault("EDGE_X_INTERVALS", f"{MONITOR_INTERVAL},{ENTRY_INTERVAL}")
    os.environ.setdefault("EDGE_X_HISTORY_SIZE", "96")

    settings = Settings.from_env(dry_run_override=True)
    client = EdgeXClient(settings)
    contracts = asyncio.run(client.get_contracts())

    now_ms = int(time.time() * 1000)
    period_end_ms = (now_ms // ENTRY_MS) * ENTRY_MS
    period_start_ms = period_end_ms - 30 * DAY_MS

    all_trades: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    contract_list = sorted(contracts.values(), key=lambda c: c.contract_name)
    print(f"Backtesting {len(contract_list)} currently tradable contracts")
    print(f"Period UTC: {iso(period_start_ms)} -> {iso(period_end_ms)}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(backtest_contract, contract, settings, period_start_ms, period_end_ms): contract
            for contract in contract_list
        }
        done = 0
        for future in as_completed(futures):
            contract = futures[future]
            done += 1
            try:
                trades, cov = future.result()
                all_trades.extend(trades)
                coverage.append(cov)
                print(
                    f"[{done}/{len(contract_list)}] {contract.contract_name}: "
                    f"{cov['entry_bars']} 15m bars, {cov['monitor_bars']} 4h bars, {len(trades)} signals"
                )
            except Exception as exc:
                errors.append({"symbol": contract.contract_name, "error": f"{type(exc).__name__}: {exc}"})
                print(f"[{done}/{len(contract_list)}] {contract.contract_name}: ERROR {exc}")

    all_trades.sort(key=lambda item: (item["entry_time_ms"], item["symbol"]))
    coverage.sort(key=lambda item: item["symbol"])
    summary = summarize(all_trades, starting_equity=24.0, risk_fraction=settings.risk_per_trade)

    for trade in all_trades:
        trade["entry_time_utc"] = iso(trade["entry_time_ms"])
        trade["exit_time_utc"] = iso(trade["exit_time_ms"])

    output = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "period_start_utc": iso(period_start_ms),
        "period_end_utc": iso(period_end_ms),
        "period_days": 30,
        "universe": "currently tradable EdgeX contracts at backtest run time",
        "strategy": {
            "monitor_interval": settings.monitor_interval,
            "entry_interval": settings.entry_interval,
            "trend_ema": [settings.trend_fast_ema, settings.trend_slow_ema],
            "roll_lookback": settings.roll_lookback,
            "roll_max_age": settings.roll_max_age,
            "retest_lookback": settings.retest_lookback,
            "atr_period": settings.atr_period,
            "atr_stop_buffer": settings.atr_stop_buffer,
            "atr_target_buffer": settings.atr_target_buffer,
            "retest_atr_tolerance": settings.retest_atr_tolerance,
            "min_rr": settings.min_rr,
            "split_rr": settings.split_rr,
            "risk_per_trade": settings.risk_per_trade,
        },
        "execution_assumptions": {
            "entry": "15m signal candle close",
            "same_candle_stop_and_target": "stop first (conservative)",
            "split": "RR>=3: 50% at 2R, 50% at final 4H target; original stop remains",
            "fees_slippage_funding": "excluded",
            "portfolio_margin_cap": "not modeled",
            "equity_compounding": "indicative sequential compounding by signal entry order",
        },
        "summary": summary,
        "coverage": {
            "contracts_total": len(contract_list),
            "contracts_completed": len(coverage),
            "contracts_failed": len(errors),
            "contracts_with_signals": sum(1 for row in coverage if row["signals"] > 0),
        },
        "errors": errors,
        "trades": all_trades,
    }

    out_dir = Path("backtest_results")
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "last_30_days.json"
    csv_path = out_dir / "last_30_days_trades.csv"
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "symbol", "contract_id", "direction", "entry_time_utc", "entry", "stop", "target",
        "rr_planned", "split", "exit_time_utc", "exit_reason", "r_result", "mark_r",
        "roll_level", "atr_15m", "atr_4h",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for trade in all_trades:
            writer.writerow({field: trade.get(field) for field in fields})

    print("SUMMARY", json.dumps(summary, ensure_ascii=False))
    print("COVERAGE", json.dumps(output["coverage"], ensure_ascii=False))
    print("ERRORS", json.dumps(errors[:20], ensure_ascii=False))


if __name__ == "__main__":
    main()
