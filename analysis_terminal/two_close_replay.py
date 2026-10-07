"""Preregistered, independent two-close Shadow experiment; no live hooks/orders.

Uses the frozen evaluator as a pure function, without changing that model's
records, prices, outcomes, decision or collection state.
"""
from bisect import bisect_right
from collections import Counter
from dataclasses import fields
import hashlib
import json
import math
from pathlib import Path

import app as scanner
from analysis_terminal import confirmation_zone_replay as zone
from analysis_terminal import pending_entry_replay as control
from analysis_terminal.replay import complete_window, strategy_parameters
from analysis_terminal.setups import setup_identity

MODEL = 'two_close_measured_net2r'
PROTOCOL = Path(__file__).with_name('two_close_protocol.json')
STEP, WAIT, MIN_RR = zone.STEP, 4, 2.0
GATES = ('confirmation_color_ok','confirmation_level_ok','retest_touched','stop_valid')


def candidate(first, second, second_candle):
    """One chance on the adjacent close; freeze first-seed structural prices."""
    identity = setup_identity(first)
    if not identity or first.get('shadow_v2_ready') is not True or any(first.get(k) is not True for k in GATES):
        raise ValueError('Invalid first qualified seed')
    side = first['direction']
    if (side not in {'LONG','SHORT'} or second_candle.time_ms != first['latest_15m_time_ms']+STEP
            or second.get('latest_15m_time_ms') != second_candle.time_ms
            or second_candle.time_ms % STEP):
        raise ValueError('Second confirmation must be the adjacent closed candle')
    if setup_identity(second) != identity or second.get('direction') != side:
        return None, 'SETUP_CHANGED'
    if second.get('entry_reference') != second_candle.close:
        raise ValueError('Second-close reference mismatch')
    roll, stop, extension = (first.get(k) for k in ('breakout_level','shadow_stop_loss','shadow_v2_extension_target'))
    if not all(zone.positive(v) for v in (roll,stop,extension)):
        raise ValueError('Invalid frozen first-seed structure')
    if (not all(zone.positive(v) for v in (second_candle.open,second_candle.high,second_candle.low,second_candle.close))
            or not second_candle.low <= min(second_candle.open,second_candle.close)
                <= max(second_candle.open,second_candle.close) <= second_candle.high):
        raise ValueError('Invalid second confirmation OHLC')
    long = side == 'LONG'
    if any(second.get(k) is not True for k in GATES) or not (
            second_candle.close > second_candle.open and second_candle.close > roll if long else
            second_candle.close < second_candle.open and second_candle.close < roll):
        return None, 'SECOND_CONFIRMATION_LOST'
    if second_candle.low <= stop if long else second_candle.high >= stop:
        return None, 'ORIGINAL_STOP_TOUCHED_BEFORE_CONFIRMATION'
    reference = second_candle.close
    target = zone.net_target(reference,stop,side)
    if target is None or not (target<=extension if long else target>=extension):
        if target is None or not math.isclose(target,extension,rel_tol=1e-12):
            return None, 'SECOND_CLOSE_NET_ROOM_INELIGIBLE'
    close_ms = second_candle.time_ms+STEP
    return dict(key=MODEL+':'+identity,model=MODEL,setup_id=identity,ticker=first['ticker'],side=side,
                first_signal_candle_ms=first['latest_15m_time_ms'],first_created_ms=first['latest_15m_time_ms']+STEP+1,
                signal_candle_ms=second_candle.time_ms,created_ms=close_ms+1,expires_ms=close_ms+WAIT*STEP,
                trigger=reference,roll_level=roll,stop=stop,target=target,extension_target=extension,
                status='PENDING',filled_ms=None,outcome_ms=None,final_net_r=None,mfe_r=0.0,mae_r=0.0), None


def evaluate(record, candles, state_at, *, end_ms):
    if (record['model'] != MODEL or record['key'] != MODEL+':'+record['setup_id']
            or record['signal_candle_ms'] != record['first_signal_candle_ms']+STEP
            or record['created_ms'] != record['first_created_ms']+STEP):
        raise ValueError('Invalid two-close identity or exclusion clock')
    # Model naming is adapted only on a private copy to reuse identical execution
    # economics and chronology. The original evaluator and ledgers stay frozen.
    out = zone.evaluate(dict(record,model=zone.MODEL),candles,state_at,end_ms=end_ms)
    out['model'] = MODEL
    return out


