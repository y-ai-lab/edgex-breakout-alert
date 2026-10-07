"""Describe missed pullback prices on frozen candles; no new entries or outcomes.

The original four-bar expiry stays fixed. Observe four further bars solely to
distinguish absent retracements from late touches, invalidation and censoring.
Late touches are NOT fills and have no invented win rate, R or portfolio ROI.
"""
from bisect import bisect_right
from collections import Counter
from dataclasses import fields
import hashlib
import json
import math
from pathlib import Path
from statistics import median

import app as scanner
from analysis_terminal import execution_funnel, pending_entry_replay as study
from analysis_terminal.replay import complete_window, rule_fingerprint, strategy_parameters
from analysis_terminal.setups import setup_identity

EXTRA_OBSERVATION_BARS = 4
DISTANCE_BINS = (.25, .5, 1)


def distance_bin(value):
    for i, upper in enumerate(DISTANCE_BINS):
        if value <= upper:
            return f'<= {upper:g}R' if i == 0 else f'({DISTANCE_BINS[i-1]:g}, {upper:g}]R'
    return '> 1R'


def diagnose(record, row, candles, state_at, *, end_ms):
    if record['model'] != study.MODEL or record['created_ms'] != record['signal_candle_ms']+study.STEP+1:
        raise ValueError('Expected frozen confirmed-pullback record and close+1ms clock')
    start = record['created_ms']-1
    stop, target, trigger = (record[k] for k in ('stop','target','trigger'))
    side = record['side'];long = side == 'LONG'
    if side not in {'LONG','SHORT'} or not all(math.isfinite(v) and v > 0 for v in (stop,target,trigger)):
        raise ValueError('Invalid frozen price geometry')
    if not (stop < trigger < target if long else target < trigger < stop):
        raise ValueError('Trigger outside structural levels')
    risk = abs(trigger-stop)
    target_behind_signal = row['entry_reference'] >= target if long else row['entry_reference'] <= target
    result = dict(key=record['key'],setup_id=record['setup_id'],ticker=record['ticker'],
                  original_status=record['status'],original_filled_ms=record['filled_ms'],
                  trigger_on_confirmed_side_of_roll=trigger>row['breakout_level'] if long else trigger<row['breakout_level'],
                  target_behind_signal_close=target_behind_signal,
                  signal_to_trigger_r=abs(row['entry_reference']-trigger)/risk,
                  closest_distance_before_expiry_r=None,target_touched_before_expiry=False,
                  later_observation=None,late_touch_open_ms=None,
                  late_touch_after_prior_target=None,late_touch_exit_same_bar=None,
                  latest_observed_close_ms=start)
    if record['status'] != 'EXPIRED':
        return result
    if record['expires_ms'] != start+study.WAIT_BARS*study.STEP:
        raise ValueError('Expiry differs from frozen protocol')
    series={c.time_ms:c for c in candles if c.time_ms+study.STEP<=end_ms}
    distances=[];target_previously_seen=target_behind_signal
    for stamp in range(start,record['expires_ms']+EXTRA_OBSERVATION_BARS*study.STEP,study.STEP):
        later = stamp >= record['expires_ms']
        if stamp+study.STEP > end_ms:
            result['later_observation']='RIGHT_CENSORED';break
        c = series.get(stamp)
        state = state_at(stamp)
        if c is None or state is None:
            if not later:raise ValueError('Frozen expiry is not supported by contiguous known data')
            result['later_observation']='DATA_GAP';break
        if state.get('setup_id') != record['setup_id']:
            if not later:raise ValueError('Expired record should have been invalidated')
            result['later_observation']='SETUP_CHANGED';break
        stop_gap = c.open <= stop if long else c.open >= stop
        if stop_gap:
            if not later:raise ValueError('Expired record should have been gap-invalidated')
            result['later_observation']='OPEN_THROUGH_STOP';break
        touched = c.low <= trigger if long else c.high >= trigger
        target_touch = c.high >= target if long else c.low <= target
        stop_touch = c.low <= stop if long else c.high >= stop
        result['latest_observed_close_ms']=stamp+study.STEP
        if not later:
            if touched:raise ValueError('Expired record contains a price touch before expiry')
            distances.append((c.low-trigger if long else trigger-c.high)/risk)
            result['target_touched_before_expiry'] |= target_touch
        elif touched:
            result.update(later_observation='LATE_PRICE_TOUCH',late_touch_open_ms=stamp,
                          late_touch_after_prior_target=target_previously_seen,
                          late_touch_exit_same_bar=target_touch or stop_touch)
            break
        target_previously_seen |= target_touch
    else:
        result['later_observation']='NO_TOUCH_IN_FIXED_EXTRA_WINDOW'
    result['closest_distance_before_expiry_r']=min(distances) if distances else None
    return result


