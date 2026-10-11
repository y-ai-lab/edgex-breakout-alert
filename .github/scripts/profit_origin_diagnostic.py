"""Frozen retrospective diagnosis of original ledgers, never a trading strategy."""
import argparse
from bisect import bisect_right
from collections import Counter
from dataclasses import fields
import hashlib
import json
import math
from pathlib import Path
from statistics import median

import app as scanner
from analysis_terminal import pending_entry_replay as control
from analysis_terminal import vwap_reclaim_replay as helpers
from analysis_terminal.replay import complete_window

PLAN = Path(__file__).resolve().parents[1] / 'research/profit_origin_plan.json'
PLAN_SHA256 = '57d389a2a7e1f74f29e8da61771223797fc24a75a5961ccaaf061ca9a785c4c4'
STEP, STEP4 = 900000, 14400000


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def plan():
    raw = PLAN.read_bytes()
    if digest(raw) != PLAN_SHA256:
        raise ValueError('Diagnostic plan changed')
    return json.loads(raw)


def label(value):
    return format(value, 'g')


def buckets(spec):
    if 'categories' in spec:
        return spec['categories'] + ['UNKNOWN']
    edges = spec['edges']
    return ([f'lt_{label(edges[0])}'] + [f'{label(a)}_to_{label(b)}' for a, b in zip(edges, edges[1:])]
            + [f'ge_{label(edges[-1])}', 'UNKNOWN'])


def bucket(value, spec):
    names = buckets(spec)
    if 'categories' in spec:
        return value if value in spec['categories'] else 'UNKNOWN'
    if value is None:
        return 'UNKNOWN'
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Invalid feature value')
    return names[bisect_right(spec['edges'], value)]


def features(row, monitor, entries):
    """Use closed signal-time inputs. Ignore future highs/lows, fill and exits."""
    close = row['signal_candle_ms'] + STEP
    if row['created_ms'] != close+1 or row['side'] not in {'LONG', 'SHORT'}:
        raise ValueError('Invalid original signal clock/direction')
    w4 = complete_window(monitor, bisect_right([c.time_ms+STEP4 for c in monitor], close+1), 180, STEP4, close+1)
    w15 = complete_window(entries, bisect_right([c.time_ms+STEP for c in entries], close+1), 180, STEP, close+1)
    if w4 is None or w15 is None or w15[-1].time_ms != row['signal_candle_ms']:
        raise ValueError('Signal-time indicator input missing')
    helpers.closed_clock(w4, w15)
    if any(c.contract_id != w15[-1].contract_id for c in w4+w15):
        raise ValueError('Mixed market features')
    if any(isinstance(c.volume, bool) or not isinstance(c.volume, (int, float)) or
            not math.isfinite(c.volume) or c.volume < 0 for c in w15):
        raise ValueError('Invalid base volume')
    d = 1 if row['side'] == 'LONG' else -1
    fast, slow = scanner._ema([c.close for c in w4], 20), scanner._ema([c.close for c in w4], 50)
    atr4, atr15 = scanner._atr(w4, 14), scanner._atr(w15, 14)
    if not all(x is not None and math.isfinite(x) and x > 0 for x in (fast, slow, atr4, atr15, row['trigger'], row['stop'])):
        raise ValueError('Invalid fixed prices/indicator')
    gross_risk = d*(row['trigger']-row['stop'])
    risk = control.cost_levels(row['trigger'], row['stop'], row['target'], row['side'])['net_risk']
    if gross_risk <= 0 or risk <= 0:
        raise ValueError('Invalid original stop side')
    signal = w15[-1]
    mean_volume = sum(c.volume for c in w15[-21:-1])/20
    span = signal.high-signal.low
    return dict(trend_separation_atr4=d*(fast-slow)/atr4,
                entry_extension_atr4=d*(signal.close-fast)/atr4,
                stop_width_atr15=gross_risk/atr15,
                relative_volume=signal.volume/mean_volume if mean_volume > 0 else None,
                directional_close_location=((signal.close-signal.low) if d == 1 else (signal.high-signal.close))/span if span > 0 else None,
                friction_share_of_net_stop_risk=(risk-gross_risk)/risk, direction=row['side'])


def describe(rows):
    m = control.metrics(rows)
    s = helpers.stressed_metrics(rows)
    return dict(candidates=m['candidates'], filled=m['filled'], resolved=m['resolved'],
                tp=m['tp'], sl=m['sl'], statuses=m['statuses'], tp_rate_pct=100*m['tp']/m['resolved'] if m['resolved'] else None,
                net_win_rate_pct=m['win_rate'], avg_net_r=m['avg_net_r'], profit_factor=m['profit_factor'],
                stress_avg_net_r=s['avg_net_r'], stress_profit_factor=s['profit_factor'],
                avg_mfe_price_r=m['avg_mfe_r'], avg_mae_price_r=m['avg_mae_r'],
                max_consecutive_losses=m['max_consecutive_losses'],
                cohort_complete=not any(m['statuses'].get(x, 0) for x in ('OPEN','AMBIGUOUS','DATA_GAP','PENDING')),
                sample_status=m['sample_status'], portfolio_roi_pct=None,
                interpretation='EXPLORATORY_SUBGROUP_NOT_VALIDATED_STRATEGY')


