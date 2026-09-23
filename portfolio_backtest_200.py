#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_URL = "https://edgex-prod-v2.edgex.exchange"
STARTING_EQUITY = 200.0
LIVE_DEFAULT_TAKER_FALLBACK = 0.00045
HELP_ARTICLE_TAKER = 0.00038
US_STOCK_HOLIDAYS_2026 = {
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day observed
    "2026-09-07",  # Labor Day
}


def get_json(path: str, params: dict[str, str] | None = None, retries: int = 6) -> dict[str, Any]:
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    delay = 0.5
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "edgex-portfolio-backtest/1.0"},
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
            delay = min(delay * 2, 8.0)
    raise RuntimeError("unreachable")


def load_trades() -> list[dict[str, Any]]:
    path = Path("backtest_results/deduped_180d_trades.csv")
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            row["entry_time_ms"] = int(datetime.fromisoformat(row["entry_time_utc"]).timestamp() * 1000)
            row["exit_time_ms"] = (
                int(datetime.fromisoformat(row["exit_time_utc"]).timestamp() * 1000)
                if row.get("exit_time_utc")
                else None
            )
            for key in ("entry", "stop", "target", "rr_planned", "roll_level", "atr_15m", "atr_4h"):
                row[key] = float(row[key])
            row["r_result"] = float(row["r_result"]) if row.get("r_result") else None
            row["mark_r"] = float(row["mark_r"]) if row.get("mark_r") else 0.0
            row["split"] = str(row["split"]).lower() == "true"
            rows.append(row)
    rows.sort(key=lambda x: (x["entry_time_ms"], x["symbol"]))
    return rows


def load_metadata() -> dict[str, dict[str, Any]]:
    payload = get_json("/api/v2/public/meta/getMetaData")
    rows = (payload.get("data") or {}).get("contractList") or []
    return {str(row["contractId"]): row for row in rows if isinstance(row, dict) and row.get("contractId")}


def fetch_funding(contract_id: str, begin_ms: int, end_ms: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    offset = ""
    seen: set[str] = set()
    while True:
        params = {
            "size": "1000",
            "contractId": contract_id,
            "filterSettlementFundingRate": "true",
            "filterBeginTimeInclusive": str(begin_ms),
            "filterEndTimeExclusive": str(end_ms + 1),
        }
        if offset:
            params["offsetData"] = offset
        payload = get_json("/api/v2/public/funding/getFundingRatePage", params)
        data = payload.get("data") or {}
        for row in data.get("dataList") or []:
            try:
                result.append(
                    {
                        "time_ms": int(row.get("fundingTimestamp") or row.get("fundingTime")),
                        "rate": float(row["fundingRate"]),
                        "oracle_price": float(row.get("oraclePrice") or row.get("indexPrice") or 0),
                    }
                )
            except (TypeError, ValueError, KeyError):
                continue
        next_offset = str(data.get("nextPageOffsetData") or "")
        if not next_offset or next_offset in seen:
            break
        seen.add(next_offset)
        offset = next_offset
    result.sort(key=lambda x: x["time_ms"])
    return result


def load_funding_for_traded_contracts(
    trades: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    begin_ms = min(t["entry_time_ms"] for t in trades) - 24 * 60 * 60 * 1000
    end_ms = max((t["exit_time_ms"] or t["entry_time_ms"]) for t in trades) + 24 * 60 * 60 * 1000
    ids = sorted({str(t["contract_id"]) for t in trades})
    funding: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_funding, cid, begin_ms, end_ms): cid for cid in ids}
        for future in as_completed(futures):
            cid = futures[future]
            try:
                funding[cid] = future.result()
            except Exception as exc:
                funding[cid] = []
                errors.append({"contract_id": cid, "error": f"{type(exc).__name__}: {exc}"})
    return funding, errors


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor((value + 1e-12) / step) * step


def stock_market_closed_approx(entry_ms: int) -> bool:
    dt = datetime.fromtimestamp(entry_ms / 1000, tz=timezone.utc)
    return dt.weekday() >= 5 or dt.date().isoformat() in US_STOCK_HOLIDAYS_2026


