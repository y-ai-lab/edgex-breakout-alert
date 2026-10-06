"""Preregistered research only: confirmed pullback, costs and capped portfolio.

No server hooks, DB writes, alerts or exchange-order transport. OHLC touch fills
are hypothetical. This module cannot promote a strategy.
"""
from bisect import bisect_right
from collections import Counter
from dataclasses import fields
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import app as scanner
from analysis_terminal.replay import complete_window, rule_fingerprint, strategy_parameters
from analysis_terminal.setups import setup_identity

STEP = 900_000
MODEL = "confirmed_pullback_cost_2r"
MODELS = ("current_next_open", "shadow_v2_next_open", MODEL)
FEE, SLIP, MIN_NET_RR, WAIT_BARS = .0005, .0002, 2.0, 4
PROTOCOL = Path(__file__).with_name("pending_entry_protocol.json")


def research_window(now_ms, origin_ms):
    """Completed UTC days in anchored weekly windows; no moving best period."""
    day, week = 86_400_000, 7*86_400_000
    end = now_ms//day*day
    ready = end > origin_ms
    start = origin_ms+((end-origin_ms-1)//week)*week if ready else origin_ms
    return dict(status="READY" if ready else "WAITING_FOR_FIRST_PROSPECTIVE_DAY",
                start_ms=start,end_ms=end,days=(end-start)//day if ready else 0,
                automatic_promotion=False,real_orders_enabled=False)


def cost_levels(reference, stop, target, side):
    """Adverse execution and fees on both sides, normalized by net stop risk."""
    d = 1 if side == "LONG" else -1
    entry = reference * (1 + d*SLIP)
    stop_exit, target_exit = stop*(1-d*SLIP), target*(1-d*SLIP)
    risk = d*(entry-stop_exit) + FEE*(entry+stop_exit)
    reward = d*(target_exit-entry) - FEE*(entry+target_exit)
    return dict(entry=entry, stop_exit=stop_exit, target_exit=target_exit,
                net_risk=risk, net_reward=reward, net_rr=reward/risk if risk > 0 else None)


def pullback_trigger(stop, target, side):
    if side not in {"LONG", "SHORT"} or not all(math.isfinite(v) and v > 0 for v in (stop, target)):
        return None
    if not (stop < target if side == "LONG" else target < stop):
        return None
    factor = (1-FEE)*(1-SLIP)/((1+FEE)*(1+SLIP))
    if side == "SHORT":
        factor = 1/factor
    trigger = factor*(target+MIN_NET_RR*stop)/(1+MIN_NET_RR)
    levels = cost_levels(trigger, stop, target, side)
    entry = levels["entry"]
    if not (stop < entry < target if side == "LONG" else target < entry < stop):
        return None
    return trigger if levels["net_rr"] is not None and levels["net_rr"] >= MIN_NET_RR-1e-9 else None


def candidate(row, model, *, candle_ms):
    identity, side = setup_identity(row), row.get("direction")
    if not identity or side not in {"LONG", "SHORT"}:
        return None
    if model == MODEL:
        if row.get("retest_touched") is not True or row.get("confirmed") is not True:
            return None
        band = row.get("entry_band") or {}
        stop, target = band.get("structural_stop"), band.get("structural_target")
    elif model == "current_next_open" and row.get("stage") == "READY":
        stop, target = row.get("stop_loss"), row.get("take_profit")
    elif model == "shadow_v2_next_open" and row.get("shadow_v2_ready") is True:
        stop, target = row.get("shadow_stop_loss"), row.get("shadow_v2_target")
    else:
        return None
    values = (stop, target, row.get("entry_reference"), row.get("breakout_level"))
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 for v in values):
        return None
    reference, roll = row["entry_reference"], row["breakout_level"]
    if model == MODEL:
        if not (stop < reference and target > roll if side == "LONG" else stop > reference and target < roll):
            return None
        trigger = pullback_trigger(stop, target, side)
        if trigger is None or not (trigger < reference if side == "LONG" else trigger > reference):
            return None
    else:
        trigger = reference
    return dict(key=model+":"+identity, model=model, setup_id=identity, ticker=row["ticker"],
                side=side, stop=stop, target=target, trigger=trigger,
                signal_candle_ms=candle_ms, created_ms=candle_ms+STEP+1,
                expires_ms=candle_ms+STEP+(WAIT_BARS if model == MODEL else 2)*STEP,
                status="PENDING", filled_ms=None, final_net_r=None,
                outcome_ms=None, mfe_r=0.0, mae_r=0.0)


