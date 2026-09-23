#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import csv
import json
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app import Candle, Contract, EdgeXClient, INTERVAL_MS, RollReversalDetector, Settings, Signal, _ema, _atr

DAY_MS = 24 * 60 * 60 * 1000
ENTRY_INTERVAL = "MINUTE_15"
MONITOR_INTERVAL = "HOUR_4"
ENTRY_MS = INTERVAL_MS[ENTRY_INTERVAL]
MONITOR_MS = INTERVAL_MS[MONITOR_INTERVAL]
BASE_URL = "https://edgex-prod-v2.edgex.exchange"
PAGE_SIZE = 640
ENTRY_CHUNK_MS = 6 * DAY_MS      # 576 x 15m bars, below the observed 640-row cap.
MONITOR_CHUNK_MS = 100 * DAY_MS  # 600 x 4h bars, below the observed 640-row cap.
PERIOD_DAYS = 180
HOLDOUT_DAYS = 30
VARIANTS = ("baseline", "reclaim", "wick_rejection", "reclaim_strong_body")

def confirmation_ok(variant: str, direction: str, candidate: Candle, roll_level: float, tolerance: float) -> bool:
    body = abs(candidate.close - candidate.open)
    rng = max(candidate.high - candidate.low, 1e-12)
    body_ratio = body / rng
    if variant == "baseline":
        return (candidate.close > candidate.open and candidate.close > roll_level) if direction == "up" else (candidate.close < candidate.open and candidate.close < roll_level)
    if variant == "reclaim":
        return (candidate.low <= roll_level and candidate.close > roll_level) if direction == "up" else (candidate.high >= roll_level and candidate.close < roll_level)
    if variant == "wick_rejection":
        if direction == "up":
            lower_wick = min(candidate.open, candidate.close) - candidate.low
            return candidate.low <= roll_level + tolerance and candidate.close > roll_level and lower_wick >= body
        upper_wick = candidate.high - max(candidate.open, candidate.close)
        return candidate.high >= roll_level - tolerance and candidate.close < roll_level and upper_wick >= body
    if variant == "reclaim_strong_body":
        reclaim = (candidate.low <= roll_level and candidate.close > roll_level) if direction == "up" else (candidate.high >= roll_level and candidate.close < roll_level)
        directional = candidate.close > candidate.open if direction == "up" else candidate.close < candidate.open
        return reclaim and directional and body_ratio >= 0.5
    raise ValueError(variant)