def summarize(rows, vectors, specs):
    if len({r['key'] for r in rows}) != len(rows) or len(rows) != len(vectors):
        raise ValueError('Duplicate/truncated feature ledger')
    results = {}
    for name, spec in specs.items():
        groups = {b: [] for b in buckets(spec)}
        for r, v in zip(rows, vectors):
            groups[bucket(v[name], spec)].append(r)
        if sum(len(x) for x in groups.values()) != len(rows):
            raise ValueError('Feature partition does not conserve candidates')
        medians = {}
        for state, sign in [('NET_WIN', 1), ('NET_LOSS', -1)]:
            values = [v[name] for r, v in zip(rows, vectors) if r['status'] in {'TP', 'SL'}
                      and sign*r['final_net_r'] > 0 and v[name] is not None]
            medians[state] = dict(count=len(values), median=median(values) if values and 'categories' not in spec else None)
        results[name] = dict(bins={b: describe(rs) for b, rs in groups.items()}, resolved_outcome_medians=medians)
    return results


def run(report_paths):
    p = plan()
    if len(report_paths) != len(p['sources']):
        raise ValueError('All fixed diagnostic ledgers required')
    output, feature_ledgers = [], []
    for spec, path in zip(p['sources'], report_paths):
        raw = Path(path).read_bytes()
        if digest(raw) != spec['report_sha256']:
            raise ValueError('Original ledger changed')
        r = json.loads(raw)
        if (r['start_ms'],r['end_ms']) != (p['start_ms'],p['end_ms']) or r['pristine_holdout'] is not False:
            raise ValueError('Different original period/scope')
        groups = {m: [x for x in r['records'] if x['model'] == m] for m in r['metrics']}
        for m, rs in groups.items():
            if control.metrics(rs) != r['metrics'][m] or helpers.stressed_metrics(rs) != r['stress_metrics'][m] or control.portfolio(rs) != r['portfolios'][m]:
                raise ValueError('Original metrics/portfolio does not reproduce')
        source_dir = Path(r['source_dir']).resolve()
        original = (source_dir/'replay-report.json').read_bytes()
        if digest(original) != p['source_report_sha256'] or r['source_report_sha256'] != digest(original):
            raise ValueError('Original market source changed')
        source = json.loads(original)
        sources, hashes = {}, []
        for item in source['manifest']['sources']:
            f = (source_dir/item['file']).resolve()
            if not f.is_relative_to(source_dir):
                raise ValueError('Unsafe original market path')
            candles = f.read_bytes()
            if digest(candles) != item['sha256']:
                raise ValueError('Original candle checksum changed')
            if item['ticker'] in sources:
                raise ValueError('Duplicate market')
            data = json.loads(candles)
            sources[item['ticker']] = data
            hashes.append((f,item['sha256']))
        rows = groups[spec['model']]
        vectors = []
        for row in rows:
            if not p['start_ms'] <= row['created_ms']-1 < p['end_ms']:
                raise ValueError('Outside original fixed period')
            data = sources[row['ticker']]
            if data['contract']['contract_name'] != row['ticker']:
                raise ValueError('Wrong original market')
            monitor = [scanner.Candle(**x) for x in data['HOUR_4']]
            entries = [scanner.Candle(**x) for x in data['MINUTE_15']]
            vectors.append(features(row, monitor, entries))
        if digest(Path(path).read_bytes()) != spec['report_sha256'] or digest((source_dir/'replay-report.json').read_bytes()) != p['source_report_sha256'] or any(digest(f.read_bytes()) != h for f,h in hashes):
            raise ValueError('Original evidence mutated')
        output.append(dict(id=spec['id'], model=spec['model'], original_report_sha256=spec['report_sha256'],
                           source_files_verified=len(sources), coverage=r['coverage'],
                           original_metrics_portfolios_reproduced=True, baseline=describe(rows),
                           original_portfolio=r['portfolios'][spec['model']],
                           features=summarize(rows, vectors, p['features'])))
        feature_ledgers.append(dict(id=spec['id'], rows=[dict(key=x['key'], features=v) for x,v in zip(rows,vectors)]))
    result = dict(plan=p['id'], plan_sha256=PLAN_SHA256, start_ms=p['start_ms'],end_ms=p['end_ms'],
                  scope=p['scope'], models=output, pooled_metrics=None, new_resolved_samples=0,
                  changes_live_rules=False, eligible_for_live_promotion=False, real_orders_enabled=False,
                  interpretation='Exploration only; all source models remain KILL. No subgroup is validated or automatically promoted.',
                  limitations=p['limitations'])
    return result, feature_ledgers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', action='append', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    result, ledgers = run(args.report)
    for name, value in [('summary.json',result),('feature-ledgers.json',ledgers)]:
        with (args.output/name).open('x') as f:
            json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps(dict(models=len(result['models']),source_models_still_killed=True,new_resolved_samples=0)))


if __name__ == '__main__':
    main()
