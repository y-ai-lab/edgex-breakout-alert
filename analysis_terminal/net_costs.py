"""Read-only hypothetical economics. Price R is not portfolio ROI.

Use the existing BTC linear cost assumption: 5bps fee + 2bps slippage on
each side. No funding, spread/queue simulation or execution claims.
"""
import math
from statistics import mean

from analysis_terminal.outcomes import verified_result

FEE_BPS, SLIPPAGE_BPS = 5.0, 2.0
COST = (FEE_BPS+SLIPPAGE_BPS)/10000
MODEL = "LINEAR_5BPS_FEE_2BPS_SLIPPAGE_EACH_SIDE"


def number(value):
    if value is None or isinstance(value,bool):
        return None
    try:
        value=float(value)
    except (ValueError,TypeError,OverflowError):
        return None
    return value if math.isfinite(value) else None


def projection(signal):
    """Frozen signal prices only; never changes levels or eligibility."""
    side=signal.get("side")
    entry,stop,target=(number(signal.get(k)) for k in ("entry","stop","target"))
    if side not in {"LONG","SHORT"} or any(v is None or v<=0 for v in (entry,stop,target)):
        return None
    if not (stop < entry < target if side=="LONG" else target < entry < stop):
        return None
    direction=1 if side=="LONG" else -1
    risk=abs(entry-stop)
    gross_tp=direction*(target-entry)/risk
    cost_tp=(entry*COST+target*COST)/risk
    cost_sl=(entry*COST+stop*COST)/risk
    tp_net,sl_net=gross_tp-cost_tp,-1-cost_sl
    net_rr=tp_net/-sl_net
    if not all(math.isfinite(v) for v in (risk,gross_tp,cost_tp,cost_sl,tp_net,sl_net,net_rr)):
        return None
    return dict(cost_model=MODEL,r_basis="ORIGINAL_ENTRY_TO_STOP_PRICE_DISTANCE",
                fee_bps_each_side=FEE_BPS,slippage_bps_each_side=SLIPPAGE_BPS,
                gross_target_r=gross_tp,cost_to_tp_r=cost_tp,cost_to_sl_r=cost_sl,
                projected_tp_net_r=tp_net,projected_sl_net_r=sl_net,
                projected_net_rr=net_rr,tp_profitable_after_assumed_costs=tp_net>0,
                real_execution_results=False,changes_live_rules=False)


def metrics(signals):
    """Caller supplies the appropriate first-entry cohort; never dedupe by ticker.

    Require chronological verified outcomes and price/R consistency. Stored R
    is rounded to four decimals; projections use the original frozen prices.
    """
    plans=[projection(s) for s in signals]
    valid_plans=[p for p in plans if p is not None]
    accepted=[]
    excluded=dict(unverified=0,invalid_prices=0,inconsistent_r=0)
    for signal,plan in zip(signals,plans):
        result=signal.get("result") or {}
        if result.get("status") not in {"TP","SL"}:
            continue
        if not verified_result(result):
            excluded["unverified"]+=1
            continue
        if plan is None:
            excluded["invalid_prices"]+=1
            continue
        reported=number(result.get("final_r"))
        expected=plan["gross_target_r"] if result["status"]=="TP" else -1.0
        if reported is None or abs(reported-expected)>5.1e-5:
            excluded["inconsistent_r"]+=1
            continue
        net=plan["projected_tp_net_r"] if result["status"]=="TP" else plan["projected_sl_net_r"]
        accepted.append((signal,plan,net))
    rs=[r for _,_,r in accepted]
    profit,loss=sum(max(r,0) for r in rs),-sum(min(r,0) for r in rs)
    ordered=[(s,p,r) for s,p,r in accepted if (number(s["result"].get("outcome_time_ms")) or 0)>0]
    ordered.sort(key=lambda x:(float(x[0]["result"]["outcome_time_ms"]),float(x[0].get("created_ms") or 0),str(x[0].get("key") or "")))
    longest=streak=0
    for _,_,r in ordered:
        streak=streak+1 if r<0 else 0
        longest=max(longest,streak)
    pf=profit/loss if loss else "INF" if profit else None
    return dict(cost_model=MODEL,r_basis="ORIGINAL_ENTRY_TO_STOP_PRICE_DISTANCE",
                fee_bps_each_side=FEE_BPS,slippage_bps_each_side=SLIPPAGE_BPS,
                signals=len(signals),priced_signals=len(valid_plans),unpriced_signals=len(signals)-len(valid_plans),
                projected_tp_nonpositive=sum(p["projected_tp_net_r"]<=0 for p in valid_plans),
                projected_net_rr_below_2=sum(p["projected_net_rr"]<2 for p in valid_plans),
                resolved=len(rs),tp=sum(s["result"]["status"]=="TP" for s,_,_ in accepted),
                sl=sum(s["result"]["status"]=="SL" for s,_,_ in accepted),
                net_wins=sum(r>0 for r in rs),net_losses=sum(r<0 for r in rs),net_flat=sum(r==0 for r in rs),
                tp_hit_rate=round(100*sum(s["result"]["status"]=="TP" for s,_,_ in accepted)/len(rs),2) if rs else None,
                net_win_rate=round(100*sum(r>0 for r in rs)/len(rs),2) if rs else None,
                avg_net_r=round(mean(rs),4) if rs else None,
                expectancy_net_r=round(mean(rs),4) if rs else None,
                profit_factor=round(pf,4) if isinstance(pf,float) else pf,
                avg_assumed_cost_r=round(mean(p["cost_to_tp_r"] if s["result"]["status"]=="TP" else p["cost_to_sl_r"] for s,p,_ in accepted),4) if accepted else None,
                max_consecutive_net_losses=longest if ordered else None,streak_samples=len(ordered),
                excluded_resolved=excluded,minimum_resolved=20,
                sample_status="SUFFICIENT SAMPLE" if len(rs)>=20 else "INSUFFICIENT SAMPLE",
                real_execution_results=False,eligible_for_live_promotion=False,automatic_promotion=False,
                portfolio_roi_pct=None,
                limitations=["Hypothetical signal-close entry and TP/SL-touch exits; not actual fills.",
                             "Linear fee/slippage assumption; funding, spread, liquidity and gaps beyond the recorded exit are unmodeled.",
                             "R uses the original price stop distance; not the net-stop-risk unit in the pullback portfolio study.",
                             "Signal R cannot determine capital return, portfolio ROI or an equity curve."])
