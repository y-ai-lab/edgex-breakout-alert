"""Preregistered independent 15M continuation research; no DB, alerts or orders."""
from bisect import bisect_right
from collections import Counter
from dataclasses import fields
import hashlib
import json
import math
from pathlib import Path

import app as scanner
from analysis_terminal import confirmation_zone_replay as zone, pending_entry_replay as control, sweep_reclaim_replay as frozen
from analysis_terminal.replay import complete_window,rule_fingerprint,strategy_parameters

MODEL='trend_15m_break_retest_net2r'
PROTOCOL=Path(__file__).with_name('break_retest_protocol.json')
STEP,STEP4,WINDOW,RANGE,WAIT=900000,14400000,180,20,4
CONFIRM_BARS,STOP_BUFFER,TARGET_BUFFER,MIN_RR=6,.5,.25,2.0
STRESS_FEE,STRESS_SLIP=.001,.0005


def closed_clock(monitor,entries):
    if len(monitor)!=WINDOW or len(entries)!=WINDOW:
        raise ValueError('Exactly 180 closed indicator bars required')
    close_ms=entries[-1].time_ms+STEP
    if entries[-1].time_ms%STEP or monitor[-1].time_ms+STEP4!=close_ms//STEP4*STEP4:
        raise ValueError('Future, stale or off-grid indicator clock')


def breakout(contract,monitor,entries,*,direction=None):
    closed_clock(monitor,entries)
    side=direction if direction is not None else frozen.trend(monitor)
    if side not in {'LONG','SHORT'}:return None,None
    c=entries[-1];prior=entries[-RANGE-1:-1];long=side=='LONG'
    if len(prior)!=RANGE:raise ValueError('Incomplete prior range')
    low,high=min(b.low for b in prior),max(b.high for b in prior)
    if not (c.close>high and c.close>c.open if long else c.close<low and c.close<c.open):return None,None
    level=high if long else low;episode=(side,monitor[-1].time_ms,frozen.level_key(level))
    atr=scanner._atr(entries,14)
    if not zone.positive(atr) or not high>low:return None,episode
    extension=high+high-low-TARGET_BUFFER*atr if long else low-(high-low)+TARGET_BUFFER*atr
    return dict(setup_id=f'break-retest-v1:{contract.contract_name}:{side}:{c.time_ms}:{frozen.level_key(level)}',
                ticker=contract.contract_name,side=side,breakout_time_ms=c.time_ms,breakout_close_ms=c.time_ms+STEP,
                roll_level=level,range_low=low,range_high=high,range_start_ms=prior[0].time_ms,range_end_ms=prior[-1].time_ms,
                extension_target=extension,atr_15m_at_breakout=atr,trend_anchor_ms=monitor[-1].time_ms,
                extreme=None,touched=False),episode


def advance(setup,monitor,entries,*,direction=None):
    """First later wick retest and strict close; no source mutation or future SL."""
    closed_clock(monitor,entries)
    s=dict(setup);c=entries[-1];long=s['side']=='LONG'
    offset=(c.time_ms-s['breakout_time_ms'])//STEP
    if offset<=0:raise ValueError('Breakout candle cannot retest or confirm')
    if offset>CONFIRM_BARS:return None,None,'SETUP_EXPIRED'
    side=direction if direction is not None else frozen.trend(monitor)
    if side!=s['side']:return None,None,'TREND_INVALIDATED'
    if not (c.close>s['roll_level'] if long else c.close<s['roll_level']):
        return None,None,'CLOSE_LOST_BREAKOUT_EDGE'
    value=c.low if long else c.high
    s['extreme']=value if s['extreme'] is None else min(s['extreme'],value) if long else max(s['extreme'],value)
    s['touched']=s['touched'] or (c.low<=s['roll_level'] if long else c.high>=s['roll_level'])
    color=c.close>c.open if long else c.close<c.open
    if not s['touched'] or not color:
        return (None,None,'SETUP_EXPIRED') if offset==CONFIRM_BARS else (s,None,'CONFIRMATION_WAIT')
    atr=scanner._atr(entries,14)
    if not zone.positive(atr):return None,None,'CONFIRMED_INVALID_ATR'
    stop=s['extreme']-STOP_BUFFER*atr if long else s['extreme']+STOP_BUFFER*atr
    target=zone.net_target(c.close,stop,s['side']);extension=s['extension_target']
    if target is None or not all(zone.positive(v) for v in (stop,target,extension)):
        return None,None,'CONFIRMED_INVALID_STRUCTURE'
    if not (target<=extension if long else target>=extension) and not math.isclose(target,extension,rel_tol=1e-12):
        return None,None,'CONFIRMED_NET_ROOM_BELOW_2R'
    stamp=c.time_ms+STEP
    return None,dict(s,key=MODEL+':'+s['setup_id'],model=MODEL,trigger=c.close,stop=stop,target=target,
                     signal_candle_ms=c.time_ms,created_ms=stamp+1,expires_ms=stamp+WAIT*STEP,
                     atr_15m_at_confirmation=atr,status='PENDING',filled_ms=None,outcome_ms=None,final_net_r=None,
                     mfe_r=0.0,mae_r=0.0),None