def summarize(records):
    expired=[r for r in records if r['original_status']=='EXPIRED']
    late=[r for r in expired if r['later_observation']=='LATE_PRICE_TOUCH']
    distances=[r['closest_distance_before_expiry_r'] for r in expired]
    return dict(candidates=len(records),expired=len(expired),
                trigger_on_confirmed_side_of_roll=sum(r['trigger_on_confirmed_side_of_roll'] for r in records),
                trigger_across_roll=sum(not r['trigger_on_confirmed_side_of_roll'] for r in records),
                target_behind_signal_close=sum(r['target_behind_signal_close'] for r in records),
                expired_target_touched_before_expiry=sum(r['target_touched_before_expiry'] for r in expired),
                expired_closest_distance_bins=dict(Counter(distance_bin(v) for v in distances)),
                expired_closest_distance_median_r=median(distances) if distances else None,
                expired_later_observations=dict(Counter(r['later_observation'] for r in expired)),
                late_price_touches=len(late),
                late_touches_after_prior_target=sum(r['late_touch_after_prior_target'] for r in late),
                late_touches_with_exit_same_bar=sum(r['late_touch_exit_same_bar'] for r in late),
                late_touches_without_prior_target_or_same_bar_exit=sum(
                    not r['late_touch_after_prior_target'] and not r['late_touch_exit_same_bar'] for r in late),
                counterfactual_fills=None,counterfactual_win_rate=None,counterfactual_avg_r=None,
                counterfactual_portfolio_roi_pct=None)