def adverse_fill(price: float, direction: str, is_entry: bool, slip: float) -> float:
    if slip <= 0:
        return price
    if direction == "LONG":
        return price * (1.0 + slip if is_entry else 1.0 - slip)
    return price * (1.0 - slip if is_entry else 1.0 + slip)


def exit_legs(trade: dict[str, Any]) -> list[tuple[float, float]]:
    """Return (fraction, planned_exit_price) for closed trades."""
    if trade["r_result"] is None:
        return []
    direction = trade["direction"]
    entry = trade["entry"]
    stop = trade["stop"]
    target = trade["target"]
    risk = abs(entry - stop)
    sign = 1.0 if direction == "LONG" else -1.0
    tp1 = entry + sign * 2.0 * risk
    reason = trade["exit_reason"]

    if not trade["split"]:
        return [(1.0, stop if reason.startswith("SL") else target)]

    if reason in {"SL", "SL_BOTH_HIT"}:
        return [(1.0, stop)]
    if reason in {"TP1_THEN_SL", "TP1_THEN_SL_BOTH_HIT"}:
        return [(0.5, tp1), (0.5, stop)]
    return [(0.5, tp1), (0.5, target)]


def funding_r_per_unit(
    trade: dict[str, Any],
    funding_rows: list[dict[str, Any]],
    mode: str,
) -> float:
    """Funding PnL per 1 unit of contract, in quote currency.

    Conservative mode ignores favorable funding and keeps full size until final exit,
    intentionally overstating funding cost for split trades after TP1.
    """
    exit_ms = trade["exit_time_ms"]
    if exit_ms is None:
        return 0.0
    long_side = trade["direction"] == "LONG"
    pnl = 0.0
    for row in funding_rows:
        if not (trade["entry_time_ms"] < row["time_ms"] <= exit_ms):
            continue
        signed_pnl = (-1.0 if long_side else 1.0) * row["oracle_price"] * row["rate"]
        if mode == "adverse_only":
            pnl += min(0.0, signed_pnl)
        else:
            pnl += signed_pnl
    return pnl


def compute_trade_pnl(
    trade: dict[str, Any],
    qty: float,
    fee_rate: float,
    slippage: float,
    funding_rows: list[dict[str, Any]],
    funding_mode: str,
) -> dict[str, float]:
    entry_fill = adverse_fill(trade["entry"], trade["direction"], True, slippage)
    entry_fee = qty * entry_fill * fee_rate
    gross = 0.0
    exit_fees = 0.0
    for fraction, planned_exit in exit_legs(trade):
        exit_fill = adverse_fill(planned_exit, trade["direction"], False, slippage)
        signed_move = (
            exit_fill - entry_fill
            if trade["direction"] == "LONG"
            else entry_fill - exit_fill
        )
        leg_qty = qty * fraction
        gross += leg_qty * signed_move
        exit_fees += leg_qty * exit_fill * fee_rate

    funding_pnl = qty * funding_r_per_unit(trade, funding_rows, funding_mode)
    fees = entry_fee + exit_fees
    net = gross - fees + funding_pnl
    return {
        "gross_pnl": gross,
        "fees": fees,
        "funding_pnl": funding_pnl,
        "net_pnl": net,
        "entry_fill": entry_fill,
    }


