"""Offline preregistered local retest-stop study. No orders or live hooks.

Freeze first strict 4H setup confirmation, independent 15M SL, original 4H TP.
Only next-open fills with net RR >=2. No parameter search or promotion.
"""
import argparse
from bisect import bisect_right
from collections import Counter
from dataclasses import fields
import hashlib
import json
import math
from pathlib import Path

import app as scanner
from analysis_terminal import pending_entry_replay as control
from analysis_terminal.replay import complete_window, rule_fingerprint, strategy_parameters
from analysis_terminal.setups import setup_identity
from analysis_terminal.vwap_reclaim_replay import stressed_metrics

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT/'.github/research/local15_structure_protocol.json'
PROTOCOL_SHA256 = '5ddc5d2e11a5b058bf00dc4754f088c2df821d9c2e910e2a73efc084865213dd'
MODEL = 'confirmed_local15_stop_4h_target_next_open'
MODELS = control.MODELS+(MODEL,)
STEP, WEEK = 900000, 604800000


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def protocol():
    raw=PROTOCOL.read_bytes()
    if digest(raw)!=PROTOCOL_SHA256:
        raise ValueError('Registered protocol changed')
    return json.loads(raw)


def periods(p):
    return {'development':p['development'],**{x['id']:x for x in p['validation_periods']}}


def frozen(analyze,settings):
    p=protocol()
    for name,expected in p['frozen_dependencies_sha256'].items():
        if digest((ROOT/name).read_bytes())!=expected:
            raise ValueError('Frozen dependency changed')
    raw=(ROOT/'app.py').read_bytes();s=p['scanner_sha256']
    if digest(raw)!=s['local_with_one_extra_eof_newline'] and not (
            digest(raw)==s['published'] and digest(raw+b'\n')==s['local_with_one_extra_eof_newline']):
        raise ValueError('Frozen scanner changed')
    if strategy_parameters(settings)!=p['production_parameters'] or rule_fingerprint(analyze,settings)!=p['production_rule_fingerprint']:
        raise ValueError('Frozen analysis changed')
    return p


def candidate(row,window):
    """Closed signal-bar extremes define SL; never used as outcome prices."""
    identity=setup_identity(row);side=row.get('direction')
    if not identity or not all(row.get(k) is True for k in (
            'retest_touched','confirmed','confirmation_color_ok','confirmation_level_ok')):
        return None,'NO_STRICT_CONFIRMATION'
    band=row.get('entry_band') or {}
    old_stop,target=band.get('structural_stop'),band.get('structural_target')
    reference,roll,atr=row.get('entry_reference'),row.get('breakout_level'),row.get('atr_15m')
    if len(window)<4 or not all(isinstance(x,(int,float)) and not isinstance(x,bool)
            and math.isfinite(x) and x>0 for x in (old_stop,target,reference,roll,atr)):
        return None,'INVALID_STRUCTURE'
    local=min(c.low for c in window[-4:])-.5*atr if side=='LONG' else max(c.high for c in window[-4:])+.5*atr
    if not math.isfinite(local) or local<=0 or not (
            old_stop<local<reference<target and target>roll if side=='LONG'
            else target<reference<local<old_stop and target<roll):
        return None,'INVALID_OR_WIDER_LOCAL_STRUCTURE'
    levels=control.cost_levels(reference,local,target,side)
    if levels['net_rr'] is None or levels['net_rr']<2-1e-9:
        return None,'NO_NET_2R_AT_CONFIRMATION'
    close=window[-1].time_ms+STEP
    return dict(key=MODEL+':'+identity,model=MODEL,setup_id=identity,ticker=row['ticker'],side=side,
        stop=local,target=target,trigger=reference,original_4h_stop=old_stop,
        signal_candle_ms=window[-1].time_ms,created_ms=close+1,expires_ms=close+STEP,
        status='PENDING',filled_ms=None,final_net_r=None,outcome_ms=None,mfe_r=0.,mae_r=0.),None