def evaluate(record, candles, states, *, end_ms, min_rr=2.0):
    """States keyed by prior-bar close: never invalidate using a future 4H close."""
    r = dict(record)
    expected = record["created_ms"]-1
    series = {c.time_ms: c for c in candles if c.time_ms+STEP <= end_ms}
    for stamp in range(expected, end_ms, STEP):
        if r["status"] == "PENDING" and stamp >= r["expires_ms"]:
            r.update(status="EXPIRED", outcome_ms=stamp)
            break
        c = series.get(stamp)
        if c is None:
            r.update(status="DATA_GAP", outcome_ms=stamp+STEP)
            break
        if r["status"] == "PENDING":
            if r["model"] == MODEL:
                if stamp not in states:
                    r.update(status="DATA_GAP", outcome_ms=stamp+STEP)
                    break
                if states[stamp] != r["setup_id"]:
                    r.update(status="INVALIDATED", outcome_ms=stamp)
                    break
                gap_through_stop = c.open <= r["stop"] if r["side"] == "LONG" else c.open >= r["stop"]
                if gap_through_stop:
                    r.update(status="INVALIDATED_GAP", outcome_ms=stamp+STEP)
                    break
                touched = c.low <= r["trigger"] if r["side"] == "LONG" else c.high >= r["trigger"]
                if not touched:
                    continue
                nominal = min(c.open, r["trigger"]) if r["side"] == "LONG" else max(c.open, r["trigger"])
            else:
                nominal = c.open
            levels = cost_levels(nominal, r["stop"], r["target"], r["side"])
            price = levels["entry"]
            valid = r["stop"] < price < r["target"] if r["side"] == "LONG" else r["target"] < price < r["stop"]
            rr = abs(r["target"]-price)/abs(price-r["stop"]) if valid else None
            if not valid or (levels["net_rr"] < MIN_NET_RR-1e-9 if r["model"] == MODEL else rr < min_rr):
                r.update(status="REJECTED_AT_FILL", outcome_ms=stamp+STEP)
                break
            r.update(levels, filled_ms=stamp, status="OPEN", nominal_fill=nominal, gross_rr=rr)
        long = r["side"] == "LONG"
        sl, tp = (c.low <= r["stop"], c.high >= r["target"]) if long else (c.high >= r["stop"], c.low <= r["target"])
        if r["model"] == MODEL and stamp == r["filled_ms"]:
            # Even a lone TP can precede the entry. Do not claim its profit.
            if sl or tp:
                r.update(status="AMBIGUOUS", outcome_ms=stamp+STEP,
                         reason="EXIT_TOUCH_ON_UNKNOWN_INTRABAR_FILL", final_net_r=None)
                break
            continue  # Fill-bar extremes before entry cannot be MFE/MAE.
        risk = abs(r["entry"]-r["stop"])
        r["mfe_r"] = max(r["mfe_r"], ((c.high-r["entry"]) if long else (r["entry"]-c.low))/risk)
        r["mae_r"] = max(r["mae_r"], ((r["entry"]-c.low) if long else (c.high-r["entry"]))/risk)
        if sl and tp:
            r.update(status="AMBIGUOUS", outcome_ms=stamp+STEP, reason="TP_AND_SL_SAME_BAR")
            break
        if sl or tp:
            # Gaps are charged at the worse open; favorable TP gaps get no bonus.
            level = r["stop"] if sl else r["target"]
            nominal_exit = min(c.open, level) if sl and long else max(c.open, level) if sl else level
            d = 1 if long else -1
            exit_price = nominal_exit*(1-d*SLIP)
            pnl = d*(exit_price-r["entry"])-FEE*(exit_price+r["entry"])
            r.update(status="SL" if sl else "TP", exit_price=exit_price,
                     net_pnl_per_unit=pnl, final_net_r=pnl/r["net_risk"], outcome_ms=stamp+STEP)
            break
    if r["status"] == "PENDING" and r["expires_ms"] <= end_ms:
        r.update(status="EXPIRED",outcome_ms=r["expires_ms"])
    return r


