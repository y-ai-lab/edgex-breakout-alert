"""Registered research only: confirmed-zone limit, measured room, net 2R TP.

No server hooks, SQLite writes, notifications, private APIs or promotion. Existing
frozen controls are reproduced independently and never edited by this module.
"""
from bisect import bisect_right
from collections import Counter
from dataclasses import fields
import hashlib
import json
import math
from pathlib import Path

import app as scanner
from analysis_terminal import execution_funnel, pending_entry_replay as control
from analysis_terminal.replay import complete_window, rule_fingerprint, strategy_parameters
from analysis_terminal.setups import setup_identity

MODEL='confirmation_zone_measured_net2r'
PROTOCOL=Path(__file__).with_name('confirmation_zone_protocol.json')
STEP,FEE,SLIP,MIN_RR,WAIT=control.STEP,control.FEE,control.SLIP,2.0,4


def positive(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>0


def net_target(reference,stop,side):
    if side not in {'LONG','SHORT'} or not all(positive(v) for v in (reference,stop)):
        return None
    if not (stop<reference if side=='LONG' else stop>reference):return None
    levels=control.cost_levels(reference,stop,stop,side)
    entry,risk=levels['entry'],levels['net_risk']
    target=((entry*(1+FEE)+MIN_RR*risk)/((1-FEE)*(1-SLIP)) if side=='LONG'
            else (entry*(1-FEE)-MIN_RR*risk)/((1+FEE)*(1+SLIP)))
    return target if positive(target) else None


def candidate(row,*,candle_ms):
    identity=setup_identity(row);side=row.get('direction')
    if (not identity or row.get('shadow_v2_ready') is not True
            or any(row.get(k) is not True for k in ('confirmation_color_ok','confirmation_level_ok','retest_touched','stop_valid'))):
        return None
    reference,stop,extension,roll=(row.get(k) for k in
        ('entry_reference','shadow_stop_loss','shadow_v2_extension_target','breakout_level'))
    if not all(positive(v) for v in (reference,stop,extension,roll)):return None
    if not (reference>roll if side=='LONG' else reference<roll):return None
    target=net_target(reference,stop,side)
    if target is None:return None
    inside_room=target<=extension if side=='LONG' else target>=extension
    if not inside_room and not math.isclose(target,extension,rel_tol=1e-12,abs_tol=0):return None
    return dict(key=MODEL+':'+identity,model=MODEL,setup_id=identity,ticker=row['ticker'],side=side,
                stop=stop,target=target,extension_target=extension,roll_level=roll,trigger=reference,
                signal_candle_ms=candle_ms,created_ms=candle_ms+STEP+1,expires_ms=candle_ms+STEP+WAIT*STEP,
                status='PENDING',filled_ms=None,outcome_ms=None,final_net_r=None,mfe_r=0.0,mae_r=0.0)


def evaluate(record,candles,state_at,*,end_ms):
    r=dict(record);start=r['created_ms']-1;long=r['side']=='LONG'
    if (start!=r['signal_candle_ms']+STEP or start%STEP or end_ms%STEP or r['model']!=MODEL
            or r['expires_ms']!=start+WAIT*STEP):
        raise ValueError('Invalid entry exclusion clock or research model')
    series={c.time_ms:c for c in candles if c.time_ms+STEP<=end_ms}
    for stamp in range(start,end_ms,STEP):
        if r['status']=='PENDING' and stamp>=r['expires_ms']:
            r.update(status='EXPIRED',outcome_ms=stamp);break
        c=series.get(stamp)
        if c is None:r.update(status='DATA_GAP',outcome_ms=stamp+STEP);break
        if r['status']=='PENDING':
            state=state_at(stamp)
            if state is None:r.update(status='DATA_GAP',outcome_ms=stamp+STEP);break
            if state.get('setup_id')!=r['setup_id']:
                r.update(status='INVALIDATED',reason='SETUP_CHANGED',outcome_ms=stamp);break
            if any(state.get(k) is not True for k in ('confirmation_color_ok','confirmation_level_ok','retest_touched','stop_valid')):
                r.update(status='INVALIDATED',reason='CLOSED_CONFIRMATION_OR_STRUCTURE_LOST',outcome_ms=stamp);break
            if c.open<=r['stop'] if long else c.open>=r['stop']:
                r.update(status='INVALIDATED_GAP',outcome_ms=stamp+STEP);break
            if c.open<=r['roll_level'] if long else c.open>=r['roll_level']:
                r.update(status='REJECTED_AT_FILL',reason='OPEN_OUTSIDE_CONFIRMATION_ZONE',outcome_ms=stamp+STEP);break
            if not (c.low<=r['trigger'] if long else c.high>=r['trigger']):continue
            nominal=min(c.open,r['trigger']) if long else max(c.open,r['trigger'])
            levels=control.cost_levels(nominal,r['stop'],r['target'],r['side']);price=levels['entry']
            valid=r['stop']<price<r['target'] if long else r['target']<price<r['stop']
            if not valid or levels['net_rr']<MIN_RR-1e-9:
                r.update(status='REJECTED_AT_FILL',reason='NET_RR_OR_STRUCTURE',outcome_ms=stamp+STEP);break
            r.update(levels,status='OPEN',filled_ms=stamp,nominal_fill=nominal,
                     gross_rr=abs(r['target']-price)/abs(price-r['stop']))
        sl,tp=(c.low<=r['stop'],c.high>=r['target']) if long else (c.high>=r['stop'],c.low<=r['target'])
        if stamp==r['filled_ms']:
            if sl or tp:
                r.update(status='AMBIGUOUS',reason='EXIT_TOUCH_ON_UNKNOWN_INTRABAR_FILL',outcome_ms=stamp+STEP);break
            continue
        risk=abs(r['entry']-r['stop'])
        r['mfe_r']=max(r['mfe_r'],((c.high-r['entry']) if long else (r['entry']-c.low))/risk)
        r['mae_r']=max(r['mae_r'],((r['entry']-c.low) if long else (c.high-r['entry']))/risk)
        if sl and tp:
            r.update(status='AMBIGUOUS',reason='TP_AND_SL_SAME_BAR',outcome_ms=stamp+STEP);break
        if sl or tp:
            level=r['stop'] if sl else r['target']
            nominal=min(c.open,level) if sl and long else max(c.open,level) if sl else level
            d=1 if long else -1;price=nominal*(1-d*SLIP)
            pnl=d*(price-r['entry'])-FEE*(price+r['entry'])
            r.update(status='SL' if sl else 'TP',exit_price=price,net_pnl_per_unit=pnl,
                     final_net_r=pnl/r['net_risk'],outcome_ms=stamp+STEP);break
    if r['status']=='PENDING' and r['expires_ms']<=end_ms:
        r.update(status='EXPIRED',outcome_ms=r['expires_ms'])
    return r


def decision(reports):
    ms=[r['metrics'][MODEL] for r in reports]
    if any(m['resolved']>=20 and (m['avg_net_r']<=0 or m['profit_factor']!='INF' and m['profit_factor']<=1) for m in ms):
        return 'KILL_NO_LIVE_PROMOTION'
    if any(m['resolved']<20 for m in ms):return 'CONTINUE_INSUFFICIENT_SAMPLE'
    if any(r['comparison']['net_filled_count_difference']<=0 for r in reports):return 'PIVOT_NO_INCREMENTAL_FILLS'
    return 'FORWARD_SHADOW_REQUIRED'


def build(source_dir,analyze,settings,*,role,baseline_path=None):
    protocol_bytes=PROTOCOL.read_bytes();p=json.loads(protocol_bytes)
    if (p['model']!=MODEL or p['rules']['min_net_rr']!=MIN_RR or p['rules']['pending_bars']!=WAIT
            or p['rules']['fee_bps_each_side']!=FEE*10000 or p['rules']['slippage_bps_each_side']!=SLIP*10000
            or p['production_parameters']!=strategy_parameters(settings)):
        raise ValueError('Implementation differs from registered rules')
    source=json.loads((source_dir/'replay-report.json').read_text());start,end=source['start_ms'],source['end_ms']
    if source.get('dataset')!='RETROSPECTIVE' or source.get('eligible_for_live_promotion') is not False:
        raise ValueError('Expected isolated public candle source')
    if role in {'development','validation'}:
        if (start,end)!=(p[role]['start_ms'],p[role]['end_ms']):raise ValueError('Unregistered retrospective period')
    elif role=='prospective':
        origin=p['prospective_start_ms']
        if start<origin or (start-origin)%(7*86400000) or not start<end<=start+7*86400000 or end%86400000:
            raise ValueError('Unregistered forward weekly window')
    else:raise ValueError('Invalid research role')
    actual=rule_fingerprint(analyze,settings)
    if (source['manifest']['parameters']!=p['production_parameters'] or source['manifest']['rule_fingerprint'] not in
            {actual,'5c9e20d6b1deeba5ed2a77daef934e1d4ced10c6162bed1aaabe02eadb95ee2d'}):
        raise ValueError('Production rule fingerprint mismatch')
    if source['manifest']['indicator_windows']!={'HOUR_4':180,'MINUTE_15':180}:
        raise ValueError('Unexpected indicator window')
    if baseline_path:
        original=baseline_path.read_bytes();baseline=json.loads(original);execution_funnel.summarize(baseline)
        if (baseline['dataset']!='RETROSPECTIVE' or (baseline['start_ms'],baseline['end_ms'])!=(start,end)
                or baseline['production_rule_fingerprint']!=actual):raise ValueError('Frozen comparator mismatch')
        controls=baseline['records']
    else:original=None;baseline=None;controls=[]
    seeds=source['signals']['shadow'];ids=[s['setup_id'] for s in seeds]
    if len(set(ids))!=len(ids):raise ValueError('Duplicate first Shadow setup')
    sources={};found=set();records=[];exclusions=Counter();allowed={f.name for f in fields(scanner.Contract)}
    for item in source['manifest']['sources']:
        ticker=item['ticker']
        if ticker in found:raise ValueError('Duplicate source ticker')
        found.add(ticker);path=(source_dir/item['file']).resolve()
        if not path.is_relative_to(source_dir.resolve()):raise ValueError('Source path escapes archive')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Candle checksum mismatch')
        sources[ticker]=json.loads(raw)
    if any(s['ticker'] not in found for s in seeds):raise ValueError('Missing seed source')
    for ticker,data in sources.items():
        contract=scanner.Contract(**{k:v for k,v in data['contract'].items() if k in allowed})
        if contract.contract_name!=ticker:raise ValueError('Contract mismatch')
        frames={i:[scanner.Candle(**c) for c in data[i]] for i in ('HOUR_4','MINUTE_15')}
        for i,cs in frames.items():
            if (any(a.time_ms>=b.time_ms for a,b in zip(cs,cs[1:])) or any(c.contract_id!=contract.contract_id or c.interval!=i
                or c.time_ms%scanner.INTERVAL_MS[i] or not all(positive(v) for v in (c.open,c.high,c.low,c.close))
                or not c.low<=min(c.open,c.close)<=max(c.open,c.close)<=c.high for c in cs)):
                raise ValueError('Invalid candle identity, time or OHLC')
        if baseline is None:
            controls.extend(control.replay_market(contract,frames['HOUR_4'],frames['MINUTE_15'],analyze,settings,
                            start_ms=start,end_ms=end)['records'])
        times={i:[c.time_ms+scanner.INTERVAL_MS[i] for c in cs] for i,cs in frames.items()};cache={}
        def state_at(stamp):
            if stamp not in cache:
                windows={i:complete_window(cs,bisect_right(times[i],stamp+1),180,scanner.INTERVAL_MS[i],stamp+1) for i,cs in frames.items()}
                if any(w is None for w in windows.values()):cache[stamp]=None
                else:
                    row=analyze(contract,windows['HOUR_4'],windows['MINUTE_15'],as_of_ms=stamp+1)
                    cache[stamp]=dict(row,setup_id=setup_identity(row))
            return cache[stamp]
        for seed in [s for s in seeds if s['ticker']==ticker]:
            stamp=seed['created_ms']-1;row=state_at(stamp)
            if (not row or seed['created_ms']!=seed['signal_candle_ms']+STEP+1 or not start<=stamp<end
                    or setup_identity(dict(seed,direction=seed['side']))!=seed['setup_id']
                    or row['setup_id']!=seed['setup_id'] or row.get('shadow_v2_ready') is not True):
                raise ValueError('First qualified Shadow seed does not reproduce')
            for stored,current in [('entry','entry_reference'),('stop','shadow_stop_loss'),('target','shadow_v2_target'),('extension_target','shadow_v2_extension_target')]:
                if seed[stored]!=row[current]:raise ValueError('Frozen seed price mismatch')
            r=candidate(row,candle_ms=seed['signal_candle_ms'])
            if r is None:exclusions['FIRST_SEED_NET_ROOM_INELIGIBLE']+=1;continue
            r.update(step_size=contract.step_size,min_order_size=contract.min_order_size,max_order_size=contract.max_order_size)
            records.append(evaluate(r,frames['MINUTE_15'],state_at,end_ms=end))
    for model,archived in [('current_next_open','current'),('shadow_v2_next_open','shadow')]:
        saved={(r['setup_id'],r['created_ms'],r['trigger'],r['stop'],r['target']) for r in controls if r['model']==model}
        expected={(s['setup_id'],s['created_ms'],s['entry'],s['stop'],s['target']) for s in source['signals'][archived]}
        if saved!=expected or len(saved)!=len(source['signals'][archived]):
            raise ValueError('Comparator first entries differ from source')
    models=(*control.MODELS,MODEL);all_records=controls+records
    metrics={m:control.metrics([r for r in all_records if r['model']==m]) for m in models}
    portfolios={m:control.portfolio([r for r in all_records if r['model']==m]) for m in models}
    if baseline and (any(metrics[m]!=baseline['metrics'][m] or portfolios[m]!=baseline['portfolios'][m] for m in control.MODELS)
                     or baseline_path.read_bytes()!=original):raise ValueError('Frozen comparator changed')
    current={r['setup_id'] for r in controls if r['model']=='current_next_open' and r['filled_ms'] is not None}
    filled={r['setup_id'] for r in records if r['filled_ms'] is not None}
    return dict(protocol=p['protocol'],dataset='ISOLATED_REGISTERED_SHADOW_REPLAY',role=role,start_ms=start,end_ms=end,
                period_complete=role!='prospective' or end-start==7*86400000,
                protocol_sha256=hashlib.sha256(protocol_bytes).hexdigest(),engine_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                baseline_report_sha256=hashlib.sha256(original).hexdigest() if original else None,
                first_shadow_seeds=len(seeds),seed_exclusions=dict(exclusions),source_files_verified=len(sources),
                metrics=metrics,portfolios=portfolios,records=all_records,
                comparison=dict(shared_filled_setups=len(current&filled),proposal_only_filled_setups=len(filled-current),
                 current_filled_missing_in_proposal=len(current-filled),net_filled_count_difference=len(filled)-len(current),
                 capped_filled_count_difference=portfolios[MODEL]['filled']-portfolios['current_next_open']['filled']),
                changes_live_rules=False,real_orders_enabled=False,automatic_promotion=False,eligible_for_live_promotion=False,
                limitations=p['limitations'])


def main():
    import argparse
    from analysis_terminal import server
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True);parser.add_argument('--baseline',type=Path)
    parser.add_argument('--role',choices=('development','validation','prospective'),required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=build(args.source,server.analyze_contract,server.SETTINGS,role=args.role,baseline_path=args.baseline)
    if args.baseline and args.output.resolve()==args.baseline.resolve():raise ValueError('Do not overwrite frozen controls')
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'metrics':result['metrics'][MODEL],'comparison':result['comparison']}))


if __name__=='__main__':main()