def simulate(
    trades: list[dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    funding: dict[str, list[dict[str, Any]]],
    *,
    risk_per_trade: float,
    portfolio_risk_cap: float,
    slippage: float,
    fee_mode: str,
    funding_mode: str,
    stock_mode: str,
    direction_cap_fraction: float | None = None,
) -> dict[str, Any]:
    equity = STARTING_EQUITY
    peak = equity
    max_dd = 0.0
    active: dict[str, dict[str, Any]] = {}
    active_by_symbol: dict[str, str] = {}
    accepted: list[dict[str, Any]] = []
    skip = defaultdict(int)
    max_concurrent = 0
    max_reserved_risk_pct = 0.0
    total_fees = 0.0
    total_funding = 0.0
    total_slippage_impact = 0.0
    gross_pnl_total = 0.0
    net_pnl_total = 0.0
    wins = losses = 0
    net_r_sum = 0.0
    max_loss_streak = current_loss_streak = 0

    def close_due(now_ms: int) -> None:
        nonlocal equity, peak, max_dd, total_fees, total_funding
        nonlocal total_slippage_impact, gross_pnl_total, net_pnl_total
        nonlocal wins, losses, net_r_sum, max_loss_streak, current_loss_streak
        due = sorted(
            (
                position for position in active.values()
                if position["trade"]["exit_time_ms"] is not None
                and position["trade"]["exit_time_ms"] <= now_ms
            ),
            key=lambda p: (p["trade"]["exit_time_ms"], p["trade"]["symbol"]),
        )
        for position in due:
            trade = position["trade"]
            cid = str(trade["contract_id"])
            fee_rate = position["fee_rate"]
            result = compute_trade_pnl(
                trade,
                position["qty"],
                fee_rate,
                slippage,
                funding.get(cid, []),
                funding_mode,
            )
            no_slip = compute_trade_pnl(
                trade,
                position["qty"],
                fee_rate,
                0.0,
                funding.get(cid, []),
                funding_mode,
            )
            equity += result["net_pnl"]
            total_fees += result["fees"]
            total_funding += result["funding_pnl"]
            total_slippage_impact += result["net_pnl"] - no_slip["net_pnl"]
            gross_pnl_total += result["gross_pnl"]
            net_pnl_total += result["net_pnl"]
            realized_r = result["net_pnl"] / position["risk_dollars"] if position["risk_dollars"] > 0 else 0.0
            net_r_sum += realized_r
            if result["net_pnl"] > 0:
                wins += 1
                current_loss_streak = 0
            elif result["net_pnl"] < 0:
                losses += 1
                current_loss_streak += 1
                max_loss_streak = max(max_loss_streak, current_loss_streak)
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)
            active.pop(trade["setup_key"], None)
            active_by_symbol.pop(trade["symbol"], None)

    for trade in trades:
        if trade["r_result"] is None:
            continue
        close_due(trade["entry_time_ms"])
        if equity <= 0:
            skip["equity_depleted"] += 1
            break

        cid = str(trade["contract_id"])
        meta = metadata.get(cid)
        if not meta:
            skip["missing_metadata"] += 1
            continue

        if stock_mode == "exclude" and bool(meta.get("isStock")):
            skip["stock_excluded"] += 1
            continue
        if (
            stock_mode == "reject_closed_approx"
            and bool(meta.get("isStock"))
            and stock_market_closed_approx(trade["entry_time_ms"])
        ):
            skip["stock_market_closed_approx"] += 1
            continue
        if trade["symbol"] in active_by_symbol:
            skip["symbol_already_open"] += 1
            continue

        risk_dollars_target = equity * risk_per_trade
        stop_distance = abs(trade["entry"] - trade["stop"])
        if stop_distance <= 0:
            skip["invalid_stop"] += 1
            continue
        theoretical_qty = risk_dollars_target / stop_distance

        step = float(meta.get("stepSize") or 0)
        min_order = float(meta.get("minOrderSize") or 0)
        max_market = float(meta.get("maxMarketPositionSize") or meta.get("maxOrderSize") or theoretical_qty)
        qty = floor_to_step(min(theoretical_qty, max_market), step)
        if qty <= 0 or qty + 1e-12 < min_order:
            skip["below_min_order"] += 1
            continue

        actual_risk = qty * stop_distance
        reserved = sum(p["risk_dollars"] for p in active.values())
        if reserved + actual_risk > equity * portfolio_risk_cap + 1e-9:
            skip["portfolio_risk_cap"] += 1
            continue

        if direction_cap_fraction is not None:
            side_reserved = sum(
                p["risk_dollars"] for p in active.values()
                if p["trade"]["direction"] == trade["direction"]
            )
            side_cap = equity * portfolio_risk_cap * direction_cap_fraction
            if side_reserved + actual_risk > side_cap + 1e-9:
                skip["direction_cluster_cap"] += 1
                continue

        leverage = float(meta.get("defaultLeverage") or 10.0)
        entry_fill = adverse_fill(trade["entry"], trade["direction"], True, slippage)
        initial_margin = qty * entry_fill / max(leverage, 1.0)
        used_margin = sum(p["margin"] for p in active.values())
        if used_margin + initial_margin > equity + 1e-9:
            skip["margin_cap"] += 1
            continue

        if fee_mode == "live_metadata":
            fee_rate = float(meta.get("defaultTakerFeeRate") or LIVE_DEFAULT_TAKER_FALLBACK)
        elif fee_mode == "help_0038":
            fee_rate = HELP_ARTICLE_TAKER
        elif fee_mode == "none":
            fee_rate = 0.0
        else:
            raise ValueError(fee_mode)

        position = {
            "trade": trade,
            "qty": qty,
            "risk_dollars": actual_risk,
            "margin": initial_margin,
            "fee_rate": fee_rate,
        }
        active[trade["setup_key"]] = position
        active_by_symbol[trade["symbol"]] = trade["setup_key"]
        accepted.append(trade)
        max_concurrent = max(max_concurrent, len(active))
        reserved_after = sum(p["risk_dollars"] for p in active.values())
        max_reserved_risk_pct = max(max_reserved_risk_pct, reserved_after / equity * 100.0)

    close_due(10**20)

    closed = wins + losses
    return {
        "starting_equity": STARTING_EQUITY,
        "ending_equity": equity,
        "return_pct": (equity / STARTING_EQUITY - 1.0) * 100.0,
        "max_drawdown_pct": max_dd * 100.0,
        "accepted_trades": len(accepted),
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wins / closed * 100.0 if closed else None,
        "net_r_sum_on_allocated_risk": net_r_sum,
        "avg_net_r": net_r_sum / closed if closed else None,
        "max_loss_streak": max_loss_streak,
        "max_concurrent_positions": max_concurrent,
        "max_reserved_stop_risk_pct": max_reserved_risk_pct,
        "gross_pnl_usd": gross_pnl_total,
        "fees_usd": total_fees,
        "funding_pnl_usd": total_funding,
        "slippage_incremental_pnl_usd": total_slippage_impact,
        "net_pnl_usd": net_pnl_total,
        "skipped": dict(skip),
    }