def replay_market(contract, monitor, entries, analyze, settings, *, start_ms, end_ms, window=180):
    step4 = scanner.INTERVAL_MS[settings.monitor_interval]
    for series, interval in ((monitor, "HOUR_4"), (entries, "MINUTE_15")):
        if any(c.contract_id != contract.contract_id or c.interval != interval or c.time_ms % scanner.INTERVAL_MS[interval] or
               not all(math.isfinite(v) and v > 0 for v in (c.open,c.high,c.low,c.close)) or
               not c.low <= min(c.open,c.close) <= max(c.open,c.close) <= c.high for c in series):
            raise ValueError("Invalid candle identity, grid or OHLC")
        if any(a.time_ms >= b.time_ms for a,b in zip(series,series[1:])):
            raise ValueError("Candle timestamps must be unique and ordered")
    times4, times15 = [c.time_ms+step4 for c in monitor], [c.time_ms+STEP for c in entries]
    seen = {m:set() for m in MODELS}
    records, states, excluded = [], {}, Counter()
    uncertain_through, valid_points = 0, 0
    scan_start = start_ms-settings.roll_max_age*step4-settings.retest_lookback*STEP
    for stamp in range(scan_start, end_ms, STEP):
        as_of = stamp+1
        w4 = complete_window(monitor,bisect_right(times4,as_of),window,step4,as_of)
        w15 = complete_window(entries,bisect_right(times15,as_of),window,STEP,as_of)
        if w4 is None or w15 is None:
            uncertain_through = as_of
            if stamp >= start_ms:
                excluded["incomplete_indicator_window"] += 1
            continue
        row = analyze(contract,w4,w15,as_of_ms=as_of)
        states[stamp] = setup_identity(row)
        valid_points += stamp >= start_ms
        for model in MODELS:
            r = candidate(row,model,candle_ms=w15[-1].time_ms)
            if not r or r["setup_id"] in seen[model]:
                continue
            seen[model].add(r["setup_id"])
            if row["breakout_time_ms"]+step4 <= uncertain_through:
                if stamp >= start_ms:
                    excluded["unknown_first_confirmation_"+model] += 1
                continue
            if stamp < start_ms:
                excluded["warmup_first_confirmation_"+model] += 1
                continue
            r.update(step_size=contract.step_size, min_order_size=contract.min_order_size,
                     max_order_size=contract.max_order_size)
            records.append(r)
    records = [evaluate(r,entries,states,end_ms=end_ms,min_rr=settings.min_rr) for r in records]
    return dict(ticker=contract.contract_name,records=records,valid_points=valid_points,
                expected_points=(end_ms-start_ms)//STEP,excluded=dict(excluded))


def metrics(records):
    resolved = sorted((r for r in records if r["status"] in {"TP","SL"}),key=lambda r:(r["outcome_ms"],r["key"]))
    rs = [r["final_net_r"] for r in resolved]
    win, loss = sum(max(0,r) for r in rs), -sum(min(0,r) for r in rs)
    streak = longest = 0
    for r in rs:
        streak = streak+1 if r < 0 else 0
        longest = max(streak,longest)
    return dict(candidates=len(records),filled=sum(r["filled_ms"] is not None for r in records),
                resolved=len(rs),tp=sum(r["status"]=="TP" for r in resolved),sl=sum(r["status"]=="SL" for r in resolved),
                statuses=dict(Counter(r["status"] for r in records)),
                win_rate=100*sum(r>0 for r in rs)/len(rs) if rs else None,
                avg_net_r=mean(rs) if rs else None,expectancy_net_r=mean(rs) if rs else None,
                profit_factor=win/loss if loss else "INF" if win else None,
                avg_mfe_r=mean(r["mfe_r"] for r in resolved) if resolved else None,
                avg_mae_r=mean(r["mae_r"] for r in resolved) if resolved else None,
                max_consecutive_losses=longest if rs else None,
                sample_status="SUFFICIENT SAMPLE" if len(rs)>=20 else "INSUFFICIENT SAMPLE")


def floor_size(quantity, step):
    return float((Decimal(str(quantity))/Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN)*Decimal(str(step)))


def portfolio(records):
    """Known-event cash ledger. Reserve before filling; no future sizing input.

    Uncertain outcomes freeze reservations and suppress ROI/equity claims.
    Only known resolved cash drawdown is measurable without mark-price series.
    """
    cash = peak = 10000.0
    drawdown, realized = 0.0, 0.0
    active, admitted, excluded, unresolved = {}, {}, Counter(), set()
    events = []
    for r in records:
        events.append((r["created_ms"]-1,2,r["key"],"CREATE",r))
        if r["filled_ms"] is not None:
            events.append((r["filled_ms"]+STEP,0,r["key"],"FILL",r))
        if r["outcome_ms"] is not None:
            events.append((r["outcome_ms"],1,r["key"],"END",r))
    day, day_cash = None, cash
    for stamp,_,key,kind,r in sorted(events):
        current_day = datetime.fromtimestamp(stamp/1000,ZoneInfo("Asia/Tokyo")).date()
        if current_day != day:
            day, day_cash = current_day,cash
        if kind == "CREATE":
            if len(active)>=3 or any(o["ticker"]==r["ticker"] for o in active.values()):
                excluded["CAPACITY_OR_TICKER"] += 1
                continue
            if cash <= day_cash*.97:
                excluded["DAILY_LOSS_LIMIT"] += 1
                continue
            levels = cost_levels(r["trigger"],r["stop"],r["target"],r["side"])
            step, minimum = r.get("step_size"),r.get("min_order_size")
            if not step or step<=0 or not minimum or minimum<=0 or levels["net_risk"]<=0:
                excluded["UNKNOWN_SIZE_RULES"] += 1
                continue
            reserved_risk = sum(o["risk"] for o in active.values())
            reserved_notional = sum(o["notional"] for o in active.values())
            budget = max(0,min(cash*.01,cash*.03-reserved_risk))
            notional = max(0,cash-reserved_notional)
            quantity = floor_size(min(budget/levels["net_risk"],notional/levels["entry"],r.get("max_order_size") or math.inf),step)
            if quantity < minimum or quantity<=0:
                excluded["SIZE_OR_RISK_LIMIT"] += 1
                continue
            order=dict(ticker=r["ticker"],quantity=quantity,risk=quantity*levels["net_risk"],
                       notional=quantity*levels["entry"],filled=False,status="PENDING")
            active[key]=order
            admitted[key]=order
        elif key in active:
            o=active[key]
            if kind == "FILL":
                quantity=floor_size(min(o["quantity"],o["risk"]/r["net_risk"],o["notional"]/r["entry"]),r["step_size"])
                if quantity < r["min_order_size"] or quantity<=0:
                    excluded["SIZE_INVALID_AT_FILL"] += 1
                    o["status"]="REJECTED_AT_FILL"
                    active.pop(key)
                    continue
                o.update(quantity=quantity,filled=True,status="OPEN",entry_fee=quantity*r["entry"]*FEE)
                cash-=o["entry_fee"]
            else:
                o["status"]=r["status"]
                if r["status"] in {"TP","SL"} and o["filled"]:
                    pnl=o["quantity"]*r["net_pnl_per_unit"]
                    cash+=pnl+o["entry_fee"]
                    realized+=pnl
                    o["net_pnl_usdc"]=pnl
                    active.pop(key)
                elif r["status"] in {"AMBIGUOUS","DATA_GAP"}:
                    unresolved.add(key)  # Keep risk locked, including uncertain pending.
                else:
                    active.pop(key)
        peak=max(peak,cash)
        drawdown=max(drawdown,100*(peak-cash)/peak)
    rs=[o["net_pnl_usdc"] for o in admitted.values() if "net_pnl_usdc" in o]
    positive,negative=sum(max(v,0) for v in rs),-sum(min(v,0) for v in rs)
    return dict(initial_cash_usdc=10000,known_cash_usdc=cash,realized_net_pnl_usdc=realized,
                admitted=len(admitted),filled=sum(o["filled"] for o in admitted.values()),resolved=len(rs),
                active=len(active),uncertain=len(unresolved),exclusions=dict(excluded),
                realized_roi_pct=100*realized/10000 if not unresolved else None,
                closed_portfolio_roi_pct=100*(cash/10000-1) if not active else None,
                equity_usdc=cash if not active else None,
                max_known_cash_drawdown_pct=drawdown,
                profit_factor=positive/negative if negative else "INF" if positive else None,
                notes=["Realized return is not total ROI when positions remain open.",
                       "Cash drawdown excludes unrealized losses; not equity drawdown.",
                       "Intrabar fills booked at bar close; reservations prevent future-profit sizing."])


def decision(periods):
    ms=[p["metrics"][MODEL] for p in periods]
    if any(m["resolved"]>=20 and (m["avg_net_r"]<=0 or m["profit_factor"]!="INF" and m["profit_factor"]<=1) for m in ms):
        return "KILL"
    if any(m["resolved"]<20 for m in ms):
        return "CONTINUE_INSUFFICIENT_SAMPLE"
    if any(p["opportunities"]["additional_filled_setups_vs_current"]<=0 for p in periods):
        return "PIVOT_NO_INCREMENTAL_FILLS"
    return "CONTINUE_FORWARD_SHADOW_REQUIRED"


def run(source_dir, analyze, settings, *, role):
    protocol=json.loads(PROTOCOL.read_text())
    if role not in {"development","validation","prospective"}:
        raise ValueError("Invalid registered period role")
    source=json.loads((source_dir/"replay-report.json").read_text())
    period = protocol.get(role)
    if role == "prospective":
        origin=protocol["prospective_start_ms"]
        begin=source["start_ms"]
        end=source["end_ms"]
        if begin < origin or (begin-origin) % (7*86_400_000) or not begin < end <= begin+7*86_400_000:
            raise ValueError("Prospective source must use anchored non-overlapping seven-day windows")
        period=dict(start_ms=begin,end_ms=end,role="PROSPECTIVE_PARAMETERS_RETROSPECTIVE_DATA")
    if source.get("dataset")!="RETROSPECTIVE" or source.get("eligible_for_live_promotion") is not False or (source["start_ms"],source["end_ms"])!=(period["start_ms"],period["end_ms"]):
        raise ValueError("Source differs from registered retrospective period")
    actual_fingerprint=rule_fingerprint(analyze,settings)
    archived_fingerprint=source["manifest"]["rule_fingerprint"]
    # v22 added price-band diagnostics to the analyzer, changing source text.
    # Only the known archived version is accepted, with exact baseline entry
    # reproduction below; a changed strategy or arbitrary fingerprint fails.
    known_archived="5c9e20d6b1deeba5ed2a77daef934e1d4ced10c6162bed1aaabe02eadb95ee2d"
    if (archived_fingerprint not in {actual_fingerprint,known_archived}
            or strategy_parameters(settings)!=source["manifest"]["parameters"]
            or strategy_parameters(settings)!=protocol["production_parameters"]):
        raise ValueError("Unrecognized production fingerprint or changed parameters")
    if any(protocol["rules"][k]!=v for k,v in {"min_net_rr":MIN_NET_RR,"pending_bars":WAIT_BARS,"fee_bps_each_side":FEE*10000,"slippage_bps_each_side":SLIP*10000}.items()):
        raise ValueError("Implementation differs from registered parameters")
    records,markets=[],[]
    allowed={f.name for f in fields(scanner.Contract)}
    for item in source["manifest"]["sources"]:
        path=(source_dir/item["file"]).resolve()
        if not path.is_relative_to(source_dir.resolve()):
            raise ValueError("Candle path outside source")
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item["sha256"]:
            raise ValueError("Candle checksum mismatch")
        data=json.loads(raw)
        contract=scanner.Contract(**{k:v for k,v in data["contract"].items() if k in allowed})
        result=replay_market(contract,[scanner.Candle(**c) for c in data["HOUR_4"]],
                             [scanner.Candle(**c) for c in data["MINUTE_15"]],analyze,settings,
                             start_ms=source["start_ms"],end_ms=source["end_ms"])
        records.extend(result.pop("records"));markets.append(result)
    groups={m:[r for r in records if r["model"]==m] for m in MODELS}
    for model,archived_model in zip(MODELS[:2],("current","shadow")):
        reproduced={(r["setup_id"],r["created_ms"],r["trigger"],r["stop"],r["target"]) for r in groups[model]}
        archived={(s["setup_id"],s["created_ms"],s["entry"],s["stop"],s["target"]) for s in source["signals"][archived_model]}
        if reproduced!=archived or len(reproduced)!=len(groups[model]):
            raise ValueError("Archived baseline first entries did not reproduce")
    current_ids={r["setup_id"] for r in groups[MODELS[0]] if r["filled_ms"] is not None}
    variant_ids={r["setup_id"] for r in groups[MODEL] if r["filled_ms"] is not None}
    return dict(protocol=protocol["protocol"],dataset="RETROSPECTIVE",role=period["role"],
                eligible_for_live_promotion=False,automatic_promotion=False,real_orders_enabled=False,
                start_ms=source["start_ms"],end_ms=source["end_ms"],
                period_complete=role!="prospective" or source["end_ms"]-source["start_ms"]==7*86_400_000,
                production_rule_fingerprint=actual_fingerprint,source_rule_fingerprint=archived_fingerprint,
                baseline_first_entries_reproduced=True,
                protocol_sha256=hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
                engine_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                source_files_verified=len(markets),coverage=source["coverage"],
                metrics={m:metrics(rs) for m,rs in groups.items()},portfolios={m:portfolio(rs) for m,rs in groups.items()},
                opportunities=dict(additional_filled_setups_vs_current=len(variant_ids-current_ids),
                                   shared_filled_setups=len(variant_ids&current_ids)),
                markets=markets,records=records,limitations=protocol["limitations"])


def main():
    import argparse
    from analysis_terminal import server
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--role",choices=("development","validation","prospective"),required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    report=run(args.source,server.analyze_contract,server.SETTINGS,role=args.role)
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/"pending-entry-report.json").write_text(json.dumps(report,ensure_ascii=False,allow_nan=False))
    summary={k:v for k,v in report.items() if k not in {"records","markets"}}
    (args.output/"pending-entry-summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
    print(json.dumps(dict(role=report["role"],metrics=report["metrics"],portfolios=report["portfolios"],opportunities=report["opportunities"])))


if __name__=="__main__":
    main()