def decision(reports):
    metrics = [r['metrics'][MODEL] for r in reports]
    if any(m['resolved']>=20 and (m['avg_net_r']<=0 or m['profit_factor']!='INF' and m['profit_factor']<=1)
           for m in metrics):
        return 'KILL_NO_LIVE_PROMOTION'
    if any(m['resolved']<20 for m in metrics):
        return 'CONTINUE_INSUFFICIENT_SAMPLE'
    if any(r['comparison']['net_filled_count_difference']<=0 for r in reports):
        return 'PIVOT_NO_INCREMENTAL_FILLS'
    return 'FORWARD_SHADOW_REQUIRED'


def build(source_dir, analyze, settings, *, role, comparator_path, baseline_path=None):
    protocol_bytes = PROTOCOL.read_bytes()
    p = json.loads(protocol_bytes)
    if (p['model']!=MODEL or p['rules']['confirmation_bars']!=2 or p['rules']['min_net_rr']!=MIN_RR
            or p['rules']['pending_bars']!=WAIT or p['rules']['fee_bps_each_side']!=zone.FEE*10000
            or p['rules']['slippage_bps_each_side']!=zone.SLIP*10000
            or p['production_parameters']!=strategy_parameters(settings)):
        raise ValueError('Implementation differs from registered rules')
    for name, digest in p['frozen_dependencies_sha256'].items():
        if hashlib.sha256(PROTOCOL.with_name(name).read_bytes()).hexdigest()!=digest:
            raise ValueError('Frozen evaluator or control changed')
    original = comparator_path.read_bytes()
    if role in p['comparator_reports_sha256'] and hashlib.sha256(original).hexdigest()!=p['comparator_reports_sha256'][role]:
        raise ValueError('Unexpected frozen comparator')
    archived = json.loads(original)
    if role in ('development','validation'):
        if (archived['start_ms'],archived['end_ms'])!=(p[role]['start_ms'],p[role]['end_ms']):
            raise ValueError('Unregistered period')
    elif role=='prospective':
        start,end = archived['start_ms'],archived['end_ms']
        if start<p['prospective_start_ms'] or (start-p['prospective_start_ms'])%(7*86400000) or not start<end<=start+7*86400000 or end%86400000:
            raise ValueError('Unregistered forward window')
    else:
        raise ValueError('Invalid study role')
    # Exact whole-report equality verifies every original candidate, execution,
    # result, metric and capped portfolio, using the original SHA-checked sources.
    reproduced = zone.build(source_dir,analyze,settings,role=role,baseline_path=baseline_path)
    if reproduced!=archived or comparator_path.read_bytes()!=original:
        raise ValueError('Frozen comparator did not reproduce exactly')
    source = json.loads((source_dir/'replay-report.json').read_text())
    start,end = source['start_ms'],source['end_ms']
    seeds = source['signals']['shadow']
    records,exclusions = [],Counter()
    allowed = {f.name for f in fields(scanner.Contract)}
    for item in source['manifest']['sources']:
        selected = [s for s in seeds if s['ticker']==item['ticker']]
        if not selected:
            continue
        raw = (source_dir/item['file']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:
            raise ValueError('Candle source changed after comparator replay')
        data = json.loads(raw)
        contract = scanner.Contract(**{k:v for k,v in data['contract'].items() if k in allowed})
        frames = {i:[scanner.Candle(**c) for c in data[i]] for i in ('HOUR_4','MINUTE_15')}
        times = {i:[c.time_ms+scanner.INTERVAL_MS[i] for c in cs] for i,cs in frames.items()}
        bars = {c.time_ms:c for c in frames['MINUTE_15']}
        cache = {}
        def state_at(stamp):
            if stamp not in cache:
                windows = {i:complete_window(cs,bisect_right(times[i],stamp+1),180,scanner.INTERVAL_MS[i],stamp+1)
                           for i,cs in frames.items()}
                if any(w is None for w in windows.values()):
                    cache[stamp] = None
                else:
                    row = analyze(contract,windows['HOUR_4'],windows['MINUTE_15'],as_of_ms=stamp+1)
                    cache[stamp] = dict(row,setup_id=setup_identity(row))
            return cache[stamp]
        for seed in selected:
            stamp = seed['created_ms']-1
            first = state_at(stamp)
            if (not first or setup_identity(first)!=seed['setup_id'] or first['latest_15m_time_ms']!=seed['signal_candle_ms']
                    or any(first[current]!=seed[saved] for current,saved in
                           [('entry_reference','entry'),('shadow_stop_loss','stop'),('shadow_v2_extension_target','extension_target')])):
                raise ValueError('First qualified seed mismatch')
            second_close = stamp+STEP
            if second_close>=end:
                exclusions['SECOND_CONFIRMATION_OUTSIDE_PERIOD'] += 1
                continue
            second = state_at(second_close)
            c = bars.get(stamp)
            if second is None or c is None:
                exclusions['SECOND_CONFIRMATION_HISTORY_UNAVAILABLE'] += 1
                continue
            r,reason = candidate(first,second,c)
            if r is None:
                exclusions[reason] += 1
                continue
            r.update(step_size=contract.step_size,min_order_size=contract.min_order_size,max_order_size=contract.max_order_size)
            records.append(evaluate(r,frames['MINUTE_15'],state_at,end_ms=end))
    if len(records)+sum(exclusions.values())!=len(seeds):
        raise ValueError('Lost or duplicated first-seed opportunity')
    all_records = archived['records']+records
    models = (*archived['metrics'],MODEL)
    metrics = {m:control.metrics([r for r in all_records if r['model']==m]) for m in models}
    portfolios = {m:control.portfolio([r for r in all_records if r['model']==m]) for m in models}
    if any(metrics[m]!=archived['metrics'][m] or portfolios[m]!=archived['portfolios'][m] for m in archived['metrics']):
        raise ValueError('Original comparator economics changed')
    current = {r['setup_id'] for r in archived['records'] if r['model']=='current_next_open' and r['filled_ms'] is not None}
    prior = {r['setup_id'] for r in archived['records'] if r['model']==zone.MODEL and r['filled_ms'] is not None}
    filled = {r['setup_id'] for r in records if r['filled_ms'] is not None}
    return dict(protocol=p['protocol'],dataset='ISOLATED_REGISTERED_TWO_CLOSE_REPLAY',role=role,start_ms=start,end_ms=end,
                period_complete=role!='prospective' or end-start==7*86400000,
                protocol_sha256=hashlib.sha256(protocol_bytes).hexdigest(),engine_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                comparator_sha256=hashlib.sha256(original).hexdigest(),source_files_verified=reproduced['source_files_verified'],
                first_shadow_seeds=len(seeds),seed_exclusions=dict(exclusions),metrics=metrics,portfolios=portfolios,records=all_records,
                comparison=dict(shared_current_setups=len(current&filled),proposal_only_current_setups=len(filled-current),
                    current_filled_missing_in_proposal=len(current-filled),net_filled_count_difference=len(filled)-len(current),
                    shared_first_confirmation_setups=len(prior&filled),first_confirmation_missing_in_proposal=len(prior-filled),
                    proposal_only_first_confirmation_setups=len(filled-prior),net_filled_difference_vs_first_confirmation=len(filled)-len(prior),
                    capped_filled_count_difference=portfolios[MODEL]['filled']-portfolios['current_next_open']['filled']),
                changes_live_rules=False,real_orders_enabled=False,automatic_promotion=False,eligible_for_live_promotion=False,limitations=p['limitations'])


def main():
    import argparse
    from analysis_terminal import server
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source','comparator','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--baseline',type=Path)
    parser.add_argument('--role',choices=('development','validation','prospective'),required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.source.resolve()) or args.output.resolve()==args.comparator.resolve() or (
            args.baseline and args.output.resolve()==args.baseline.resolve()):
        raise ValueError('Do not overwrite frozen inputs')
    result = build(args.source,server.analyze_contract,server.SETTINGS,role=args.role,
                   comparator_path=args.comparator,baseline_path=args.baseline)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'metrics':result['metrics'][MODEL],'comparison':result['comparison'],'seed_exclusions':result['seed_exclusions']}))


if __name__=='__main__':
    main()
