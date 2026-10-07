"""Independent preregistered trend-aligned 15M sweep/reclaim research.

No server hooks, DB writes, notifications or exchange orders. Existing 4H
strategies and all frozen experimental protocols stay unchanged.
"""
from bisect import bisect_right
from collections import Counter
from dataclasses import fields
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

import app as scanner
from analysis_terminal import confirmation_zone_replay as zone, pending_entry_replay as control
from analysis_terminal.replay import complete_window, rule_fingerprint, strategy_parameters

MODEL = 'trend_range_sweep_reclaim_net2r'
PROTOCOL = Path(__file__).with_name('sweep_reclaim_protocol.json')
STEP, STEP4, WINDOW, RANGE, WAIT = zone.STEP, 14400000, 180, 20, 4
STOP_BUFFER, TARGET_BUFFER, MIN_RR = .5, .25, 2.0


def verify_frozen_dependency(name,raw,digest):
    actual=hashlib.sha256(raw).hexdigest()
    if actual==digest:
        return
    # The preregistered local app copy had exactly one additional EOF newline.
    # Accept only the unchanged published blob and proof of that precise byte difference.
    if (name=='app.py' and actual=='b1f4d4d15dc1ce4665f0f9f776de050059b60fb18faa4cf1583b04bf02a6c4af'
            and digest=='2e24f61e47ee40fdb3ae5f18152129f6632e83d508b73d27bf33fda0919ac9ab'
            and hashlib.sha256(raw+b'\n').hexdigest()==digest):
        return
    raise ValueError('Frozen control changed')


def level_key(value):
    return format(Decimal(str(value)).normalize(),'f')


def trend(monitor):
    fast = scanner._ema([c.close for c in monitor],20)
    slow = scanner._ema([c.close for c in monitor],50)
    if fast is None or slow is None:
        return None
    return 'LONG' if monitor[-1].close>fast>slow else 'SHORT' if monitor[-1].close<fast<slow else None


def candidate(contract, monitor, entries, *, direction=None):
    """All structural prices are known at signal close; prior range excludes it."""
    if len(entries)<RANGE+1 or not monitor:
        return None,None,'HISTORY_UNAVAILABLE'
    c = entries[-1]
    if (c.time_ms%STEP or monitor[-1].time_ms+STEP4>c.time_ms+STEP
            or any(a.time_ms+STEP!=b.time_ms for a,b in zip(entries,entries[1:]))):
        raise ValueError('Invalid signal clock or future monitor candle')
    side = direction if direction is not None else trend(monitor)
    if side not in {'LONG','SHORT'}:
        return None,None,'TREND_WAIT'
    prior = entries[-RANGE-1:-1]
    low,high = min(b.low for b in prior),max(b.high for b in prior)
    long = side=='LONG'
    swept = c.low<low and c.close>low and c.close>c.open if long else c.high>high and c.close<high and c.close<c.open
    if not swept:
        return None,None,'SWEEP_WAIT'
    level = low if long else high
    episode = (side,monitor[-1].time_ms,level_key(level))
    atr = scanner._atr(entries,14)
    if not zone.positive(atr):
        return None,episode,'INVALID_ATR'
    stop = c.low-STOP_BUFFER*atr if long else c.high+STOP_BUFFER*atr
    extension = high-TARGET_BUFFER*atr if long else low+TARGET_BUFFER*atr
    if not all(zone.positive(v) for v in (c.close,stop,extension,level)):
        return None,episode,'INVALID_STRUCTURE'
    target = zone.net_target(c.close,stop,side)
    if target is None or not (stop<c.close<extension if long else extension<c.close<stop):
        return None,episode,'INVALID_STRUCTURE'
    if not (target<=extension if long else target>=extension) and not math.isclose(target,extension,rel_tol=1e-12):
        return None,episode,'NET_ROOM_BELOW_2R'
    identity = f'sweep-reclaim-v1:{contract.contract_name}:{side}:{c.time_ms}:{level_key(level)}'
    close_ms = c.time_ms+STEP
    return dict(key=MODEL+':'+identity,model=MODEL,setup_id=identity,ticker=contract.contract_name,side=side,
                signal_candle_ms=c.time_ms,created_ms=close_ms+1,expires_ms=close_ms+WAIT*STEP,
                trigger=c.close,roll_level=level,stop=stop,target=target,extension_target=extension,
                range_low=low,range_high=high,range_start_ms=prior[0].time_ms,range_end_ms=prior[-1].time_ms,
                atr_15m_at_signal=atr,trend_anchor_ms=monitor[-1].time_ms,
                episode_key=f'{side}:{monitor[-1].time_ms}:{level_key(level)}',
                status='PENDING',filled_ms=None,outcome_ms=None,final_net_r=None,mfe_r=0.0,mae_r=0.0,
                step_size=contract.step_size,min_order_size=contract.min_order_size,max_order_size=contract.max_order_size),episode,None


def pending_state(record, monitor, entries, *, direction=None):
    side = direction if direction is not None else trend(monitor)
    long = record['side']=='LONG'
    c = entries[-1]
    aligned = side==record['side']
    return dict(setup_id=record['setup_id'] if aligned else None,
                confirmation_color_ok=c.close>c.open if long else c.close<c.open,
                confirmation_level_ok=c.close>record['roll_level'] if long else c.close<record['roll_level'],
                retest_touched=any(b.low<=record['roll_level'] if long else b.high>=record['roll_level'] for b in entries[-4:]),
                stop_valid=c.close>record['stop'] if long else c.close<record['stop'])


def evaluate(record, candles, state_at, *, end_ms):
    if record['model']!=MODEL or record['key']!=MODEL+':'+record['setup_id']:
        raise ValueError('Invalid independent sweep identity')
    result = zone.evaluate(dict(record,model=zone.MODEL),candles,state_at,end_ms=end_ms)
    result['model'] = MODEL
    return result