def build(source_dir, report_path, analyze, settings):
    original_bytes=report_path.read_bytes();ledger=json.loads(original_bytes)
    execution_funnel.summarize(ledger)  # Validate saved cohort, timestamps, metrics and capital.
    if ledger['dataset'] != 'RETROSPECTIVE':
        raise ValueError('Use original entry cohort, not a second sample from followup')
    source_bytes=(source_dir/'replay-report.json').read_bytes();source=json.loads(source_bytes)
    if (source.get('dataset')!='RETROSPECTIVE' or source.get('eligible_for_live_promotion') is not False
            or (source['start_ms'],source['end_ms'])!=(ledger['start_ms'],ledger['end_ms'])
            or source['manifest']['rule_fingerprint']!=ledger['source_rule_fingerprint']
            or rule_fingerprint(analyze,settings)!=ledger['production_rule_fingerprint']
            or strategy_parameters(settings)!=source['manifest']['parameters']):
        raise ValueError('Original source, time window or production rules mismatch')
    rows=[r for r in ledger['records'] if r['model']==study.MODEL]
    by_ticker={}
    for r in rows:by_ticker.setdefault(r['ticker'],[]).append(r)
    found=set();diagnostics=[];verified=0
    allowed={f.name for f in fields(scanner.Contract)}
    for item in source['manifest']['sources']:
        if item['ticker'] in found:raise ValueError('Duplicate source ticker')
        found.add(item['ticker'])
        path=(source_dir/item['file']).resolve()
        if not path.is_relative_to(source_dir.resolve()):raise ValueError('Source path escapes archive')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Candle checksum mismatch')
        verified+=1
        if item['ticker'] not in by_ticker:continue
        data=json.loads(raw)
        contract=scanner.Contract(**{k:v for k,v in data['contract'].items() if k in allowed})
        if contract.contract_name!=item['ticker']:raise ValueError('Source contract mismatch')
        frames={interval:[scanner.Candle(**c) for c in data[interval]] for interval in ('HOUR_4','MINUTE_15')}
        for interval,cs in frames.items():
            if (any(a.time_ms>=b.time_ms for a,b in zip(cs,cs[1:])) or
                    any(c.contract_id!=contract.contract_id or c.interval!=interval or
                        c.time_ms%scanner.INTERVAL_MS[interval] or
                        not all(math.isfinite(v) and v>0 for v in (c.open,c.high,c.low,c.close)) or
                        not c.low<=min(c.open,c.close)<=max(c.open,c.close)<=c.high for c in cs)):
                raise ValueError('Invalid candle order, identity, grid or OHLC')
        times={i:[c.time_ms+scanner.INTERVAL_MS[i] for c in cs] for i,cs in frames.items()}
        cache={}
        def state_at(stamp):
            if stamp not in cache:
                windows={i:complete_window(cs,bisect_right(times[i],stamp+1),180,
                         scanner.INTERVAL_MS[i],stamp+1) for i,cs in frames.items()}
                if any(w is None for w in windows.values()):cache[stamp]=None
                else:
                    row=analyze(contract,windows['HOUR_4'],windows['MINUTE_15'],as_of_ms=stamp+1)
                    cache[stamp]=dict(row,setup_id=setup_identity(row))
            return cache[stamp]
        for r in by_ticker[item['ticker']]:
            row=state_at(r['created_ms']-1)
            if row is None:raise ValueError('Cannot reconstruct frozen confirmation')
            reconstructed=study.candidate(row,study.MODEL,candle_ms=r['signal_candle_ms'])
            if not reconstructed or any(reconstructed[k]!=r[k] for k in
                    ('key','setup_id','ticker','side','created_ms','expires_ms','stop','target','trigger')):
                raise ValueError('Original candidate does not reproduce exactly')
            diagnostics.append(diagnose(r,row,frames['MINUTE_15'],state_at,end_ms=source['end_ms']))
    if any(t not in found for t in by_ticker):raise ValueError('Missing candidate ticker source')
    return dict(dataset='DESCRIPTIVE_FROZEN_ENTRY_PRICE_DIAGNOSTICS',source_role=ledger['role'],
                start_ms=ledger['start_ms'],end_ms=ledger['end_ms'],
                source_report_sha256=hashlib.sha256(original_bytes).hexdigest(),
                source_manifest_sha256=hashlib.sha256(source_bytes).hexdigest(),
                source_files_verified=verified,original_candidates_reproduced=len(diagnostics),
                original_metrics=ledger['metrics'][study.MODEL],original_portfolio=ledger['portfolios'][study.MODEL],
                observation=dict(original_pending_bars=study.WAIT_BARS,extra_observation_bars=EXTRA_OBSERVATION_BARS,
                                 distance_r_basis='TRIGGER_TO_FROZEN_STOP_PRICE_DISTANCE',bins=list(DISTANCE_BINS)),
                summary=summarize(diagnostics),records=diagnostics,
                changes_live_rules=False,real_orders_enabled=False,automatic_promotion=False,
                eligible_for_live_promotion=False,
                limitations=['Descriptive diagnostics of previously viewed retrospective periods; not untouched validation.',
                             'Four extra bars measure prices only; expiry and all frozen entries/outcomes stay unchanged.',
                             'A late touch is not a fill, incremental setup, profitable trade or an expiry-extension backtest.',
                             'Unknown candle/indicator history stops observation; signal-bar extrema are excluded.',
                             'Stop/target on a touch bar has unknown intrabar order; never infer profit.',
                             'Do not select parameters from these counts; insufficient resolved samples stay insufficient.'])


def main():
    import argparse
    from analysis_terminal import server
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.resolve()==args.report.resolve():raise ValueError('Do not overwrite frozen ledger')
    result=build(args.source,args.report,server.analyze_contract,server.SETTINGS)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps(result['summary']))


if __name__=='__main__':main()