class VariantDetector(RollReversalDetector):
    def __init__(self, settings: Settings, variant: str) -> None:
        super().__init__(settings)
        self.variant = variant

    def detect(self, contract: Contract, monitor_candles: Iterable[Candle], entry_candles: Iterable[Candle], candidate: Candle) -> Signal | None:
        monitor = sorted(monitor_candles, key=lambda item: item.time_ms)
        entries = sorted((x for x in entry_candles if x.time_ms <= candidate.time_ms), key=lambda item: item.time_ms)
        if not monitor or not entries or entries[-1].time_ms != candidate.time_ms:
            return None
        required_monitor = max(self.settings.trend_slow_ema, self.settings.roll_lookback + self.settings.roll_max_age, self.settings.atr_period + 1)
        required_entry = max(self.settings.atr_period + 1, self.settings.retest_lookback, self.settings.pullback_swing_lookback)
        if len(monitor) < required_monitor or len(entries) < required_entry:
            return None

        closes = [x.close for x in monitor]
        ema_fast = _ema(closes, self.settings.trend_fast_ema)
        ema_slow = _ema(closes, self.settings.trend_slow_ema)
        atr_monitor = _atr(monitor, self.settings.atr_period)
        atr_entry = _atr(entries, self.settings.atr_period)
        if None in {ema_fast, ema_slow, atr_monitor, atr_entry}:
            return None
        if atr_monitor <= 0 or atr_entry <= 0:
            return None

        latest_monitor = monitor[-1]
        if ema_fast > ema_slow and latest_monitor.close > ema_fast:
            direction = "up"
        elif ema_fast < ema_slow and latest_monitor.close < ema_fast:
            direction = "down"
        else:
            return None

        breakout = self._recent_breakout(monitor, direction)
        if breakout is None:
            return None
        breakout_index, roll_level = breakout
        tolerance = max(atr_entry * self.settings.retest_atr_tolerance, roll_level * 0.001)
        retest_window = entries[-self.settings.retest_lookback:]
        if direction == "up":
            touched = any(x.low <= roll_level + tolerance for x in retest_window)
        else:
            touched = any(x.high >= roll_level - tolerance for x in retest_window)
        if not touched or not confirmation_ok(self.variant, direction, candidate, roll_level, tolerance):
            return None

        monitor_structure = monitor[breakout_index:]
        if direction == "up":
            structural_stop = min(roll_level, min(x.low for x in monitor_structure))
            stop_loss = structural_stop - atr_monitor * self.settings.atr_stop_buffer
            raw_target = max(x.high for x in monitor_structure)
            take_profit = raw_target - atr_monitor * self.settings.atr_target_buffer
            if stop_loss >= candidate.close or take_profit <= candidate.close:
                return None
            rr = (take_profit - candidate.close) / (candidate.close - stop_loss)
        else:
            structural_stop = max(roll_level, max(x.high for x in monitor_structure))
            stop_loss = structural_stop + atr_monitor * self.settings.atr_stop_buffer
            raw_target = min(x.low for x in monitor_structure)
            take_profit = raw_target + atr_monitor * self.settings.atr_target_buffer
            if stop_loss <= candidate.close or take_profit >= candidate.close:
                return None
            rr = (candidate.close - take_profit) / (stop_loss - candidate.close)

        if rr < self.settings.min_rr:
            return None
        stop_distance = abs(candidate.close - stop_loss)
        if rr >= self.settings.split_rr:
            sign = 1.0 if direction == "up" else -1.0
            split_targets = ((0.5, candidate.close + sign * stop_distance * 2.0), (0.5, take_profit))
        else:
            split_targets = ((1.0, take_profit),)
        volume_window = entries[-min(len(entries), self.settings.volume_lookback):]
        average_value = sum(x.value for x in volume_window) / len(volume_window) if volume_window else candidate.value
        volume_ratio = candidate.value / average_value if average_value > 0 else 1.0
        breakout_pct = ((candidate.close / roll_level - 1.0) * 100.0 if direction == "up" else -(1.0 - candidate.close / roll_level) * 100.0)
        return Signal(
            contract=contract, interval=self.settings.entry_interval, direction=direction, candle=candidate,
            breakout_level=roll_level, breakout_pct=breakout_pct, volume_average=average_value,
            volume_ratio=volume_ratio, volume_lookback=len(volume_window), strategy_name=f"4H trend + roll reversal [{self.variant}]",
            monitor_interval=self.settings.monitor_interval, ema_fast=ema_fast, ema_slow=ema_slow,
            atr_entry=atr_entry, atr_monitor=atr_monitor, raw_target=raw_target,
            stop_loss_override=stop_loss, take_profit_override=take_profit, rr=rr,
            breakout_time_ms=monitor[breakout_index].time_ms, split_targets=split_targets,
        )


def fetch_json(path: str, params: dict[str, str], retries: int = 6) -> dict[str, Any]:
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    delay = 1.0
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "edgex-roll-reversal-backtest/2.0",
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or payload.get("code") != "SUCCESS":
                raise RuntimeError(f"EdgeX API error: {payload}")
            return payload
        except Exception:
            if attempt + 1 >= retries:
                raise
            time.sleep(delay)
            delay = min(delay * 2.0, 12.0)
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
        base_params = {
            "contractId": contract.contract_id,
            "klineType": interval,
            "priceType": "LAST_PRICE",
            "size": str(PAGE_SIZE),
            "filterBeginKlineTimeInclusive": str(cursor),
            "filterEndKlineTimeExclusive": str(chunk_end),
        }
        payload = fetch_json("/api/v2/public/quote/getKline", base_params)
        data = payload.get("data") or {}
        rows = data.get("dataList") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            candle = Candle.from_payload(row, fallback_interval=interval)
            if candle is not None and begin_ms <= candle.time_ms < end_ms:
                by_time[candle.time_ms] = candle

        offset = str(data.get("nextPageOffsetData") or "")
        seen_offsets: set[str] = set()
        while offset and offset not in seen_offsets:
            seen_offsets.add(offset)
            page = fetch_json(
                "/api/v2/public/quote/getKline",
                {**base_params, "offsetData": offset},
            )
            page_data = page.get("data") or {}
            for row in page_data.get("dataList") or []:
                if not isinstance(row, dict):
                    continue
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


