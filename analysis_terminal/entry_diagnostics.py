"""Descriptive entry-time diagnostics, isolated from live strategies and data."""
from bisect import bisect_right
from collections import defaultdict
from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from statistics import median

import app as scanner
from analysis_terminal.comparison import cohort, metrics, resolved_result
from analysis_terminal.replay import complete_window, rule_fingerprint
from analysis_terminal.setups import setup_identity


BINS = {
    'confirmation_body_atr': (0.5, 1),
    'confirmation_roll_margin_atr': (0.5, 1, 2),
    'trend_spread_atr_4h': (0.5, 1, 2),
    'stop_distance_atr_4h': (0.5, 1, 2),
    'room_rr': (3, 4),
    'volume_ratio': (0.5, 1, 2),
}
CATEGORIES = ('side', 'retest_chronology', 'breakout_age_bars', 'entry_date_utc')


def entry_features(signal, contract, monitor, entries, analyze, settings, windows):
    """Rebuild an original entry; outcomes cannot affect any returned feature."""
    as_of = int(signal['created_ms'])
    step4, step15 = scanner.INTERVAL_MS[settings.monitor_interval], scanner.INTERVAL_MS[settings.entry_interval]
    if as_of != int(signal['signal_candle_ms']) + step15 + 1 or (as_of-1) % step15:
        raise ValueError('Entry clock is not signal close plus 1ms')
    for candles, interval in ((monitor, settings.monitor_interval), (entries, settings.entry_interval)):
        if any(c.contract_id != contract.contract_id or c.interval != interval for c in candles):
            raise ValueError('Candle identity mismatch')
        if any(a.time_ms >= b.time_ms for a, b in zip(candles, candles[1:])):
            raise ValueError('Unordered or duplicate candles')
    n4 = bisect_right([c.time_ms+step4 for c in monitor], as_of)
    n15 = bisect_right([c.time_ms+step15 for c in entries], as_of)
    m = complete_window(monitor, n4, windows[settings.monitor_interval], step4, as_of)
    e = complete_window(entries, n15, windows[settings.entry_interval], step15, as_of)
    if m is None or e is None:
        raise ValueError('Incomplete entry indicator window')
    row = analyze(contract, m, e, as_of_ms=as_of)
    if (row.get('shadow_v2_ready') is not True or setup_identity(row) != signal['setup_id']
            or row.get('direction') != signal['side'] or contract.contract_name != signal['ticker']):
        raise ValueError('Original Shadow entry does not reproduce')
    for stored, feature in (('entry', 'entry_reference'), ('stop', 'shadow_stop_loss'),
                            ('target', 'shadow_v2_target'), ('room_rr', 'shadow_v2_room_rr'),
                            ('extension_target', 'shadow_v2_extension_target')):
        if not math.isclose(float(signal[stored]), float(row[feature]), rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError('Original entry prices do not reproduce')
    atr4 = float(row['atr_4h'])
    if atr4 <= 0:
        raise ValueError('Invalid entry ATR')
    level = row['breakout_level']
    tolerance = max(row['atr_15m']*settings.retest_atr_tolerance, level*0.001)
    touched = [c for c in e[-settings.retest_lookback:]
               if (c.low <= level+tolerance if signal['side']=='LONG' else c.high >= level-tolerance)]
    breakout_close = int(row['breakout_time_ms'])+step4
    after = [c for c in touched if c.time_ms >= breakout_close]
    prior = [c for c in after if c.time_ms < e[-1].time_ms]
    chronology = ('PRIOR_CANDLE_AFTER_BREAKOUT' if prior else
                  'ONLY_CONFIRMATION_CANDLE_AFTER_BREAKOUT' if after else 'ONLY_BEFORE_BREAKOUT_CLOSE')
    result = dict(side=signal['side'], retest_chronology=chronology,
                  breakout_age_bars=(as_of-1-breakout_close)//step4,
                  entry_date_utc=datetime.fromtimestamp((as_of-1)/1000,timezone.utc).date().isoformat(),
                  confirmation_body_atr=row['confirmation_body_atr'],
                  confirmation_roll_margin_atr=row['confirmation_roll_margin_atr'],
                  trend_spread_atr_4h=abs(row['ema20_4h']-row['ema50_4h'])/atr4,
                  stop_distance_atr_4h=abs(signal['entry']-signal['stop'])/atr4,
                  room_rr=row['shadow_v2_room_rr'], volume_ratio=row['volume_ratio'],
                  breakout_close_ms=breakout_close, retest_candle_ms=[c.time_ms for c in touched],
                  post_breakout_retest_candle_ms=[c.time_ms for c in after])
    if any(not math.isfinite(float(result[k])) for k in BINS):
        raise ValueError('Nonfinite diagnostic feature')
    return result


def bucket(value, boundaries):
    for i, upper in enumerate(boundaries):
        if value < upper:
            return f'<{upper:g}' if i==0 else f'[{boundaries[i-1]:g},{upper:g})'
    return f'>={boundaries[-1]:g}'


def diagnostic_summary(records):
    """Report all predeclared groups; OPEN remains visible in every denominator."""
    strata = {}
    for name in (*BINS, *CATEGORIES):
        groups = defaultdict(list)
        for s in records:
            value = s['entry_features'][name]
            label = bucket(value, BINS[name]) if name in BINS else str(value)
            groups[label].append(s)
        if name in BINS:
            bounds=BINS[name]
            labels=[f'<{bounds[0]:g}',*(f'[{a:g},{b:g})' for a,b in zip(bounds,bounds[1:])),f'>={bounds[-1]:g}']
        else:
            labels=sorted(groups)
        strata[name] = [dict(group=k, **metrics(groups[k])) for k in labels]
    distributions = {}
    for status in ('TP','SL','OPEN','AMBIGUOUS'):
        selected = [s for s in records if (s.get('result') or {}).get('status','OPEN')==status
                    and (status not in ('TP','SL') or resolved_result(s))]
        distributions[status] = dict(signals=len(selected), features={
            name: dict(samples=len(selected), median=round(median(s['entry_features'][name] for s in selected),4)
                       if selected else None) for name in BINS})
    return dict(metrics=metrics(records), strata=strata, feature_distributions=distributions,
                dependence=dict(unique_tickers=len({s['ticker'] for s in records}),
                                breakout_close_times=len({s['entry_features']['breakout_close_ms'] for s in records})))


def build_diagnostics(source_dir: Path, analyze, settings):
    source = json.loads((source_dir/'replay-report.json').read_text())
    if source.get('dataset')!='RETROSPECTIVE' or source.get('eligible_for_live_promotion') is not False:
        raise ValueError('Invalid research dataset')
    manifest = source['manifest']
    if rule_fingerprint(analyze, settings)!=manifest['rule_fingerprint']:
        raise ValueError('Research analyzer fingerprint mismatch')
    signals, excluded = cohort(source['signals']['shadow'])
    if any(excluded[k] for k in ('legacy_unidentified','invalid_identity','duplicates')):
        raise ValueError('Original research cohort is not unique and identified')
    sources = {}
    for item in manifest['sources']:
        path = (source_dir/item['file']).resolve()
        if not path.is_relative_to(source_dir.resolve()):
            raise ValueError('Research source path outside artifact')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:
            raise ValueError('Research candle checksum mismatch')
        sources[item['ticker']] = json.loads(raw)
    records = []
    allowed = {f.name for f in fields(scanner.Contract)}
    for signal in signals:
        data = sources[signal['ticker']]
        contract = scanner.Contract(**{k:v for k,v in data['contract'].items() if k in allowed})
        monitor = [scanner.Candle(**c) for c in data[settings.monitor_interval]]
        entries = [scanner.Candle(**c) for c in data[settings.entry_interval]]
        features = entry_features(signal, contract, monitor, entries, analyze, settings, manifest['indicator_windows'])
        records.append(dict(signal, entry_features=features))
    summary = diagnostic_summary(records)
    if summary['metrics'] != source['comparison']['shadow']:
        raise ValueError('Reconstructed cohort metrics differ from research')
    return dict(dataset='RETROSPECTIVE', eligible_for_live_promotion=False,
                protocol='entry_loss_diagnostics_v1', start_ms=source['start_ms'], end_ms=source['end_ms'],
                bins=BINS, categories=CATEGORIES, source_rule_fingerprint=manifest['rule_fingerprint'],
                verified_source_files=len(sources), reconstructed_entries=len(records), summary=summary,
                records=records,
                limitations=['Descriptive, post-outcome analysis on the previously inspected 7-day dataset; no out-of-sample test.',
                             'Fixed bins are diagnostic groups, not proposed strategy thresholds.',
                             'OPEN entries are right-censored and excluded only from resolved metrics.',
                             'Subgroups are correlated, share market conditions, and may have fewer than 20 resolved samples.',
                             'Chronology uses OHLC candle bounds; intrabar order is unknown.',
                             'A subgroup comparison is not a replay of a new rule: rejecting a first entry may allow a later entry.',
                             'No live promotion, strategy modification, notification or order execution.'])


def main():
    import argparse
    from analysis_terminal import server
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    report = build_diagnostics(args.source,server.analyze_contract,server.SETTINGS)
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'entry-diagnostics.json').write_text(json.dumps(report,ensure_ascii=False,allow_nan=False))
    summary = {k:v for k,v in report.items() if k!='records'}
    (args.output/'entry-diagnostics-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':
    main()
