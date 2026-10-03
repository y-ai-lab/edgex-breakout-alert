"""Independent, research-only EMA20 pullback Shadow. No collector, DB or Push hooks."""
from bisect import bisect_right
from collections import Counter
from dataclasses import fields
import hashlib
import inspect
import json
from pathlib import Path

import app as scanner
from analysis_terminal.comparison import metrics
from analysis_terminal.outcome_history import consecutive_window
from analysis_terminal.outcomes import evaluate_paper_signal
from analysis_terminal.replay import complete_window, replay_contract, rule_fingerprint, strategy_parameters
from analysis_terminal.setups import setup_identity

MODEL = 'trend_ema20_pullback_structural_room_fixed_2r'
PROTOCOL_PATH = Path(__file__).with_name('pullback_protocol.json')


def pullback_identity(ticker, direction, anchor):
    identity = setup_identity(dict(ticker=ticker,direction=direction,
                                  breakout_time_ms=anchor['time_ms'],breakout_level=anchor['level']))
    return identity.replace('setup-v1:', 'pullback-v1:', 1) if identity else None


def trend(monitor, settings):
    fast=scanner._ema([c.close for c in monitor],settings.trend_fast_ema)
    slow=scanner._ema([c.close for c in monitor],settings.trend_slow_ema)
    if fast is None or slow is None:
        return None,fast
    direction='LONG' if fast>slow and monitor[-1].close>fast else 'SHORT' if fast<slow and monitor[-1].close<fast else None
    return direction,fast


def discover_anchor(monitor, settings):
    if len(monitor)<max(settings.trend_slow_ema+1,settings.roll_lookback+1):
        return None
    direction,level=trend(monitor,settings)
    if direction is None:
        return None
    previous=scanner._ema([c.close for c in monitor[:-1]],settings.trend_fast_ema)
    latest=monitor[-1]
    if previous is None or not latest.low<=level<=latest.high:
        return None
    if not (monitor[-2].close>previous if direction=='LONG' else monitor[-2].close<previous):
        return None
    prior=monitor[-settings.roll_lookback-1:-1]
    return dict(time_ms=latest.time_ms,level=level,direction=direction,
                raw_extension=max(c.high for c in prior) if direction=='LONG' else min(c.low for c in prior))


def entry_for_anchor(contract, monitor, entries, anchor, settings):
    if anchor is None:
        return dict(stage='PULLBACK_WAIT',ready=False)
    direction,_=trend(monitor,settings)
    if direction!=anchor['direction']:
        return dict(stage='TREND_WAIT',ready=False)
    step4=scanner.INTERVAL_MS[settings.monitor_interval]
    boundary=anchor['time_ms']+step4
    if monitor[-1].time_ms-anchor['time_ms'] >= settings.roll_max_age*step4:
        return dict(stage='EXPIRED',ready=False)
    eligible=[c for c in monitor if c.time_ms>=anchor['time_ms']]
    latest=entries[-1]
    if not eligible or eligible[0].time_ms!=anchor['time_ms'] or latest.time_ms<boundary:
        return dict(stage='POST_ANCHOR_WAIT',ready=False)
    atr4=scanner._atr(monitor,settings.atr_period)
    atr15=scanner._atr(entries,settings.atr_period)
    if atr4 is None or atr15 is None:
        return dict(stage='DATA_WAIT',ready=False)
    level=anchor['level'];entry=latest.close
    tolerance=max(atr15*settings.retest_atr_tolerance,level*.001)
    retest=any(c.time_ms>=boundary and c.low<=level+tolerance and c.high>=level-tolerance
               for c in entries[-settings.retest_lookback:])
    color=latest.close>latest.open if direction=='LONG' else latest.close<latest.open
    reclaimed=latest.close>level if direction=='LONG' else latest.close<level
    stop=min(level,min(c.low for c in eligible))-atr4*settings.atr_stop_buffer if direction=='LONG' else max(level,max(c.high for c in eligible))+atr4*settings.atr_stop_buffer
    extension=anchor['raw_extension']-atr4*settings.atr_target_buffer if direction=='LONG' else anchor['raw_extension']+atr4*settings.atr_target_buffer
    valid=0<stop<entry<extension if direction=='LONG' else 0<extension<entry<stop
    risk=abs(entry-stop)
    room=abs(extension-entry)/risk if valid and risk>0 else None
    ready=bool(retest and color and reclaimed and room is not None and room>=settings.min_rr)
    stage='SHADOW_READY' if ready else 'RETEST_WAIT' if not retest else 'CONFIRMATION_WAIT' if not color or not reclaimed else 'RR_WAIT'
    return dict(stage=stage,ready=ready,setup_id=pullback_identity(contract.contract_name,direction,anchor),
                direction=direction,anchor_time_ms=anchor['time_ms'],anchor_level=level,
                entry=entry,stop=stop,target=entry+(2*risk if direction=='LONG' else -2*risk),
                extension_target=extension,room_rr=room,retest=retest,color_ok=color,level_ok=reclaimed)


