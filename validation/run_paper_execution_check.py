"""Offline execution check using a recorded CURRENT READY and public 15M bars.

python -m validation.run_paper_execution_check --source validation/fixtures/velvet_ready_execution.json --output /tmp/paper-check
Add --server-hook with analysis_terminal/test-requirements.txt installed to exercise
the actual collector hook, additive SQLite migration and read-only API as well.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import app as scanner
from analysis_terminal import paper_execution as paper
from analysis_terminal.setups import setup_identity

DATASET = "RETROSPECTIVE_EXECUTION_CHECK"
STEP = 900_000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_capture(path):
    raw = path.read_bytes()
    capture = json.loads(raw)
    require(capture.get("dataset") == "RECORDED_READY_EXECUTION_FIXTURE", "Unknown source dataset")
    s, m, response = capture["signal"], capture["contract"], capture["kline_response"]
    require(s.get("stage") == "READY" and s.get("eligible") is True and s.get("source") == "server-background"
            and s.get("key") == "current:" + s.get("setup_id", ""), "Only recorded current READY is eligible")
    require(s["created_ms"] == s["source_candle_ms"] + STEP + 1, "Unexpected recorded signal clock")
    contract = scanner.Contract(str(m["contractId"]), m["contractName"], "USDC", m["enableTrade"], m["enableDisplay"],
        float(m["stepSize"]), float(m["minOrderSize"]), float(m["maxOrderSize"]))
    require(contract.contract_name == s["ticker"] and contract.enable_trade is True, "Contract mismatch")
    require(response.get("code") == "SUCCESS" and not response["data"].get("nextPageOffsetData"), "Incomplete public history")
    bars = []
    for v in response["data"]["dataList"]:
        c = scanner.Candle.from_payload(v)
        require(c is not None and v.get("priceType") == "LAST_PRICE"
                and c.contract_id == contract.contract_id and c.contract_name == s["ticker"]
                and c.interval == "MINUTE_15" and c.time_ms % STEP == 0, "Invalid public candle identity")
        values = [Decimal(str(x)) for x in (c.open, c.high, c.low, c.close)]
        require(all(x.is_finite() and x > 0 for x in values)
                and c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high, "Invalid public OHLC")
        bars.append(c)
    bars.sort(key=lambda c: c.time_ms)
    require(bool(bars) and bars[0].time_ms == s["source_candle_ms"], "Signal candle exclusion control missing")
    require(all(b.time_ms - a.time_ms == STEP for a, b in zip(bars, bars[1:])), "Missing or duplicate public candle")
    row = dict(ticker=s["ticker"], direction=s["side"], stage=s["stage"], setup_id=s["setup_id"],
        breakout_time_ms=s["breakout_time_ms"], breakout_level=s["breakout_level"],
        latest_15m_time_ms=s["source_candle_ms"], entry_reference=s["entry"], stop_loss=s["stop"],
        take_profit=s["target"], score=s["score"], contract_id=contract.contract_id,
        step_size=contract.step_size, min_order_size=contract.min_order_size, max_order_size=contract.max_order_size)
    require(setup_identity(row) == s["setup_id"], "Recorded setup identity mismatch")
    require(abs(s["target"] - s["entry"]) / abs(s["entry"] - s["stop"]) >= 2, "Recorded READY RR below 2")
    require((s["stop"] < s["entry"] < s["target"] if s["side"] == "LONG"
             else s["target"] < s["entry"] < s["stop"] if s["side"] == "SHORT" else False), "Invalid recorded levels")
    return capture, contract, bars, row, hashlib.sha256(raw).hexdigest()


def visible_bars(bars, now_ms, missing=None):
    """Reveal OHLC only at close; reveal open alone while a foot is forming."""
    result = []
    for c in bars:
        if c.time_ms == missing or c.time_ms > now_ms:
            continue
        result.append(c if c.time_ms + STEP <= now_ms else replace(c, high=c.open, low=c.open,
            close=c.open, volume=0, value=0, trades=0))
    return result


def independent_ledger(row, contract, bars, detected_ms):
    """Decimal cash-flow oracle; no production size, fee or outcome helper calls."""
    d = lambda x: Decimal(str(x))
    side = Decimal(1 if row["direction"] == "LONG" else -1)
    fee, slip, initial, budget = d("0.0005"), d("0.0002"), d(10000), d(100)
    stop, target, step = d(row["stop_loss"]), d(row["take_profit"]), d(contract.step_size)
    reference = d(row["entry_reference"]) * (1 + side * slip)
    stop_exit = stop * (1 - side * slip)
    cost = lambda price: abs(price - stop_exit) + (price + stop_exit) * fee
    floor = lambda qty: (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step
    committed_qty = floor(min(budget / cost(reference), initial / (reference * (1 + 2 * fee)), d(contract.max_order_size)))
    committed_risk, committed_notional = committed_qty * cost(reference), committed_qty * reference
    fill_ms = (detected_ms // STEP + 1) * STEP
    foot = next(c for c in bars if c.time_ms == fill_ms)
    price = d(foot.open) * (1 + side * slip)
    quantity = floor(min(committed_qty, committed_risk / cost(price), committed_notional / price))
    require(quantity >= d(contract.min_order_size), "Oracle quantity too small")
    rr = abs(target - price) / abs(price - stop)
    require(rr >= 2, "Oracle fill fails unchanged RR")
    entry_fee = quantity * price * fee
    result = dict(quantity=float(quantity), fill_price=float(price), filled_ms=fill_ms,
        fill_rr=float(rr), entry_fee_usdc=float(entry_fee), reserved_risk_usdc=float(quantity * cost(price)))
    for c in bars:
        if c.time_ms < fill_ms:
            continue
        stop_hit = d(c.low) <= stop if side == 1 else d(c.high) >= stop
        target_hit = d(c.high) >= target if side == 1 else d(c.low) <= target
        if stop_hit and target_hit:
            return dict(result, status="AMBIGUOUS", exit_candle_ms=c.time_ms)
        if not (stop_hit or target_hit):
            continue
        status = "SL" if stop_hit else "TP"
        gap = d(c.open) <= stop if side == 1 else d(c.open) >= stop
        gap = gap if status == "SL" else (d(c.open) >= target if side == 1 else d(c.open) <= target)
        trigger = d(c.open) if gap else stop if status == "SL" else target
        exit_price = trigger * (1 - side * slip)
        gross, exit_fee = side * quantity * (exit_price - price), quantity * exit_price * fee
        net = gross - entry_fee - exit_fee
        return dict(result, status=status, exit_candle_ms=c.time_ms, exit_price=float(exit_price),
            gross_pnl_usdc=float(gross), exit_fee_usdc=float(exit_fee), net_pnl_usdc=float(net),
            net_r=float(net / (quantity * cost(price))), final_cash_usdc=float(initial + net))
    return dict(result, status="OPEN")


class Runner:
    def __init__(self, folder, capture, contract, row, *, server_hook=False):
        folder.mkdir()
        self.db = folder / "simulation.db"
        self.capture, self.contract, self.row = capture, contract, row
        self.trace, self.server, self.stack = [], None, ExitStack()
        if server_hook:
            # No lifespan, collector task, EdgeX HTTP request or notification starts.
            from analysis_terminal import server
            self.server = server
            self.stack.enter_context(patch.object(server, "DB_PATH", self.db))
            self.stack.enter_context(patch.object(server.CLIENT, "_get_json_sync", side_effect=AssertionError("Offline check forbids network")))
            self.stack.enter_context(patch.object(server.CLIENT, "_get_private_json_sync", side_effect=AssertionError("No private API in validation")))
            require(server.SETTINGS.min_rr == 2, "Production min_rr differs from the check protocol")
        self.initialize(capture["signal"]["created_ms"] - 1)

    def initialize(self, now_ms, previous=()):
        if self.server:
            with patch.object(self.server.time, "time", return_value=now_ms / 1000):
                self.server._init_db()
        else:
            with sqlite3.connect(self.db) as conn:
                paper.initialize(conn, now_ms=now_ms, previous_signals=previous, min_rr=2)

    def report(self, now_ms, limit=200):
        if self.server:
            import httpx
            async def request():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://isolated-check") as client:
                    response = await client.get("/api/paper-execution", params={"limit":limit})
                    require(response.status_code == 200, "Isolated GET API failed")
                    return response.json()
            with patch.object(self.server.time, "time", return_value=now_ms / 1000):
                return asyncio.run(request())
        with sqlite3.connect(self.db) as conn:
            return paper.report(conn, now_ms=now_ms, limit=limit)

    def tick(self, now_ms, bars, *, rows=(), age_ms=0):
        if self.server:
            cache = {(self.contract.contract_id, "MINUTE_15"): bars}
            with patch.object(self.server, "_snapshot_cache", ((now_ms-age_ms)/1000, cache)), patch.object(self.server.time, "time", return_value=now_ms/1000):
                self.server._simulation_cycle({self.contract.contract_id: self.contract}, list(rows))
        else:
            with sqlite3.connect(self.db) as conn:
                conn.execute("BEGIN IMMEDIATE")
                paper.cycle(conn, rows=list(rows), candles_by_ticker={self.row["ticker"]: bars}, now_ms=now_ms, snapshot_age_ms=age_ms)
        report = self.report(now_ms)
        require(report["account"]["last_error"] is None and not report["account"]["paused"], "Simulation failed or paused")
        require(report["metrics"]["records"] == 1, "Duplicate or missing order")
        order = report["latest"][0]
        require(order["stop"] == self.row["stop_loss"] and order["target"] == self.row["take_profit"], "Frozen SL/TP changed")
        if order.get("last_closed_candle_ms") is not None:
            require(order["last_closed_candle_ms"] >= order["filled_ms"] > order["source_candle_ms"] + STEP, "Pre-entry candle used")
        self.trace.append(dict(now_ms=now_ms, status=order["status"], quality=order["quality"],
            last_closed_candle_ms=order.get("last_closed_candle_ms"), cash_usdc=report["account"]["cash_usdc"]))
        return report

    def close(self):
        self.stack.close()


def scenario(folder, capture, contract, bars, row, *, server_hook=False, gap=False, latency_ms=1, stale=False):
    observed = row["latest_15m_time_ms"] + STEP + latency_ms
    runner = Runner(folder, capture, contract, row, server_hook=server_hook)
    try:
        oracle = independent_ledger(row, contract, bars, observed) if not stale else None
        result = runner.tick(observed, visible_bars(bars, observed), rows=[row])
        require(result["latest"][0]["status"] == ("REJECTED" if stale else "PENDING"), "Unexpected initial intent")
        if stale:
            require(result["latest"][0]["reason"] == "STALE_OR_UNKNOWN_DATA", "Stale detection accepted")
        else:
            missing = oracle["filled_ms"] + STEP if gap else None
            for c in bars:
                now = c.time_ms + 1
                if now <= observed:
                    continue
                shown = visible_bars(bars, now, missing=missing)
                result = runner.tick(now, shown, rows=[row])
                if now == oracle["filled_ms"] + 1:
                    require(result["latest"][0]["status"] == "OPEN", "No fill at next open")
                    require(result["latest"][0].get("last_closed_candle_ms") is None, "Forming high/low used at fill")
                    runner.initialize(now, previous=[capture["signal"]])
                if gap and missing is not None and now == missing + STEP + 1:
                    require(result["latest"][0]["quality"] == "HISTORY_GAP", "Missing closed foot was skipped")
                    held = result["latest"][0]["next_candle_ms"]
                    result = runner.tick(now+1, visible_bars(bars, now+1), rows=[row])
                    require(result["latest"][0]["next_candle_ms"] > held, "Public data restoration did not recover")
                    missing = None
            final_now = bars[-1].time_ms + STEP + 1
            result = runner.tick(final_now, visible_bars(bars, final_now), rows=[row])
            order = result["latest"][0]
            require(order["status"] == oracle["status"], "Outcome mismatch")
            for key, value in oracle.items():
                if key in {"status", "final_cash_usdc"}:
                    continue
                require(abs(order[key]-value) < 1e-8, "Independent Decimal mismatch: "+key)
            require(abs(result["account"]["cash_usdc"]-oracle["final_cash_usdc"]) < 1e-8, "Cash mismatch")
            before = json.dumps(order, sort_keys=True)
            runner.initialize(final_now+1, previous=[capture["signal"]])
            duplicate = runner.tick(final_now+1000, visible_bars(bars, final_now+1000), rows=[row, row])
            require(json.dumps(duplicate["latest"][0], sort_keys=True) == before, "Restart/repetition changed settlement")
            require(duplicate["account"]["cash_usdc"] == result["account"]["cash_usdc"], "Fee or settlement counted twice")
            raw_db = runner.db.read_bytes()
            require(runner.report(final_now+1000, limit=1)["metrics"] == duplicate["metrics"], "Read limit changed cohort")
            require(runner.db.read_bytes() == raw_db, "Read-only report changed SQLite")
        payload = dict(backend="SERVER_COLLECTOR_HOOK_AND_API" if server_hook else "PRODUCTION_ENGINE",
            detected_ms=observed, detection_latency_ms=latency_ms, gap_control=gap,
            trace=runner.trace, independent_decimal=oracle, final=result)
        (folder / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
        return payload
    finally:
        runner.close()


def run(source, output, *, server_hook=False):
    capture, contract, bars, row, source_hash = load_capture(source)
    root = output.resolve()
    require(root != Path("/") and Path("/data") not in [root, *root.parents], "Production /data is forbidden")
    root.mkdir(parents=True, exist_ok=False)
    (root / "source.json").write_bytes(source.read_bytes())
    checks = {"nominal": scenario(root/"nominal", capture, contract, bars, row),
              "missing_bar_recovery": scenario(root/"missing-bar", capture, contract, bars, row, gap=True),
              "detected_30s": scenario(root/"detected-30s", capture, contract, bars, row, latency_ms=30000),
              "detected_119999ms": scenario(root/"detected-119999ms", capture, contract, bars, row, latency_ms=119999),
              "stale_120s_rejected": scenario(root/"stale-120s", capture, contract, bars, row, latency_ms=120000, stale=True)}
    if server_hook:
        checks["server_hook_and_api"] = scenario(root/"server-hook", capture, contract, bars, row, server_hook=True)
    baseline = root/"cold-start-baseline.db"
    with sqlite3.connect(baseline) as conn:
        paper.initialize(conn, now_ms=capture["signal"]["created_ms"]+STEP, previous_signals=[capture["signal"]], min_rr=2)
        o = paper.orders(conn)[0]
        require(o["status"] == "BASELINED" and "filled_ms" not in o, "Cold start retroactively filled a stored signal")
    report = dict(dataset=DATASET, eligible_for_live_promotion=False, automatic_promotion=False,
        changes_live_results=False, real_orders_enabled=False, verdict="PASS", source_sha256=source_hash,
        engine_sha256=hashlib.sha256(Path(paper.__file__).read_bytes()).hexdigest(),
        sample_basis="one recorded CURRENT READY reused in execution-control scenarios; not independent trades",
        unique_recorded_ready=1, ticker=row["ticker"], public_bars=len(bars), assumptions=capture["assumptions"],
        cold_start_baselined=True, checks={k:{"backend":v["backend"], "status":v["final"]["latest"][0]["status"],
            "net_pnl_usdc":v["final"]["metrics"]["net_pnl_usdc"], "resolved":v["final"]["metrics"]["resolved"]} for k,v in checks.items()},
        nominal_order=checks["nominal"]["final"]["latest"][0], independent_decimal=checks["nominal"]["independent_decimal"],
        validation_samples_added_to_production=0)
    (root / "execution-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    print(json.dumps({k:report[k] for k in ("dataset","verdict","unique_recorded_ready","ticker","public_bars","checks")},ensure_ascii=False))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server-hook", action="store_true")
    args=parser.parse_args()
    run(args.source, args.output, server_hook=args.server_hook)


if __name__ == "__main__":
    main()