def evaluate(record,candles,*,end_ms):
    """Known next-open ordering, no signal candle or missing-bar replacement."""
    r=dict(record);stamp=r['created_ms']-1
    first=next((c for c in candles if c.time_ms==stamp and c.time_ms+STEP<=end_ms),None)
    if first is not None:
        levels=control.cost_levels(first.open,r['stop'],r['target'],r['side'])
        valid=r['stop']<levels['entry']<r['target'] if r['side']=='LONG' else r['target']<levels['entry']<r['stop']
        if not valid or levels['net_rr'] is None or levels['net_rr']<2-1e-9:
            r.update(status='REJECTED_AT_FILL',outcome_ms=stamp+STEP,reason='NEXT_OPEN_NET_2R_OR_STRUCTURE_FAILED')
            return r
    # This distinct next-open model uses the original non-limit execution path.
    # Missing first/subsequent candle stops at DATA_GAP. Both exits AMBIGUOUS.
    return control.evaluate(r,candles,{},end_ms=end_ms,min_rr=2.)


def replay_market(contract,monitor,entries,analyze,settings,*,start_ms,end_ms):
    times4=[c.time_ms+16*STEP for c in monitor];times15=[c.time_ms+STEP for c in entries]
    seen=set();unknown=0;records=[];excluded=Counter();valid=0
    scan_start=start_ms-settings.roll_max_age*16*STEP-settings.retest_lookback*STEP
    for stamp in range(scan_start,end_ms,STEP):
        w4=complete_window(monitor,bisect_right(times4,stamp+1),180,16*STEP,stamp+1)
        w15=complete_window(entries,bisect_right(times15,stamp+1),180,STEP,stamp+1)
        if w4 is None or w15 is None:
            unknown=stamp+1
            if stamp>=start_ms:excluded['INCOMPLETE_INDICATOR_WINDOW']+=1
            continue
        valid+=stamp>=start_ms
        row=analyze(contract,w4,w15,as_of_ms=stamp+1)
        identity=setup_identity(row)
        if not identity or identity in seen or not all(row.get(k) is True for k in (
                'retest_touched','confirmed','confirmation_color_ok','confirmation_level_ok')):
            continue
        seen.add(identity) # Consume first confirmation including room/stop failures.
        if row['breakout_time_ms']+16*STEP<=unknown:
            if stamp>=start_ms:excluded['UNKNOWN_SETUP_ORIGIN']+=1
            continue
        if stamp<start_ms:
            excluded['WARMUP_FIRST_CONFIRMATION']+=1
            continue
        record,reason=candidate(row,w15)
        if record is None:
            excluded[reason]+=1
            continue
        record.update(step_size=contract.step_size,min_order_size=contract.min_order_size,
                      max_order_size=contract.max_order_size)
        records.append(evaluate(record,entries,end_ms=end_ms))
    return dict(records=records,valid_points=valid,excluded=dict(excluded),ticker=contract.contract_name)


def compare(groups,portfolios):
    current={r['setup_id'] for r in groups[control.MODELS[0]] if r['filled_ms'] is not None}
    proposal={r['setup_id'] for r in groups[MODEL] if r['filled_ms'] is not None}
    return dict(shared_filled_setups=len(current&proposal),proposal_only_filled_setups=len(proposal-current),
        current_filled_missing_in_proposal=len(current-proposal),net_filled_count_difference=len(proposal)-len(current),
        capped_filled_count_difference=portfolios[MODEL]['filled']-portfolios[control.MODELS[0]]['filled'],
        capped_shared_setup_count=None)