def simulate_trade(
    signal: Any,
    entries: list[Candle],
    entry_index: int,
    end_ms: int,
) -> dict[str, Any]:
    entry = signal.candle.close
    stop = float(signal.stop_loss_override)
    target = float(signal.take_profit_override)
    rr = float(signal.rr)
    split = len(signal.split_targets) >= 2
    tp1 = signal.split_targets[0][1] if split else None
    tp1_done = False
    last_close = entry

    result = {
        "setup_key": signal.key,
        "contract_id": signal.contract.contract_id,
        "symbol": signal.contract.contract_name,
        "direction": "LONG" if signal.direction == "up" else "SHORT",
        "entry_time_ms": signal.candle.time_ms + ENTRY_MS,
        "breakout_time_ms": signal.breakout_time_ms,
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

    mark = mark_r(signal.direction, entry, stop, last_close)
    if split and tp1_done:
        mark = 1.0 + 0.5 * mark
    result["mark_r"] = mark
    return result


def backtest_contract(
    contract: Contract,
    settings: Settings,
    period_start_ms: int,
    period_end_ms: int,
    holdout_start_ms: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    monitor_begin = period_start_ms - 14 * DAY_MS
    entry_begin = period_start_ms - 2 * DAY_MS
    monitor = fetch_klines(
        contract, MONITOR_INTERVAL, monitor_begin, period_end_ms, MONITOR_CHUNK_MS
    )
    entries = fetch_klines(
        contract, ENTRY_INTERVAL, entry_begin, period_end_ms, ENTRY_CHUNK_MS
    )

    detectors = {v: VariantDetector(settings, v) for v in VARIANTS}
    full_trades: dict[str, list[dict[str, Any]]] = {v: [] for v in VARIANTS}
    pre_holdout_trades: dict[str, list[dict[str, Any]]] = {v: [] for v in VARIANTS}
    monitor_end = 0
    seen_setup_keys: dict[str, set[str]] = {v: set() for v in VARIANTS}

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
        for variant, detector in detectors.items():
            signal = detector.detect(contract, monitor_history, entry_history, candidate)
            if signal is None or signal.key in seen_setup_keys[variant]:
                continue
            seen_setup_keys[variant].add(signal.key)
            trade = simulate_trade(signal, entries, i, period_end_ms)
            trade["variant"] = variant
            full_trades[variant].append(trade)
            if candidate_close_ms < holdout_start_ms:
                pre_trade = simulate_trade(signal, entries, i, holdout_start_ms)
                pre_trade["variant"] = variant
                pre_holdout_trades[variant].append(pre_trade)

    coverage = {
        "symbol": contract.contract_name,
        "contract_id": contract.contract_id,
        "monitor_bars": len(monitor),
        "entry_bars": len(entries),
        "first_entry_bar_ms": entries[0].time_ms if entries else None,
        "last_entry_bar_ms": entries[-1].time_ms if entries else None,
        "signals_180d": {v: len(full_trades[v]) for v in VARIANTS},
        "signals_pre_holdout": {v: len(pre_holdout_trades[v]) for v in VARIANTS},
    }
    return full_trades, pre_holdout_trades, coverage


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def compact_summary(
    trades: Iterable[dict[str, Any]],
    starting_equity: float,
    risk_fraction: float,
) -> dict[str, Any]:
    rows = list(trades)
    closed = [trade for trade in rows if trade["r_result"] is not None]
    open_trades = [trade for trade in rows if trade["r_result"] is None]
    wins = [trade for trade in closed if trade["r_result"] > 0]
    losses = [trade for trade in closed if trade["r_result"] < 0]
    gross_profit_r = sum(float(trade["r_result"]) for trade in wins)
    gross_loss_r = -sum(float(trade["r_result"]) for trade in losses)
    net_r = sum(float(trade["r_result"]) for trade in closed)

    max_loss_streak = 0
    current_loss_streak = 0
    equity = starting_equity
    peak = starting_equity
    max_drawdown = 0.0
    for trade in sorted(closed, key=lambda item: (item["entry_time_ms"], item["symbol"])):
        r_value = float(trade["r_result"])
        if r_value < 0:
            current_loss_streak += 1
            max_loss_streak = max(max_loss_streak, current_loss_streak)
        else:
            current_loss_streak = 0
        equity *= 1.0 + risk_fraction * r_value
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

    return {
        "signals_total": len(rows),
        "closed_trades": len(closed),
        "open_trades": len(open_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / len(closed) * 100.0) if closed else None,
        "net_r": net_r,
        "avg_r": (net_r / len(closed)) if closed else None,
        "gross_profit_r": gross_profit_r,
        "gross_loss_r": gross_loss_r,
        "profit_factor": (
            gross_profit_r / gross_loss_r if gross_loss_r > 0 else None
        ),
        "avg_planned_rr": (
            sum(float(trade["rr_planned"]) for trade in rows) / len(rows)
            if rows
            else None
        ),
        "split_trade_count": sum(1 for trade in rows if trade["split"]),
        "max_loss_streak": max_loss_streak,
        "starting_equity_usdc": starting_equity,
        "indicative_ending_equity_usdc": equity,
        "indicative_return_pct": (equity / starting_equity - 1.0) * 100.0,
        "indicative_max_drawdown_pct": max_drawdown * 100.0,
        "open_mark_r": sum(float(trade["mark_r"]) for trade in open_trades),
    }


def risk_sensitivity(trades: list[dict[str, Any]], starting_equity: float) -> list[dict[str, Any]]:
    return [
        {
            "risk_pct": risk * 100.0,
            **compact_summary(trades, starting_equity, risk),
        }
        for risk in (0.01, 0.02, 0.03, 0.05)
    ]


def grouped_summary(
    trades: list[dict[str, Any]],
    key_fn,
    starting_equity: float,
    risk_fraction: float,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[str(key_fn(trade))].append(trade)
    return [
        {
            "group": key,
            **compact_summary(rows, starting_equity, risk_fraction),
        }
        for key, rows in sorted(groups.items())
    ]


def top_symbols(
    trades: list[dict[str, Any]],
    min_closed: int = 10,
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        groups[trade["symbol"]].append(trade)
    stats = []
    for symbol, rows in groups.items():
        summary = compact_summary(rows, 24.0, 0.05)
        if summary["closed_trades"] >= min_closed:
            stats.append({"symbol": symbol, **summary})
    stats.sort(key=lambda item: float(item["net_r"]))
    return {
        "worst": stats[:15],
        "best": list(reversed(stats[-15:])),
    }


def enrich_times(trades: list[dict[str, Any]]) -> None:
    for trade in trades:
        trade["entry_time_utc"] = iso(trade["entry_time_ms"])
        trade["exit_time_utc"] = iso(trade["exit_time_ms"])
        trade["breakout_time_utc"] = iso(trade["breakout_time_ms"])


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
    period_start_ms = period_end_ms - PERIOD_DAYS * DAY_MS
    holdout_start_ms = period_end_ms - HOLDOUT_DAYS * DAY_MS

    full_trades: dict[str, list[dict[str, Any]]] = {v: [] for v in VARIANTS}
    pre_holdout_trades: dict[str, list[dict[str, Any]]] = {v: [] for v in VARIANTS}
    coverage: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    contract_list = sorted(contracts.values(), key=lambda contract: contract.contract_name)
    print(f"Backtesting 4H-exit strategy on {len(contract_list)} currently tradable contracts", flush=True)
    print(
        f"Period UTC: {iso(period_start_ms)} -> {iso(period_end_ms)}; "
        f"pre-holdout ends {iso(holdout_start_ms)}",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(
                backtest_contract,
                contract,
                settings,
                period_start_ms,
                period_end_ms,
                holdout_start_ms,
            ): contract
            for contract in contract_list
        }
        done = 0
        for future in as_completed(futures):
            contract = futures[future]
            done += 1
            try:
                trades, pre_trades, cov = future.result()
                for v in VARIANTS:
                    full_trades[v].extend(trades[v])
                    pre_holdout_trades[v].extend(pre_trades[v])
                coverage.append(cov)
                print(
                    f"[{done}/{len(contract_list)}] {contract.contract_name}: "
                    f"{cov['entry_bars']} 15m, {cov['monitor_bars']} 4h, "
                    f"{cov['signals_180d']} setups",
                    flush=True,
                )
            except Exception as exc:
                errors.append(
                    {
                        "symbol": contract.contract_name,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                print(
                    f"[{done}/{len(contract_list)}] {contract.contract_name}: ERROR {exc}",
                    flush=True,
                )

    coverage.sort(key=lambda item: item["symbol"])
    starting_equity = 200.0
    risk_fraction = settings.risk_per_trade
    variants = {}
    for v in VARIANTS:
        full_trades[v].sort(key=lambda item: (item["entry_time_ms"], item["symbol"]))
        pre_holdout_trades[v].sort(key=lambda item: (item["entry_time_ms"], item["symbol"]))
        holdout = [t for t in full_trades[v] if t["entry_time_ms"] >= holdout_start_ms]
        enrich_times(full_trades[v]); enrich_times(pre_holdout_trades[v])
        variants[v] = {
            "summary_180d": compact_summary(full_trades[v], starting_equity, risk_fraction),
            "summary_pre_holdout_150d": compact_summary(pre_holdout_trades[v], starting_equity, risk_fraction),
            "summary_latest_30d": compact_summary(holdout, starting_equity, risk_fraction),
            "risk_sensitivity_180d": risk_sensitivity(full_trades[v], starting_equity),
        }

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "period_start_utc": iso(period_start_ms),
        "holdout_start_utc": iso(holdout_start_ms),
        "period_end_utc": iso(period_end_ms),
        "period_days": PERIOD_DAYS,
        "holdout_days": HOLDOUT_DAYS,
        "universe": "contracts tradable on EdgeX at backtest run time",
        "hypothesis": "Improve 15M entry confirmation quality while keeping all 4H exit/trend/RR parameters fixed.",
        "variant_definitions": {
            "baseline": "current bullish/bearish close on correct side of roll level",
            "reclaim": "candidate pierces roll level intrabar and closes back on trend side",
            "wick_rejection": "candidate tests roll level within tolerance and rejection wick >= candle body",
            "reclaim_strong_body": "reclaim plus directional body >= 50% of candle range"
        },
        "variants": variants,
        "coverage": {"contracts_total": len(contract_list), "contracts_completed": len(coverage), "contracts_failed": len(errors)},
        "errors": errors,
    }

    out_dir = Path("backtest_results")
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "entry_confirmation_quality_summary.json"
    csv_path = out_dir / "entry_confirmation_quality_trades.csv"
    summary_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    fields = [
        "variant",
        "setup_key",
        "symbol",
        "contract_id",
        "direction",
        "breakout_time_utc",
        "entry_time_utc",
        "entry",
        "stop",
        "target",
        "rr_planned",
        "split",
        "exit_time_utc",
        "exit_reason",
        "r_result",
        "mark_r",
        "roll_level",
        "atr_15m",
        "atr_4h",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for v in VARIANTS:
            for trade in full_trades[v]:
                writer.writerow({field: trade.get(field) for field in fields})

    print("VARIANTS", json.dumps(variants, ensure_ascii=False), flush=True)
    print("COVERAGE", json.dumps(result["coverage"], ensure_ascii=False), flush=True)
    print("ERRORS", json.dumps(errors[:20], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
