"""Read-only hypothetical economics. Price R is not portfolio ROI.

Use the existing BTC linear cost assumption: 5bps fee + 2bps slippage on
each side. No funding, spread/queue simulation or execution claims.
"""
import math
from collections import Counter
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


def completion(signals, plans, accepted):
    """Account for censoring; scenarios are assumptions, never new outcomes.

    Only OPEN/TP1 with verified coverage and valid frozen prices can enter a
    scenario. Ambiguous, erroneous or missing histories block a whole-cohort
    estimate instead of being guessed as future winners or losers.
    """
    statuses=Counter(str((s.get("result") or {}).get("status") or "OPEN") for s in signals)
    open_plans=[p for s,p in zip(signals,plans)
                if (s.get("result") or {}).get("status") in {"OPEN","TP1"}
                and verified_result(s.get("result")) and p is not None
                and (s.get("result") or {}).get("quality") not in {"HISTORY_GAP","DATA_ERROR"}]
    total,resolved=len(signals),len(accepted)
    remaining=total-resolved
    blocked=remaining-len(open_plans)
    state="EMPTY" if not total else "ALL_RESOLVED" if not remaining else "PARTIALLY_RESOLVED" if resolved else "NO_VERIFIED_RESOLUTIONS"
    sensitivity=dict(status="EMPTY" if not total else "BLOCKED_DATA_QUALITY" if blocked else
                     "NOT_NEEDED_ALL_RESOLVED" if not remaining else "HYPOTHETICAL_OPEN_OUTCOMES",
                     available=bool(open_plans) and blocked==0,
                     assumes_no_new_entries=True,real_execution_results=False,
                     portfolio_roi_pct=None,scenarios=None,
                     limitations=["Scenarios assign every currently open entry its frozen TP or SL; they are not forecasts, probabilities or confidence bounds.",
                                  "Full-position TP/SL assumption; funding, actual fills, gap losses and partial position sizing are unmodeled.",
                                  "Exclude AMBIGUOUS and incomplete/error histories; never rewrite recorded outcomes."])
    if sensitivity["available"]:
        values=[r for _,_,r in accepted]
        scenarios={}
        for name,field in (("all_open_tp","projected_tp_net_r"),("all_open_sl","projected_sl_net_r")):
            rs=values+[p[field] for p in open_plans]
            profit,loss=sum(max(r,0) for r in rs),-sum(min(r,0) for r in rs)
            pf=profit/loss if loss else "INF" if profit else None
            scenarios[name]=dict(signals=len(rs),sum_net_r=round(sum(rs),4),avg_net_r=round(mean(rs),4),
                                 net_win_rate=round(100*sum(r>0 for r in rs)/len(rs),2),
                                 profit_factor=round(pf,4) if isinstance(pf,float) else pf)
        sensitivity["scenarios"]=scenarios
    return dict(status=state,estimate_scope="VERIFIED_RESOLVED_SIGNALS_ONLY",total_signals=total,
                verified_resolved=resolved,resolved_pct=round(100*resolved/total,2) if total else None,
                unresolved_or_excluded=remaining,open_signals=statuses["OPEN"]+statuses["TP1"],
                modeled_open_signals=len(open_plans),blocked_signals=blocked,
                status_counts=dict(statuses),cohort_complete=bool(total) and remaining==0,
                observed_metrics_are_provisional=bool(remaining),sensitivity=sensitivity)


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
                completion=completion(signals,plans,accepted),
                sample_status="SUFFICIENT SAMPLE" if len(rs)>=20 else "INSUFFICIENT SAMPLE",
                real_execution_results=False,eligible_for_live_promotion=False,automatic_promotion=False,
                portfolio_roi_pct=None,
                limitations=["Hypothetical signal-close entry and TP/SL-touch exits; not actual fills.",
                             "Linear fee/slippage assumption; funding, spread, liquidity and gaps beyond the recorded exit are unmodeled.",
                             "R uses the original price stop distance; not the net-stop-risk unit in the pullback portfolio study.",
                             "Signal R cannot determine capital return, portfolio ROI or an equity curve."])
