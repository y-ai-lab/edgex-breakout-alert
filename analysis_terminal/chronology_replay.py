"""Research-only first-entry replay of post-breakout retest; no live hooks."""
from dataclasses import fields
import hashlib
import inspect
import json
from pathlib import Path

import app as scanner
from analysis_terminal.comparison import strategy_comparison
from analysis_terminal.replay import replay_contract, rule_fingerprint

BASELINE_MODEL = 'measured_room_fixed_2r'
VARIANT_MODEL = 'measured_room_fixed_2r_post_breakout_retest'
PERIODS = {
    'EXPLORATORY': (1790391600000,1790996400000),
    'UNINSPECTED_RETROSPECTIVE': (1789786800000,1790391600000),
}


def post_breakout_retest(row, entries, settings):
    """A matching 15M candle must start after the selected 4H bar closes.

    Keep the same lookback/tolerance; the confirmation candle may be the retest.
    """
    if not row.get('breakout_time_ms') or not row.get('breakout_level') or not entries:
        return False
    boundary = int(row['breakout_time_ms'])+scanner.INTERVAL_MS[settings.monitor_interval]
    level = float(row['breakout_level'])
    tolerance = max(float(row['atr_15m'])*settings.retest_atr_tolerance,level*0.001)
    candidates = [c for c in entries[-settings.retest_lookback:] if c.time_ms >= boundary]
    if row.get('direction')=='LONG':
        return any(c.low <= level+tolerance for c in candidates)
    if row.get('direction')=='SHORT':
        return any(c.high >= level-tolerance for c in candidates)
    return False


def variant_analyzer(analyze, settings):
    def historical_analyze(contract, monitor, entries, *, as_of_ms):
        row = analyze(contract,monitor,entries,as_of_ms=as_of_ms)
        eligible = row.get('shadow_v2_ready') is True and post_breakout_retest(row,entries,settings)
        return dict(row,shadow_v2_ready=eligible)
    return historical_analyze


def replay_comparison(source_dir: Path, analyze, settings, *, role: str):
    source = json.loads((source_dir/'replay-report.json').read_text())
    if source.get('dataset')!='RETROSPECTIVE' or source.get('eligible_for_live_promotion') is not False:
        raise ValueError('Invalid research dataset')
    if role not in PERIODS or (source['start_ms'],source['end_ms'])!=PERIODS[role]:
        raise ValueError('Period differs from fixed research protocol')
    manifest = source['manifest']
    baseline_fingerprint = rule_fingerprint(analyze,settings)
    if baseline_fingerprint != manifest['rule_fingerprint']:
        raise ValueError('Original analysis fingerprint mismatch')
    adapter = variant_analyzer(analyze,settings)
    baseline,variant,current = [],[],[]
    markets = []
    allowed = {f.name for f in fields(scanner.Contract)}
    for item in manifest['sources']:
        path = (source_dir/item['file']).resolve()
        if not path.is_relative_to(source_dir.resolve()):
            raise ValueError('Source path outside research artifact')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:
            raise ValueError('Research candle checksum mismatch')
        data=json.loads(raw)
        contract=scanner.Contract(**{k:v for k,v in data['contract'].items() if k in allowed})
        monitor=[scanner.Candle(**c) for c in data[settings.monitor_interval]]
        entries=[scanner.Candle(**c) for c in data[settings.entry_interval]]
        kwargs=dict(start_ms=source['start_ms'],end_ms=source['end_ms'],monitor_interval=settings.monitor_interval,
                    entry_interval=settings.entry_interval,monitor_window=manifest['indicator_windows'][settings.monitor_interval],
                    entry_window=manifest['indicator_windows'][settings.entry_interval],setup_max_age=settings.roll_max_age,
                    retest_lookback=settings.retest_lookback)
        original = replay_contract(contract,monitor,entries,analyze,**kwargs)
        revised = replay_contract(contract,monitor,entries,adapter,**kwargs)
        if original['current']!=revised['current']:
            raise ValueError('Variant unexpectedly changed current strategy')
        current.extend(original['current']);baseline.extend(original['shadow'])
        for signal in revised['shadow']:
            signal['model']=VARIANT_MODEL
            signal['key']='replay-post-breakout:'+signal['setup_id']
            variant.append(signal)
        markets.append(dict(ticker=contract.contract_name,
                            baseline={k:v for k,v in original.items() if k not in {'current','shadow','stage_counts'}},
                            variant={k:v for k,v in revised.items() if k not in {'current','shadow','stage_counts'}}))
    original = {s['key']:s for s in source['signals']['shadow']}
    if {s['key']:s for s in baseline}!=original:
        raise ValueError('Archived baseline entries or outcomes do not reproduce')
    if {s['key']:s for s in current}!={s['key']:s for s in source['signals']['current']}:
        raise ValueError('Archived current entries or outcomes do not reproduce')
    comparison = strategy_comparison(baseline,variant,limit=50)
    if comparison['current']!=source['comparison']['shadow']:
        raise ValueError('Archived baseline cohort changed')
    b={s['setup_id']:s for s in baseline};v={s['setup_id']:s for s in variant}
    both=b.keys()&v.keys()
    delayed=[k for k in both if v[k]['created_ms']>b[k]['created_ms']]
    if any(v[k]['created_ms']<b[k]['created_ms'] for k in both):
        raise ValueError('Stricter variant entered before baseline')
    fingerprint=hashlib.sha256((baseline_fingerprint+inspect.getsource(post_breakout_retest)+
                                inspect.getsource(variant_analyzer)).encode()).hexdigest()
    return dict(dataset='RETROSPECTIVE',eligible_for_live_promotion=False,
                protocol='post_breakout_retest_comparison_v1',role=role,
                models=dict(current=BASELINE_MODEL,shadow=VARIANT_MODEL),
                start_ms=source['start_ms'],end_ms=source['end_ms'],coverage=source['coverage'],
                baseline_rule_fingerprint=baseline_fingerprint,variant_rule_fingerprint=fingerprint,
                source_files_verified=len(manifest['sources']),comparison=comparison,
                entry_changes=dict(both=len(both),same_time=len(both)-len(delayed),delayed=len(delayed),
                                   removed=len(b.keys()-v.keys()),new_to_reporting_period=len(v.keys()-b.keys())),
                markets=markets,signals=dict(baseline=baseline,variant=variant),
                limitations=source['limitations']+[
                    'This is a full first-entry replay, not filtering the old trades after their outcomes.',
                    'The uninspected period is a backward historical validation using today\'s universe, not a forward/live test.',
                    'No confidence or independence claims; paired entry timing and price may differ.',
                    'No retrospective samples enter the live promotion gate.'])


def main():
    import argparse
    from analysis_terminal import server
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--role',choices=PERIODS,required=True)
    args=parser.parse_args()
    report=replay_comparison(args.source,server.analyze_contract,server.SETTINGS,role=args.role)
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'chronology-report.json').write_text(json.dumps(report,ensure_ascii=False,allow_nan=False))
    summary={k:v for k,v in report.items() if k not in {'signals','markets'}}
    (args.output/'chronology-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print('CHRONOLOGY_METRICS='+json.dumps(dict(role=report['role'],coverage=report['coverage'],
                                             entry_changes=report['entry_changes'],models=report['models'],
                                             baseline=report['comparison']['current'],variant=report['comparison']['shadow'])))


if __name__=='__main__':
    main()