def replay_pullback(contract, monitor, entries, settings, *, start_ms, end_ms, window=180):
    step=scanner.INTERVAL_MS[settings.entry_interval];step4=scanner.INTERVAL_MS[settings.monitor_interval]
    if start_ms<=0 or end_ms<=start_ms or start_ms%step or end_ms%step or window<1:
        raise ValueError('Invalid pullback replay bounds')
    for rows,interval in ((monitor,settings.monitor_interval),(entries,settings.entry_interval)):
        if any(c.contract_id!=contract.contract_id or c.interval!=interval or c.time_ms%scanner.INTERVAL_MS[interval] for c in rows):
            raise ValueError('Invalid pullback source identity/time')
        if any(a.time_ms>=b.time_ms for a,b in zip(rows,rows[1:])):
            raise ValueError('Pullback sources must be ordered and unique')
    times4=[c.time_ms+step4 for c in monitor];times15=[c.time_ms+step for c in entries]
    scan_start=start_ms-settings.roll_max_age*step4-settings.retest_lookback*step
    anchor=None;seen=set();last4=None;uncertain=0;signals=[];stages=Counter();exclusions=Counter();valid_points=0;warmup=0
    for close_ms in range(scan_start,end_ms,step):
        now=close_ms+1;reporting=close_ms>=start_ms
        n4=bisect_right(times4,now);n15=bisect_right(times15,now)
        m=complete_window(monitor,n4,window,step4,now);e=complete_window(entries,n15,window,step,now)
        if m is None or e is None:
            anchor=None;last4=None;uncertain=now
            if reporting:exclusions['incomplete_indicator_window']+=1
            continue
        if m[-1].time_ms!=last4:
            candidate=discover_anchor(m,settings)
            if candidate is not None:anchor=candidate
            last4=m[-1].time_ms
        row=entry_for_anchor(contract,m,e,anchor,settings)
        if reporting:
            valid_points+=1;stages[row['stage']]+=1
        if not row['ready'] or row['setup_id'] in seen:
            continue
        seen.add(row['setup_id'])
        if anchor['time_ms']+step4<=uncertain:
            if reporting:exclusions['unknown_first_entry']+=1
            continue
        if not reporting:
            warmup+=1
            continue
        signal=dict(key='replay-pullback:'+row['setup_id'],setup_id=row['setup_id'],ticker=contract.contract_name,
                    side=row['direction'],anchor_time_ms=row['anchor_time_ms'],anchor_level=row['anchor_level'],
                    entry=row['entry'],stop=row['stop'],target=row['target'],extension_target=row['extension_target'],
                    room_rr=row['room_rr'],signal_candle_ms=e[-1].time_ms,created_ms=now,dataset='RETROSPECTIVE',model=MODEL)
        prefix,gap,_=consecutive_window(signal,entries,contract,settings.entry_interval,now_ms=end_ms)
        signal['result']=evaluate_paper_signal(signal,prefix,interval_ms=step,now_ms=end_ms)
        signal['outcome_gap_ms']=gap if signal['result']['status'] not in {'TP','SL','AMBIGUOUS'} else None
        signals.append(signal)
    return dict(ticker=contract.contract_name,expected_points=(end_ms-start_ms)//step,valid_points=valid_points,
                excluded_points=dict(exclusions),warmup_first_entries=warmup,stage_counts=dict(stages),signals=signals)