def evaluate(record,candles,state_at,*,end_ms):
    if record['model']!=MODEL or record['key']!=MODEL+':'+record['setup_id']:
        raise ValueError('Invalid independent continuation identity')
    result=zone.evaluate(dict(record,model=zone.MODEL),candles,state_at,end_ms=end_ms)
    result['model']=MODEL
    return result


def replay_market(contract,monitor,entries,*,start_ms,end_ms):
    if not 0<start_ms<end_ms or start_ms%STEP or end_ms%STEP:raise ValueError('Invalid study window')
    for cs,interval,step in [(monitor,'HOUR_4',STEP4),(entries,'MINUTE_15',STEP)]:
        if any(a.time_ms>=b.time_ms for a,b in zip(cs,cs[1:])) or any(
                c.contract_id!=contract.contract_id or c.interval!=interval or c.time_ms%step
                or not all(zone.positive(v) for v in (c.open,c.high,c.low,c.close))
                or not c.low<=min(c.open,c.close)<=max(c.open,c.close)<=c.high for c in cs):
            raise ValueError('Invalid candle identity, order, grid or OHLC')
    times4,times15=[c.time_ms+STEP4 for c in monitor],[c.time_ms+STEP for c in entries]
    frames,trends={},{}
    def windows_at(stamp):
        if stamp not in frames:
            w4=complete_window(monitor,bisect_right(times4,stamp+1),WINDOW,STEP4,stamp+1)
            w15=complete_window(entries,bisect_right(times15,stamp+1),WINDOW,STEP,stamp+1)
            if w4 is None or w15 is None:frames[stamp]=None
            else:
                anchor=w4[-1].time_ms
                if anchor not in trends:trends[anchor]=frozen.trend(w4)
                frames[stamp]=(w4,w15,trends[anchor])
        return frames[stamp]
    active=None;seen=set();records=[];excluded=Counter();valid=0;unknown_through=0;blocked_until=0
    for stamp in range(start_ms-25*3600000,end_ms,STEP):
        frame=windows_at(stamp)
        if frame is None:
            active=None;unknown_through=stamp+1
            if stamp>=start_ms:excluded['INCOMPLETE_INDICATOR_WINDOW']+=1
            continue
        w4,w15,direction=frame;valid+=stamp>=start_ms
        if active is not None:
            original=active
            active,r,reason=advance(active,w4,w15,direction=direction)
            if r is not None or reason and reason.startswith('CONFIRMED_'):
                blocked_until=stamp+WAIT*STEP
                if original['breakout_close_ms']<start_ms or stamp<start_ms:
                    excluded['WARMUP_BREAKOUT_CONFIRMATION']+=1
                elif r is None:excluded[reason]+=1
                else:
                    r.update(step_size=contract.step_size,min_order_size=contract.min_order_size,max_order_size=contract.max_order_size)
                    records.append(r)
            elif active is None and stamp>=start_ms:excluded[reason]+=1
            continue
        if stamp<blocked_until:continue
        s,episode=breakout(contract,w4,w15,direction=direction)
        if episode is None:continue
        if episode in seen:
            if stamp>=start_ms:excluded['DUPLICATE_BREAKOUT_EPISODE']+=1
            continue
        seen.add(episode)
        if w4[-1].time_ms+STEP4<=unknown_through:
            if stamp>=start_ms:excluded['UNKNOWN_FIRST_EPISODE']+=1
            continue
        if s is None:
            if stamp>=start_ms:excluded['BREAKOUT_INVALID_RANGE_OR_ATR']+=1
            continue
        active=s
    results=[]
    for r in records:
        def state_at(stamp):
            frame=windows_at(stamp)
            if frame is None:return None
            w4,w15,direction=frame
            return frozen.pending_state(r,w4,w15,direction=direction)
        results.append(evaluate(r,entries,state_at,end_ms=end_ms))
    return dict(records=results,valid_points=valid,expected_points=(end_ms-start_ms)//STEP,exclusions=dict(excluded))


def stressed_metrics(records):
    """Same trades/outcomes, hypothetical higher costs; never another sample."""
    rows=[]
    for original in records:
        r=dict(original)
        if r['status'] in {'TP','SL'}:
            d=1 if r['side']=='LONG' else -1
            entry=r['nominal_fill']*(1+d*STRESS_SLIP)
            stop=r['stop']*(1-d*STRESS_SLIP)
            risk=d*(entry-stop)+STRESS_FEE*(entry+stop)
            exit_nominal=r['exit_price']/(1-d*zone.SLIP)
            exit_price=exit_nominal*(1-d*STRESS_SLIP)
            r['final_net_r']=(d*(exit_price-entry)-STRESS_FEE*(exit_price+entry))/risk
        rows.append(r)
    m=control.metrics(rows)
    return {k:v for k,v in m.items() if k not in ('avg_mfe_r','avg_mae_r')}


def decision(reports):
    for r in reports:
        m=r['metrics'][MODEL];pf=m['profit_factor']
        if m['resolved']>=20 and (m['avg_net_r']<=0 or math.isclose(m['avg_net_r'],0,abs_tol=1e-12)
                or pf!='INF' and (pf<=1 or math.isclose(pf,1,rel_tol=1e-12))):
            return 'KILL_NO_LIVE_PROMOTION'
    if any(r['metrics'][MODEL]['resolved']<50 for r in reports):return 'CONTINUE_INSUFFICIENT_SAMPLE'
    for r in reports:
        m=r['metrics'][MODEL];s=r['stress_metrics'][MODEL]
        if (m['win_rate']<40 or m['profit_factor']!='INF' and m['profit_factor']<1.2
                or s['avg_net_r']<=0 or s['profit_factor']!='INF' and s['profit_factor']<=1
                or r['comparison']['net_filled_count_difference']<=0):
            return 'PIVOT_NO_PRACTICAL_MARGIN'
    if len(reports)<2:return 'HISTORICAL_VALIDATION_REQUIRED'
    if any(r['portfolios'][MODEL]['resolved']<20 for r in reports):return 'CONTINUE_INSUFFICIENT_CAPITAL_SAMPLE'
    if any(r['portfolios'][MODEL]['closed_portfolio_roi_pct'] is None for r in reports):return 'CONTINUE_ROI_UNVERIFIED'
    if any(r['portfolios'][MODEL]['closed_portfolio_roi_pct']<=0 for r in reports):return 'PIVOT_NO_CAPITAL_PROFIT'
    return 'LIVE_CAPTURE_SHADOW_REQUIRED'


def build(source_dir,analyze,settings,*,role,baseline_path=None):
    if (role=='development') != (baseline_path is not None):
        raise ValueError('Development requires the registered frozen comparator; other roles cannot replace it')
    protocol_bytes = PROTOCOL.read_bytes();p = json.loads(protocol_bytes)
    expected_rules = dict(range_bars=RANGE,atr_period=14,trend_fast_ema=20,trend_slow_ema=50,
                         atr_stop_buffer=STOP_BUFFER,atr_target_buffer=TARGET_BUFFER,min_net_rr=MIN_RR,confirmation_bars=CONFIRM_BARS,
                         pending_bars=WAIT,fee_bps_each_side=zone.FEE*10000,slippage_bps_each_side=zone.SLIP*10000)
    if p['model']!=MODEL or any(p['rules'][k]!=v for k,v in expected_rules.items()):
        raise ValueError('Implementation differs from registered rules')
    if (p['cost_stress']['fee_bps_each_side']!=STRESS_FEE*10000
            or p['cost_stress']['slippage_bps_each_side']!=STRESS_SLIP*10000):
        raise ValueError('Registered cost stress changed')
    raw=(PROTOCOL.parent.parent/'app.py').read_bytes();pins=p['scanner_sha256']
    published=raw[:-1] if hashlib.sha256(raw).hexdigest()==pins['local_with_one_extra_eof_newline'] else raw
    if (hashlib.sha256(published).hexdigest()!=pins['published'] or hashlib.sha256(published+b'\n').hexdigest()!=pins['local_with_one_extra_eof_newline']):
        raise ValueError('Frozen scanner changed')
    for name,digest in p['frozen_dependencies_sha256'].items():
        path=PROTOCOL.parent.parent/name if name=='app.py' else PROTOCOL.with_name(name)
        frozen.verify_frozen_dependency(name,path.read_bytes(),digest)
    source_bytes=(source_dir/'replay-report.json').read_bytes();source=json.loads(source_bytes)
    start,end=source['start_ms'],source['end_ms'];manifest=source['manifest']
    if role in ('development','validation'):
        if (start,end)!=(p[role]['start_ms'],p[role]['end_ms']):raise ValueError('Unregistered period')
    elif role=='prospective':
        origin=p['prospective_start_ms']
        if start<origin or (start-origin)%(7*86400000) or not start<end<=start+7*86400000 or end%86400000:
            raise ValueError('Unregistered forward week')
    else:raise ValueError('Invalid study role')
    actual=rule_fingerprint(analyze,settings)
    if (source.get('dataset')!='RETROSPECTIVE' or source.get('eligible_for_live_promotion') is not False
            or manifest['parameters']!=p['production_parameters'] or strategy_parameters(settings)!=p['production_parameters']
            or manifest['rule_fingerprint'] not in {actual,'5c9e20d6b1deeba5ed2a77daef934e1d4ced10c6162bed1aaabe02eadb95ee2d'}
            or manifest['indicator_windows']!=p['indicator_windows']
            or hashlib.sha256(json.dumps(manifest['universe'],sort_keys=True,separators=(',',':')).encode()).hexdigest()!=p['source_universe_sha256']):
        raise ValueError('Source universe, rules or indicator windows mismatch')
    original=baseline_path.read_bytes() if baseline_path else None
    if original and (role!='development' or hashlib.sha256(original).hexdigest()!=p['development_comparator_sha256']):
        raise ValueError('Unexpected frozen development comparator')
    baseline=json.loads(original) if original else None
    benchmark,records,coverage,excluded=[],[],Counter(),Counter()
    found=set();allowed={f.name for f in fields(scanner.Contract)}
    universe={c['contract_id']:scanner.Contract(**{k:v for k,v in c.items() if k in allowed}) for c in manifest['universe']}
    if len(universe)!=len(manifest['universe']):raise ValueError('Duplicate universe contract')
    for item in manifest['sources']:
        if item['ticker'] in found:raise ValueError('Duplicate source ticker')
        found.add(item['ticker']);path=(source_dir/item['file']).resolve()
        if not path.is_relative_to(source_dir.resolve()):raise ValueError('Source path escapes archive')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Candle checksum mismatch')
        data=json.loads(raw);contract=scanner.Contract(**{k:v for k,v in data['contract'].items() if k in allowed})
        if contract.contract_name!=item['ticker'] or universe.get(contract.contract_id)!=contract:
            raise ValueError('Contract identity mismatch')
        frames={i:[scanner.Candle(**c) for c in data[i]] for i in ('HOUR_4','MINUTE_15')}
        controls=control.replay_market(contract,frames['HOUR_4'],frames['MINUTE_15'],analyze,settings,start_ms=start,end_ms=end)
        benchmark.extend(controls['records'])
        result=replay_market(contract,frames['HOUR_4'],frames['MINUTE_15'],start_ms=start,end_ms=end)
        records.extend(result['records']);coverage['valid_points']+=result['valid_points'];excluded.update(result['exclusions'])
    for model,name in [('current_next_open','current'),('shadow_v2_next_open','shadow')]:
        saved={(r['setup_id'],r['created_ms'],r['trigger'],r['stop'],r['target']) for r in benchmark if r['model']==model}
        expected={(r['setup_id'],r['created_ms'],r['entry'],r['stop'],r['target']) for r in source['signals'][name]}
        if (saved!=expected or len(saved)!=sum(r['model']==model for r in benchmark)
                or len(expected)!=len(source['signals'][name])):raise ValueError('Original first entries did not reproduce')
    failed=[f['ticker'] for f in manifest['failures']]
    if len(failed)!=len(set(failed)) or found&set(failed) or found|set(failed)!={c.contract_name for c in universe.values()}:
        raise ValueError('Universe source/failure accounting mismatch')
    if baseline and ({r['key']:r for r in benchmark}!={r['key']:r for r in baseline['records']} or baseline_path.read_bytes()!=original):
        raise ValueError('Original control ledger did not reproduce exactly')
    all_records=benchmark+records;models=(*control.MODELS,MODEL)
    metrics={m:control.metrics([r for r in all_records if r['model']==m]) for m in models}
    portfolios={m:control.portfolio([r for r in all_records if r['model']==m]) for m in models}
    if baseline and any(metrics[m]!=baseline['metrics'][m] or portfolios[m]!=baseline['portfolios'][m] for m in control.MODELS):
        raise ValueError('Original control economics changed')
    current=[r for r in benchmark if r['model']=='current_next_open' and r['filled_ms'] is not None]
    filled=[r for r in records if r['filled_ms'] is not None]
    key=lambda r:(r['ticker'],r['side'],r['created_ms']-1)
    a,b={key(r) for r in current},{key(r) for r in filled}
    denominator=(end-start)//STEP*len(manifest['universe'])
    return dict(protocol=p['protocol'],dataset='ISOLATED_REGISTERED_BREAK_RETEST_REPLAY',role=role,start_ms=start,end_ms=end,
                period_complete=role!='prospective' or end-start==7*86400000,
                protocol_sha256=hashlib.sha256(protocol_bytes).hexdigest(),engine_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                source_manifest_sha256=hashlib.sha256(source_bytes).hexdigest(),source_files_verified=len(found),
                baseline_report_sha256=hashlib.sha256(original).hexdigest() if original else None,
                stress_metrics={m:stressed_metrics([r for r in all_records if r['model']==m]) for m in models},
                coverage=dict(valid_points=coverage['valid_points'],expected_points_all_markets=denominator,requested_markets=len(manifest['universe']),
                              fetched_markets=len(found),failed_markets=len(manifest['failures']),coverage_pct=100*coverage['valid_points']/denominator),
                exclusions=dict(excluded),metrics=metrics,portfolios=portfolios,records=all_records,
                comparison=dict(shared_entry_timestamps=len(a&b),proposal_only_entry_timestamps=len(b-a),current_missing_entry_timestamps=len(a-b),
                                net_filled_count_difference=len(filled)-len(current),
                                capped_filled_count_difference=portfolios[MODEL]['filled']-portfolios['current_next_open']['filled']),
                source_strategy_comparison=source['comparison'],changes_live_rules=False,real_orders_enabled=False,
                automatic_promotion=False,eligible_for_live_promotion=False,limitations=p['limitations'])


def main():
    import argparse
    from analysis_terminal import server
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--baseline',type=Path);parser.add_argument('--role',choices=('development','validation','prospective'),required=True)
    args=parser.parse_args()
    if args.output.resolve().is_relative_to(args.source.resolve()) or (args.baseline and args.output.resolve()==args.baseline.resolve()):
        raise ValueError('Do not overwrite frozen inputs')
    result=build(args.source,server.analyze_contract,server.SETTINGS,role=args.role,baseline_path=args.baseline)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'metrics':result['metrics'][MODEL],'comparison':result['comparison'],'coverage':result['coverage']}))


if __name__=='__main__':main()