def decision(reports):
    p=protocol();registered=periods(p);seen=set()
    for r in reports:
        role=r['role'];period=registered.get(role)
        if not period or role in seen or (r['start_ms'],r['end_ms'])!=(period['start_ms'],period['end_ms']) or r['protocol_sha256']!=PROTOCOL_SHA256:
            raise ValueError('Unknown, repeated or changed period')
        seen.add(role)
    if any(r['coverage']['failed_markets'] or not r['coverage']['valid_points'] for r in reports):
        return 'BLOCKED_DATA_QUALITY'
    for r in reports:
        m=r['metrics'][MODEL]
        if r['period_complete'] and m['resolved']>=20 and (m['avg_net_r']<=0 or m['profit_factor']!='INF' and m['profit_factor']<=1):
            return 'KILL_NO_LIVE_PROMOTION'
    validation=[r for r in reports if r['role']!='development']
    if len(validation)!=2 or any(not r['period_complete'] or r['metrics'][MODEL]['resolved']<50 for r in validation):
        return 'CONTINUE_INSUFFICIENT_SAMPLE'
    for r in validation:
        m,s,a,c=r['metrics'][MODEL],r['stress_metrics'][MODEL],r['portfolios'][MODEL],r['comparison']
        if not (m['win_rate']>=40 and m['avg_net_r']>0 and (m['profit_factor']=='INF' or m['profit_factor']>=1.2)
                and s['avg_net_r']>0 and (s['profit_factor']=='INF' or s['profit_factor']>1)
                and c['net_filled_count_difference']>0 and c['capped_filled_count_difference']>0
                and a['resolved']>=20 and not a['active'] and not a['uncertain']
                and a['closed_portfolio_roi_pct'] is not None and a['closed_portfolio_roi_pct']>0):
            return 'NO_QUALIFIED_REPLACEMENT'
    return 'RETROSPECTIVE_CRITERIA_MET_SEPARATE_LIVE_SHADOW_AND_EXECUTION_VALIDATION_REQUIRED'


def validate_predecessors(role,previous):
    """Called before acquisition too: never open unused data after a KILL."""
    order=list(periods(protocol()));expected=order[:order.index(role)] if role in order else None
    if expected is None or [r['role'] for r in previous]!=expected:
        raise ValueError('Sequential predecessor evidence required')
    for r in previous:
        if r['engine_sha256']!=digest(Path(__file__).read_bytes()) or not r['period_complete']:
            raise ValueError('Changed or incomplete predecessor evidence')
        groups={m:[x for x in r['records'] if x['model']==m] for m in MODELS}
        if len({x['key'] for x in r['records']})!=len(r['records']) or any(
                control.metrics(rows)!=r['metrics'][m] or control.portfolio(rows)!=r['portfolios'][m]
                or stressed_metrics(rows)!=r['stress_metrics'][m] for m,rows in groups.items()):
            raise ValueError('Predecessor ledger does not reproduce')
        if compare(groups,r['portfolios'])!=r['comparison']:
            raise ValueError('Predecessor comparison does not reproduce')
    if previous and decision(previous) in {'KILL_NO_LIVE_PROMOTION','BLOCKED_DATA_QUALITY'}:
        raise ValueError('Predecessor killed or blocked: unused period stays sealed')