def replay_market(contract,monitor,entries,*,start_ms,end_ms):
    if not 0<start_ms<end_ms or start_ms%STEP or end_ms%STEP:
        raise ValueError('Invalid study window')
    for cs,interval,step in [(monitor,'HOUR_4',STEP4),(entries,'MINUTE_15',STEP)]:
        if any(a.time_ms>=b.time_ms for a,b in zip(cs,cs[1:])) or any(
                c.contract_id!=contract.contract_id or c.interval!=interval or c.time_ms%step
                or not all(zone.positive(v) for v in (c.open,c.high,c.low,c.close))
                or not c.low<=min(c.open,c.close)<=max(c.open,c.close)<=c.high for c in cs):
            raise ValueError('Invalid market candle identity, order, grid or OHLC')
    times4,times15 = [c.time_ms+STEP4 for c in monitor],[c.time_ms+STEP for c in entries]
    frames,trend_cache = {},{}
    def windows_at(stamp):
        if stamp not in frames:
            w4 = complete_window(monitor,bisect_right(times4,stamp+1),WINDOW,STEP4,stamp+1)
            w15 = complete_window(entries,bisect_right(times15,stamp+1),WINDOW,STEP,stamp+1)
            if w4 is None or w15 is None:
                frames[stamp] = None
            else:
                anchor = w4[-1].time_ms
                if anchor not in trend_cache:trend_cache[anchor] = trend(w4)
                frames[stamp] = (w4,w15,trend_cache[anchor])
        return frames[stamp]
    seen,records,excluded = set(),[],Counter()
    valid,unknown_through = 0,0
    for stamp in range(start_ms-25*3600000,end_ms,STEP):
        frame = windows_at(stamp)
        if frame is None:
            unknown_through = stamp+1
            if stamp>=start_ms:excluded['INCOMPLETE_INDICATOR_WINDOW'] += 1
            continue
        w4,w15,direction = frame
        valid += stamp>=start_ms
        r,episode,reason = candidate(contract,w4,w15,direction=direction)
        if episode is None:
            continue
        if episode in seen:
            if stamp>=start_ms:excluded['DUPLICATE_SWEEP_EPISODE'] += 1
            continue
        seen.add(episode)
        if w4[-1].time_ms+STEP4<=unknown_through:
            if stamp>=start_ms:excluded['UNKNOWN_FIRST_EPISODE'] += 1
            continue
        if stamp<start_ms:
            excluded['WARMUP_FIRST_EPISODE'] += 1
            continue
        if r is None:
            excluded[reason] += 1
            continue
        records.append(r)
    results = []
    for r in records:
        def state_at(stamp):
            frame = windows_at(stamp)
            if frame is None:return None
            w4,w15,direction = frame
            return pending_state(r,w4,w15,direction=direction)
        results.append(evaluate(r,entries,state_at,end_ms=end_ms))
    return dict(records=results,valid_points=valid,expected_points=(end_ms-start_ms)//STEP,exclusions=dict(excluded))


def decision(reports):
    ms = [r['metrics'][MODEL] for r in reports]
    # A mathematically zero expectancy / PF=1 must not pass on rounding noise.
    if any(m['resolved']>=20 and (m['avg_net_r']<=0 or math.isclose(m['avg_net_r'],0,abs_tol=1e-12)
            or m['profit_factor']!='INF' and (m['profit_factor']<=1 or math.isclose(m['profit_factor'],1,rel_tol=1e-12))) for m in ms):
        return 'KILL_NO_LIVE_PROMOTION'
    if any(m['resolved']<20 for m in ms):
        return 'CONTINUE_INSUFFICIENT_SAMPLE'
    if len(reports)<2:return 'HISTORICAL_VALIDATION_REQUIRED'
    if any(r['comparison']['net_filled_count_difference']<=0 for r in reports):
        return 'PIVOT_NO_INCREMENTAL_FILLS'
    return 'FORWARD_SHADOW_REQUIRED'


def build(source_dir,analyze,settings,*,role,baseline_path=None):
    if (role=='development') != (baseline_path is not None):
        raise ValueError('Development requires the registered frozen comparator; other roles cannot replace it')
    protocol_bytes = PROTOCOL.read_bytes();p = json.loads(protocol_bytes)
    expected_rules = dict(range_bars=RANGE,atr_period=14,trend_fast_ema=20,trend_slow_ema=50,
                         atr_stop_buffer=STOP_BUFFER,atr_target_buffer=TARGET_BUFFER,min_net_rr=MIN_RR,
                         pending_bars=WAIT,fee_bps_each_side=zone.FEE*10000,slippage_bps_each_side=zone.SLIP*10000)
    if p['model']!=MODEL or any(p['rules'][k]!=v for k,v in expected_rules.items()):
        raise ValueError('Implementation differs from registered rules')
    for name,digest in p['frozen_dependencies_sha256'].items():
        path=PROTOCOL.parent.parent/name if name=='app.py' else PROTOCOL.with_name(name)
        verify_frozen_dependency(name,path.read_bytes(),digest)
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
    return dict(protocol=p['protocol'],dataset='ISOLATED_REGISTERED_SWEEP_REPLAY',role=role,start_ms=start,end_ms=end,
                period_complete=role!='prospective' or end-start==7*86400000,
                protocol_sha256=hashlib.sha256(protocol_bytes).hexdigest(),engine_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                source_manifest_sha256=hashlib.sha256(source_bytes).hexdigest(),source_files_verified=len(found),
                baseline_report_sha256=hashlib.sha256(original).hexdigest() if original else None,
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
