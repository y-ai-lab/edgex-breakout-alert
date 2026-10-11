"""Separate preregistered allocation experiment. Never called by production.

The copied event ledger is checked against the original on every input. Only
CREATE notional differs for the experimental arm; no outcome/fill changes.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from analysis_terminal import pending_entry_replay as engine
from analysis_terminal import pending_followup as follow
from analysis_terminal import execution_funnel as funnel
from analysis_terminal.vwap_reclaim_replay import stressed_metrics

STEP, FEE = engine.STEP, engine.FEE
floor_size, cost_levels = engine.floor_size, engine.cost_levels
PLAN = ROOT / '.github/research/equal_reservation_protocol.json'
PLAN_SHA = '06a3a41d6d90a2203a592d2283241ac6ae95e036eb183b143595a5ed92ca85f0'
REGISTRATION_COMMIT = 'c789b95c9b3c03e86cf71841b9b05937e2253990'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_plan():
    if sha(PLAN) != PLAN_SHA:
        raise ValueError('Registered allocation protocol changed')
    plan = json.loads(PLAN.read_text())
    for path, expected in plan['preserve_sha256'].items():
        if sha(ROOT/path) != expected:
            raise ValueError('Frozen allocation dependency changed')
    return plan


def _portfolio(records, *, equal_ceiling):
    """Known-event cash ledger. Reserve before filling; no future sizing input.

    Uncertain outcomes freeze reservations and suppress ROI/equity claims.
    Only known resolved cash drawdown is measurable without mark-price series.
    """
    cash = peak = 10000.0
    drawdown, realized = 0.0, 0.0
    active, admitted, excluded, unresolved = {}, {}, Counter(), set()
    events, creations = [], {}
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
            notional = max(0,min(cash/3,cash-reserved_notional)) if equal_ceiling else max(0,cash-reserved_notional)
            quantity = floor_size(min(budget/levels["net_risk"],notional/levels["entry"],r.get("max_order_size") or math.inf),step)
            if quantity < minimum or quantity<=0:
                excluded["SIZE_OR_RISK_LIMIT"] += 1
                continue
            order=dict(ticker=r["ticker"],quantity=quantity,risk=quantity*levels["net_risk"],
                       notional=quantity*levels["entry"],filled=False,status="PENDING")
            creations[key]=dict(stamp_ms=stamp,known_cash=cash,quantity=quantity,
                                risk_budget=budget,notional_budget=notional,
                                reserved_risk=reserved_risk,reserved_notional=reserved_notional)
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
    summary = dict(initial_cash_usdc=10000,known_cash_usdc=cash,realized_net_pnl_usdc=realized,
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
    return summary, admitted, creations


def compare(records, expected):
    """Preserve original ledger; both models use identical allocation rules."""
    if len({r['key'] for r in records}) != len(records):
        raise ValueError('Duplicate setup key')
    before = deepcopy(records)
    original, _, _ = _portfolio(records, equal_ceiling=False)
    if original != expected or original != engine.portfolio(records):
        raise ValueError('Original portfolio does not reproduce exactly')
    proposal, admitted, creations = _portfolio(records, equal_ceiling=True)
    selected = []
    for r in records:
        if r['key'] not in admitted:
            continue
        o = admitted[r['key']]
        row = dict(r, status=o['status'],
                   filled_ms=r['filled_ms'] if o['filled'] else None)
        if o['status'] not in {'TP', 'SL'}:
            row['final_net_r'] = None
        selected.append(row)
    done = [r for r in selected if r['status'] in {'TP', 'SL'}]
    stressed_cash = 0.0
    for r in done:
        d = 1 if r['side'] == 'LONG' else -1
        entry = r['nominal_fill']*(1+d*.0005)
        exit_price = r['exit_price']/(1-d*engine.SLIP)*(1-d*.0005)
        pnl = d*(exit_price-entry)-.001*(entry+exit_price)
        stressed_cash += admitted[r['key']]['quantity']*pnl
    old_fills = {k for k, o in _portfolio(records, equal_ceiling=False)[1].items()
                 if o['filled']}
    new_fills = {k for k, o in admitted.items() if o['filled']}
    shared, added, missing = old_fills & new_fills, new_fills-old_fills, old_fills-new_fills
    if records != before:
        raise ValueError('Allocation mutated source ledger')
    result = dict(original_portfolio=original, equal_ceiling_portfolio=proposal,
                  funded_metrics=engine.metrics(selected),
                  funded_stress_metrics=stressed_metrics(selected),
                  uncapped_metrics_unchanged=engine.metrics(records),
                  allocation_fill_comparison=dict(shared=len(shared), new_only=len(added),
                                                  original_missing=len(missing),
                                                  net_fill_difference=len(new_fills)-len(old_fills)),
                  fixed_quantity_stress_realized_pnl_usdc=stressed_cash,
                  stress_final_portfolio_roi_pct=None,
                  funding_inclusive_avg_r=None, funding_inclusive_pf=None,
                  original_ledger_unchanged=True)
    return result, admitted, creations


def review(source_dir, ledger_path, *, role, now_ms):
    plan = validate_plan()
    periods = {p['id']: p for p in plan['future_periods']}
    periods['development'] = plan['development']
    if role not in periods or type(now_ms) is not int:
        raise ValueError('Unknown registered role or invalid clock')
    period = periods[role]
    # Check time and fixed period BEFORE opening market files; never peek ahead.
    if now_ms < period['end_ms']:
        raise ValueError('Registered period has not ended')
    raw_ledger = ledger_path.read_bytes()
    report = json.loads(raw_ledger)
    source_path = source_dir/'replay-report.json'
    source = json.loads(source_path.read_bytes())
    if (report['start_ms'], report['end_ms']) != (period['start_ms'], period['end_ms']):
        raise ValueError('Unregistered market window')
    if role == 'development' and (sha(ledger_path) != period['ledger_sha256']
                                  or sha(source_path) != period['source_sha256']):
        raise ValueError('Viewed development archive changed')
    if (report.get('dataset') != 'RETROSPECTIVE'
            or any(report.get(f) is not False for f in
                   ('real_orders_enabled', 'automatic_promotion', 'eligible_for_live_promotion'))):
        raise ValueError('Only public isolated research supported')
    follow.validate_base(report, source)
    # Read-only candle hash/grid checks and original control metrics/portfolios.
    for item in source['manifest']['sources']:
        path = (source_dir/item['file']).resolve()
        if not path.is_relative_to(source_dir.resolve()) or sha(path) != item['sha256']:
            raise ValueError('Public source archive hash/path mismatch')
    funnel.summarize(report)
    groups = {m: [r for r in report['records'] if r['model'] == m] for m in engine.MODELS}
    models, orders, evidence = {}, {}, {}
    for model, rows in groups.items():
        for r in rows:
            if (type(r['created_ms']) is not int
                    or r['created_ms'] != r['signal_candle_ms']+STEP+1
                    or not report['start_ms'] <= r['created_ms']-1 < report['end_ms']):
                raise ValueError('Invalid original signal sentinel')
        models[model], orders[model], evidence[model] = compare(rows, report['portfolios'][model])
        if models[model]['uncapped_metrics_unchanged'] != report['metrics'][model]:
            raise ValueError('Original metrics do not reproduce')
    current, proposal = 'current_next_open', engine.MODEL
    shared = {r['setup_id'] for r in groups[current] if orders[current].get(r['key'], {}).get('filled')}
    proposed = {r['setup_id'] for r in groups[proposal] if orders[proposal].get(r['key'], {}).get('filled')}
    comparison = dict(shared_filled_setups=len(shared & proposed),
                      proposal_only_filled_setups=len(proposed-shared),
                      current_filled_missing_in_proposal=len(shared-proposed),
                      net_filled_count_difference=len(proposed)-len(shared))
    m = models[proposal]['funded_metrics']
    negative = m['resolved'] >= 20 and (m['avg_net_r'] <= 0 or m['profit_factor'] != 'INF' and m['profit_factor'] <= 1)
    blocked = any(r['status'] in {'AMBIGUOUS', 'DATA_GAP', 'INVALIDATED_GAP'}
                  for r in report['records']) or bool(source['manifest']['failures'])
    decision = ('KILL_ALLOCATION_NO_PROMOTION' if period.get('period_complete', True) and negative
                else 'DEVELOPMENT_ONLY_NO_SELECTION' if role == 'development'
                else 'BLOCKED_DATA_QUALITY' if blocked
                else 'INSUFFICIENT_SAMPLE' if m['resolved'] < 50
                else 'BLOCKED_UNVERIFIED_FUNDING_AND_EXECUTION')
    if sha(ledger_path) != hashlib.sha256(raw_ledger).hexdigest():
        raise ValueError('Original ledger changed during review')
    result = dict(dataset='PUBLIC_RESEARCH_ALLOCATION_EXPERIMENT', protocol=plan['protocol'],
                  registration_commit=REGISTRATION_COMMIT, protocol_sha256=PLAN_SHA,
                  role=role, start_ms=report['start_ms'], end_ms=report['end_ms'],
                  entry_period_complete=period.get('period_complete', True),
                  previously_viewed=period['market_already_viewed'],
                  source_sha256=sha(source_path), ledger_sha256=sha(ledger_path),
                  models=models, capped_proposal_vs_same_allocator_current=comparison,
                  decision=decision, data_quality_status='BLOCKED_DATA_QUALITY' if blocked else 'NO_BLOCKED_RECORDS',
                  original_entries_outcomes_metrics_portfolios_preserved=True,
                  future_periods_unopened=True, actual_execution_evidence=False,
                  eligible_for_live_promotion=False, real_orders_enabled=False,
                  automatic_promotion=False, changes_live_rules=False,
                  limitations=plan['limitations']+[
                      'Funding-inclusive profitability unavailable, not zero Funding cost.',
                      'Fixed-quantity stress is a cost diagnostic, not a stress portfolio replay.',
                      'Capped selected-subset trade statistics are not changed uncapped signal statistics.'])
    return result, dict(admitted=orders, creation_evidence=evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--role', choices=('development', 'future_validation_1', 'future_validation_2'), required=True)
    parser.add_argument('--previous-review', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Never overwrite an allocation result')
    # Sequential holdout review requires the exact first sealed prior result.
    if args.role == 'future_validation_2':
        if not args.previous_review:
            raise ValueError('Missing first holdout review')
        previous = json.loads(args.previous_review.read_bytes())
        p = validate_plan()['future_periods'][0]
        if (previous.get('protocol_sha256') != PLAN_SHA
                or previous.get('role') != 'future_validation_1'
                or (previous.get('start_ms'), previous.get('end_ms')) != (p['start_ms'], p['end_ms'])
                or previous.get('decision') in {'KILL_ALLOCATION_NO_PROMOTION', 'BLOCKED_DATA_QUALITY'}):
            raise ValueError('Prior holdout missing, blocked, mismatched or KILL')
    result, _ = review(args.source, args.ledger, role=args.role, now_ms=int(time.time()*1000))
    with args.output.open('x') as out:
        json.dump(result, out, indent=2, ensure_ascii=False)
        out.write('\n')
    print(json.dumps(dict(decision=result['decision'], changes_live_rules=False)))


if __name__ == '__main__':
    main()