def run(source_dir,role,analyze,settings,*,previous=()):
    p=frozen(analyze,settings);validate_predecessors(role,previous);period=periods(p)[role]
    start,end=period['start_ms'],period['end_ms']
    if end-start!=WEEK or start%STEP or end%STEP or any(start<x['end_ms'] and x['start_ms']<end for x in p['sealed_periods']):
        raise ValueError('Invalid or sealed evaluation interval')
    source_dir=Path(source_dir).resolve();source_raw=(source_dir/'replay-report.json').read_bytes();source=json.loads(source_raw)
    if role=='development' and digest(source_raw)!=period['source_report_sha256']:
        raise ValueError('Development source changed')
    manifest=source['manifest'];known_old='5c9e20d6b1deeba5ed2a77daef934e1d4ced10c6162bed1aaabe02eadb95ee2d'
    if (source.get('dataset')!='RETROSPECTIVE' or source.get('eligible_for_live_promotion') is not False
            or (source['start_ms'],source['end_ms'])!=(start,end)
            or manifest['parameters']!=p['production_parameters'] or manifest['indicator_windows']!=p['indicator_windows']
            or manifest['rule_fingerprint'] not in {p['production_rule_fingerprint'],known_old}
            or digest(canonical(manifest['universe']))!=p['source_universe_sha256']):
        raise ValueError('Source differs from registration')
    universe={c['contract_name']:c for c in manifest['universe']};inventory=[x['ticker'] for x in manifest['sources']+manifest['failures']]
    if len(universe)!=len(manifest['universe']) or len(set(inventory))!=len(inventory) or set(inventory)!=set(universe):
        raise ValueError('Missing/duplicate market inventory')
    records=[];markets=[];hashes=[]
    for item in manifest['sources']:
        path=(source_dir/item['file']).resolve()
        if not path.is_relative_to(source_dir):raise ValueError('Source outside archive')
        raw=path.read_bytes()
        if digest(raw)!=item['sha256']:raise ValueError('Candle checksum mismatch')
        hashes.append((path,digest(raw)));data=json.loads(raw)
        if data['contract']!=universe[item['ticker']]:raise ValueError('Contract inventory changed')
        c=scanner.Contract(**{k:v for k,v in data['contract'].items() if k in {f.name for f in fields(scanner.Contract)}})
        w4=[scanner.Candle(**x) for x in data['HOUR_4']];w15=[scanner.Candle(**x) for x in data['MINUTE_15']]
        baseline=control.replay_market(c,w4,w15,analyze,settings,start_ms=start,end_ms=end)
        variant=replay_market(c,w4,w15,analyze,settings,start_ms=start,end_ms=end)
        if baseline['valid_points']!=variant['valid_points']:raise ValueError('Observation coverage differs')
        records.extend(baseline.pop('records'));records.extend(variant.pop('records'))
        markets.append(dict(control=baseline,proposal=variant))
    groups={m:[r for r in records if r['model']==m] for m in MODELS}
    if len({r['key'] for r in records})!=len(records):raise ValueError('Duplicate setup ledger')
    for m,old in zip(control.MODELS[:2],('current','shadow')):
        actual={(r['setup_id'],r['created_ms'],r['trigger'],r['stop'],r['target']) for r in groups[m]}
        original={(r['setup_id'],r['created_ms'],r['entry'],r['stop'],r['target']) for r in source['signals'][old]}
        if actual!=original or len(actual)!=len(groups[m]):raise ValueError('Independent comparator mismatch')
    for r in records:
        if not start<=r['created_ms']-1<end or r['created_ms']!=r['signal_candle_ms']+STEP+1:
            raise ValueError('Invalid signal chronology')
        if r['filled_ms'] is not None and not r['created_ms']-1<=r['filled_ms']<r['expires_ms']:
            raise ValueError('Invalid fill chronology')
    coverage=source['coverage']
    if (coverage['valid_points']!=sum(m['proposal']['valid_points'] for m in markets)
            or coverage['expected_points_all_markets']!=len(universe)*WEEK//STEP
            or coverage['failed_markets']!=len(manifest['failures']) or coverage['fetched_markets']!=len(markets)):
        raise ValueError('Source coverage does not reproduce')
    portfolios={m:control.portfolio(rows) for m,rows in groups.items()}
    report=dict(protocol=p['protocol'],dataset='RETROSPECTIVE',role=role,start_ms=start,end_ms=end,
        period_complete=True,eligible_for_live_promotion=False,automatic_promotion=False,real_orders_enabled=False,
        changes_live_rules=False,forward_collection_enabled=False,protocol_sha256=PROTOCOL_SHA256,
        engine_sha256=digest(Path(__file__).read_bytes()),source_report_sha256=digest(source_raw),
        original_baseline_first_entries_reproduced=True,source_files_verified=len(markets),coverage=coverage,
        metrics={m:control.metrics(rows) for m,rows in groups.items()},
        stress_metrics={m:stressed_metrics(rows) for m,rows in groups.items()},portfolios=portfolios,
        comparison=compare(groups,portfolios),markets=markets,records=records,limitations=p['limitations'])
    if digest((source_dir/'replay-report.json').read_bytes())!=digest(source_raw) or any(digest(path.read_bytes())!=h for path,h in hashes):
        raise ValueError('Source mutated during evaluation')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--role',choices=list(periods(protocol())),required=True)
    parser.add_argument('--previous-report',type=Path,action='append',default=[])
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    from analysis_terminal import server
    previous=[json.loads(p.read_text()) for p in args.previous_report]
    report=run(args.source,args.role,server.analyze_contract,server.SETTINGS,previous=previous)
    args.output.mkdir(parents=True,exist_ok=False)
    summary={k:v for k,v in report.items() if k not in {'records','markets'}}
    summary['decision']=decision(previous+[report])
    for name,value in [('report.json',report),('summary.json',summary)]:
        with (args.output/name).open('x') as f:json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({'role':report['role'],'decision':summary['decision'],'metrics':report['metrics'][MODEL],
                      'comparison':report['comparison'],'portfolio':report['portfolios'][MODEL]}))


if __name__=='__main__':main()