def compare_sources(source_dir, analyze, settings, *, role):
    protocol=json.loads(PROTOCOL_PATH.read_text());period=next((p for p in protocol['periods'] if p['role']==role),None)
    source=json.loads((source_dir/'replay-report.json').read_text())
    if source.get('dataset')!='RETROSPECTIVE' or source.get('eligible_for_live_promotion') is not False or period is None:
        raise ValueError('Invalid pullback research source')
    if (source['start_ms'],source['end_ms'])!=(period['start_ms'],period['end_ms']) or strategy_parameters(settings)!=protocol['parameters']:
        raise ValueError('Research configuration differs from fixed pullback protocol')
    manifest=source['manifest']
    if manifest['rule_fingerprint']!=rule_fingerprint(analyze,settings) or manifest['indicator_windows']!=protocol['windows']:
        raise ValueError('Baseline fingerprint/window mismatch')
    if source['coverage']['requested_markets']!=len(manifest['universe']) or source['coverage']['fetched_markets']!=len(manifest['sources']):
        raise ValueError('Invalid research coverage denominator')
    signals=[];current=[];shadow=[];markets=[];seen_contracts=set()
    allowed={f.name for f in fields(scanner.Contract)}
    for item in manifest['sources']:
        path=(source_dir/item['file']).resolve()
        if not path.is_relative_to(source_dir.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('Invalid research candle path/checksum')
        data=json.loads(path.read_text());contract=scanner.Contract(**{k:v for k,v in data['contract'].items() if k in allowed})
        if contract.contract_id in seen_contracts or item['ticker']!=contract.contract_name:
            raise ValueError('Duplicate or mismatched source contract')
        seen_contracts.add(contract.contract_id)
        m=[scanner.Candle(**c) for c in data[settings.monitor_interval]];e=[scanner.Candle(**c) for c in data[settings.entry_interval]]
        kwargs=dict(start_ms=source['start_ms'],end_ms=source['end_ms'])
        baseline=replay_contract(contract,m,e,analyze,**kwargs,setup_max_age=settings.roll_max_age,retest_lookback=settings.retest_lookback)
        variant=replay_pullback(contract,m,e,settings,**kwargs)
        current.extend(baseline['current']);shadow.extend(baseline['shadow']);signals.extend(variant['signals'])
        markets.append({k:v for k,v in variant.items() if k!='signals'})
    for label,rows in (('current',current),('shadow',shadow)):
        if {s['key']:s for s in rows}!={s['key']:s for s in source['signals'][label]}:
            raise ValueError('Archived baseline did not reproduce: '+label)
    for s in signals:
        identity=pullback_identity(s['ticker'],s['side'],dict(time_ms=s['anchor_time_ms'],level=s['anchor_level']))
        if identity!=s['setup_id']:raise ValueError('Invalid pullback identity')
    if len({s['setup_id'] for s in signals})!=len(signals):raise ValueError('Repeated pullback entry')
    timestamps=lambda rows:{(s['ticker'],s['side'],s['created_ms']) for s in rows}
    v=timestamps(signals);c=timestamps(current);b=timestamps(shadow)
    expected=sum(r['expected_points'] for r in markets);valid=sum(r['valid_points'] for r in markets)
    fingerprint=hashlib.sha256((PROTOCOL_PATH.read_text()+''.join(inspect.getsource(f) for f in (trend,pullback_identity,discover_anchor,entry_for_anchor,replay_pullback,complete_window,consecutive_window,evaluate_paper_signal))).encode()).hexdigest()
    return dict(dataset='RETROSPECTIVE',eligible_for_live_promotion=False,automatic_promotion=False,
                protocol=protocol['protocol'],model=MODEL,role=role,start_ms=source['start_ms'],end_ms=source['end_ms'],
                rule_fingerprint=fingerprint,baseline_fingerprint=manifest['rule_fingerprint'],source_files_verified=len(markets),
                source_manifest_sha256=hashlib.sha256((source_dir/'replay-report.json').read_bytes()).hexdigest(),
                coverage=dict(source['coverage'],pullback_valid_points=valid,pullback_expected_points_all_markets=source['coverage']['expected_points_all_markets'],
                              pullback_coverage_pct=round(valid/source['coverage']['expected_points_all_markets']*100,2)),
                metrics=dict(current=source['comparison']['current'],existing_shadow=source['comparison']['shadow'],pullback=metrics(signals)),
                opportunities=dict(pullback=len(v),additional_vs_current=len(v-c),overlap_current=len(v&c),
                                   additional_vs_existing_shadow=len(v-b),overlap_existing_shadow=len(v&b)),
                markets=markets,signals=signals,limitations=protocol['limitations'])


def decision(periods):
    for p in periods:
        m=p['metrics']['pullback'];pf=m['profit_factor']
        if m['resolved']>=20 and (m['avg_r']<=0 or (pf!='INF' and pf<=1)):
            return 'KILL'
    if any(p['metrics']['pullback']['resolved']<20 for p in periods):return 'CONTINUE_INSUFFICIENT_SAMPLE'
    if any(p['opportunities']['additional_vs_current']==0 for p in periods):return 'PIVOT_NO_INCREMENTAL_OPPORTUNITIES'
    return 'CONTINUE_FORWARD_SHADOW_REQUIRED'


def main():
    import argparse
    from analysis_terminal import server
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--role',choices=[p['role'] for p in json.loads(PROTOCOL_PATH.read_text())['periods']],required=True)
    args=parser.parse_args();report=compare_sources(args.source,server.analyze_contract,server.SETTINGS,role=args.role)
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'pullback-report.json').write_text(json.dumps(report,ensure_ascii=False,allow_nan=False))
    summary={k:v for k,v in report.items() if k not in {'markets','signals'}}
    (args.output/'pullback-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False))
    print('PULLBACK_METRICS='+json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':main()