def main() -> None:
    trades = load_trades()
    metadata = load_metadata()
    funding, funding_errors = load_funding_for_traded_contracts(trades)

    # Main grid: realistic current V2 default taker fee + signed historical funding.
    scenarios: list[dict[str, Any]] = []
    for risk in (0.01, 0.02, 0.03, 0.05):
        for cap in (0.10, 0.15, 0.20):
            for slip in (0.0005, 0.0010, 0.0020):
                result = simulate(
                    trades,
                    metadata,
                    funding,
                    risk_per_trade=risk,
                    portfolio_risk_cap=cap,
                    slippage=slip,
                    fee_mode="live_metadata",
                    funding_mode="signed",
                    stock_mode="all",
                )
                scenarios.append({
                    "name": f"live_fee_risk{risk:.2%}_cap{cap:.0%}_slip{slip:.2%}",
                    "risk_per_trade": risk,
                    "portfolio_risk_cap": cap,
                    "slippage_each_fill": slip,
                    "fee_mode": "live_metadata",
                    "funding_mode": "signed",
                    "stock_mode": "all",
                    **result,
                })

    # Diagnostics around a balanced risk-controlled case.
    diagnostic_specs = [
        ("gross_2pct_cap10", 0.02, 0.10, 0.0, "none", "signed", "all", None),
        ("help_fee_2pct_cap10_slip005", 0.02, 0.10, 0.0005, "help_0038", "signed", "all", None),
        ("live_fee_2pct_cap10_slip005", 0.02, 0.10, 0.0005, "live_metadata", "signed", "all", None),
        ("live_fee_2pct_cap10_slip010", 0.02, 0.10, 0.0010, "live_metadata", "signed", "all", None),
        ("live_fee_2pct_cap10_slip020", 0.02, 0.10, 0.0020, "live_metadata", "signed", "all", None),
        ("adverse_funding_2pct_cap10_slip005", 0.02, 0.10, 0.0005, "live_metadata", "adverse_only", "all", None),
        ("non_stock_2pct_cap10_slip005", 0.02, 0.10, 0.0005, "live_metadata", "signed", "exclude", None),
        ("stock_closed_reject_2pct_cap10_slip005", 0.02, 0.10, 0.0005, "live_metadata", "signed", "reject_closed_approx", None),
        ("direction_cluster_2pct_cap10_slip005", 0.02, 0.10, 0.0005, "live_metadata", "signed", "all", 0.60),
        ("current_5pct_cap10_slip005", 0.05, 0.10, 0.0005, "live_metadata", "signed", "all", None),
        ("current_5pct_cap15_slip005", 0.05, 0.15, 0.0005, "live_metadata", "signed", "all", None),
        ("current_5pct_cap20_slip005", 0.05, 0.20, 0.0005, "live_metadata", "signed", "all", None),
    ]
    diagnostics = []
    for name, risk, cap, slip, fee, fund_mode, stock_mode, dir_cap in diagnostic_specs:
        diagnostics.append({
            "name": name,
            "risk_per_trade": risk,
            "portfolio_risk_cap": cap,
            "slippage_each_fill": slip,
            "fee_mode": fee,
            "funding_mode": fund_mode,
            "stock_mode": stock_mode,
            "direction_cap_fraction": dir_cap,
            **simulate(
                trades,
                metadata,
                funding,
                risk_per_trade=risk,
                portfolio_risk_cap=cap,
                slippage=slip,
                fee_mode=fee,
                funding_mode=fund_mode,
                stock_mode=stock_mode,
                direction_cap_fraction=dir_cap,
            ),
        })

    live_taker_rates = sorted({
        round(float(meta.get("defaultTakerFeeRate") or LIVE_DEFAULT_TAKER_FALLBACK), 8)
        for meta in metadata.values()
    })
    live_maker_rates = sorted({
        round(float(meta.get("defaultMakerFeeRate") or 0.0), 8)
        for meta in metadata.values()
    })
    stock_ids = {cid for cid, meta in metadata.items() if bool(meta.get("isStock"))}
    stock_trade_count = sum(1 for trade in trades if str(trade["contract_id"]) in stock_ids)

    output = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_trades": "backtest_results/deduped_180d_trades.csv",
        "source_trade_count": len(trades),
        "starting_equity_usdc": STARTING_EQUITY,
        "execution_model": {
            "one_position_per_symbol": True,
            "position_size": "equity*risk_per_trade / planned Entry-SL distance, floored to live step size",
            "margin": "live current defaultLeverage; sum initial margins cannot exceed realized equity",
            "market_size_cap": "live current maxMarketPositionSize",
            "fees": "current live contract defaultTakerFeeRate for entry and every exit fill",
            "slippage": "adverse percentage on entry and every exit fill",
            "funding": "historical settled funding; position value uses settlement oracle price",
            "funding_split_caveat": "full original size assumed through final exit; conservative for adverse funding after TP1",
            "stock_closed_approx": "optional stress test rejects stock entries on weekends and four US full-day holidays in sample; not a complete exchange calendar",
        },
        "live_metadata": {
            "taker_fee_rates_seen": live_taker_rates,
            "maker_fee_rates_seen": live_maker_rates,
            "stock_contract_count": len(stock_ids),
            "stock_signal_count": stock_trade_count,
            "funding_contracts_loaded": sum(1 for rows in funding.values() if rows),
            "funding_errors": funding_errors,
        },
        "diagnostics": diagnostics,
        "grid": scenarios,
    }

    out_dir = Path("backtest_results")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "portfolio_realistic_200.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("LIVE_TAKER_RATES", live_taker_rates)
    print("FUNDING", json.dumps({
        "contracts_loaded": output["live_metadata"]["funding_contracts_loaded"],
        "errors": len(funding_errors),
    }))
    for row in diagnostics:
        print("DIAG", json.dumps(row, ensure_ascii=False))
    print("GRID_COUNT", len(scenarios))


if __name__ == "__main__":
    main()
