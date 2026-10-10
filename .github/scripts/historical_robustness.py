"""Offline, preregistered extra-period replay of existing frozen specifications.

No network, database, trading controls, order transport or parameter optimization.
Public source archives are collected separately after Git preregistration.
"""
import argparse
from dataclasses import fields
import hashlib
import json
from pathlib import Path

import app as scanner
from analysis_terminal import pending_entry_replay as study
from analysis_terminal import execution_funnel
from analysis_terminal.replay import rule_fingerprint, strategy_parameters
from analysis_terminal.vwap_reclaim_replay import stressed_metrics

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / '.github/research/historical_robustness_protocol.json'
PINNED_PROTOCOL_SHA256 = '5f92a471c1aecb302c7e610b0f947e6e1737c5f12e78aa2ea1f482e5650ec667'
WEEK = 7 * 86400000


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def protocol():
    raw = PROTOCOL.read_bytes()
    if digest(raw) != PINNED_PROTOCOL_SHA256:
        raise ValueError('Preregistered protocol changed')
    return json.loads(raw)


def run(source_dir, period_index, analyze, settings):
    p = protocol()
    if type(period_index) is not int or not 0 <= period_index < len(p['periods']):
        raise ValueError('Unregistered period')
    period = p['periods'][period_index]
    start, end = period['start_ms'], period['end_ms']
    if end - start != WEEK or start % study.STEP or end % study.STEP:
        raise ValueError('Not a complete registered interval')
    for sealed in p['sealed_periods']:
        if start < sealed['end_ms'] and sealed['start_ms'] < end:
            raise ValueError('Reserved evaluation interval')
    for name, expected in p['frozen_dependencies_sha256'].items():
        if digest((ROOT / name).read_bytes()) != expected:
            raise ValueError('Frozen dependency changed')
    if (strategy_parameters(settings) != p['production_parameters']
            or rule_fingerprint(analyze, settings) != p['production_rule_fingerprint']):
        raise ValueError('Production rule changed')
    source_dir = Path(source_dir).resolve()
    source_raw = (source_dir / 'replay-report.json').read_bytes()
    source = json.loads(source_raw)
    manifest = source['manifest']
    if (source.get('dataset') != 'RETROSPECTIVE'
            or source.get('eligible_for_live_promotion') is not False
            or (source['start_ms'], source['end_ms']) != (start, end)
            or manifest['parameters'] != p['production_parameters']
            or manifest['rule_fingerprint'] != p['production_rule_fingerprint']
            or manifest['indicator_windows'] != p['indicator_windows']
            or digest(canonical(manifest['universe'])) != p['source_universe_sha256']):
        raise ValueError('Source differs from preregistration')
    universe = {c['contract_name']: c for c in manifest['universe']}
    observed = [x['ticker'] for x in manifest['sources'] + manifest['failures']]
    if (len(universe) != len(manifest['universe']) or len(set(observed)) != len(observed)
            or set(observed) != set(universe)):
        raise ValueError('Missing or duplicate market inventory')
    records, markets, hashes = [], [], []
    for item in manifest['sources']:
        path = (source_dir / item['file']).resolve()
        if not path.is_relative_to(source_dir):
            raise ValueError('Source path outside archive')
        raw = path.read_bytes()
        if digest(raw) != item['sha256']:
            raise ValueError('Source checksum mismatch')
        hashes.append((path, digest(raw)))
        data = json.loads(raw)
        if data['contract'] != universe[item['ticker']]:
            raise ValueError('Contract inventory mismatch')
        allowed = {f.name for f in fields(scanner.Contract)}
        contract = scanner.Contract(**{k:v for k,v in data['contract'].items() if k in allowed})
        result = study.replay_market(contract,
            [scanner.Candle(**c) for c in data['HOUR_4']],
            [scanner.Candle(**c) for c in data['MINUTE_15']], analyze, settings,
            start_ms=start, end_ms=end)
        records.extend(result.pop('records'))
        markets.append(result)
    if len({r['key'] for r in records}) != len(records):
        raise ValueError('Duplicate setup ledger')
    groups = {m:[r for r in records if r['model'] == m] for m in study.MODELS}
    for model, archived_model in zip(study.MODELS[:2], ('current', 'shadow')):
        actual = {(r['setup_id'],r['created_ms'],r['trigger'],r['stop'],r['target'])
                  for r in groups[model]}
        archived = {(r['setup_id'],r['created_ms'],r['entry'],r['stop'],r['target'])
                    for r in source['signals'][archived_model]}
        if actual != archived or len(actual) != len(groups[model]):
            raise ValueError('Independent first-entry comparator mismatch')
    for r in records:
        if not (start <= r['created_ms']-1 < end
                and r['created_ms'] == r['signal_candle_ms']+study.STEP+1):
            raise ValueError('Entry outside registered chronology')
        if r['filled_ms'] is not None and not r['created_ms']-1 <= r['filled_ms'] < r['expires_ms']:
            raise ValueError('Fill outside frozen execution interval')
    coverage = dict(source['coverage'])
    if (coverage['valid_points'] != sum(m['valid_points'] for m in markets)
            or coverage['expected_points_all_markets'] != len(universe)*WEEK//study.STEP
            or coverage['failed_markets'] != len(manifest['failures'])
            or coverage['fetched_markets'] != len(markets)):
        raise ValueError('Coverage does not reproduce')
    report = dict(protocol=p['protocol'],dataset='RETROSPECTIVE',role=period['id'],
        start_ms=start,end_ms=end,period_complete=True,
        eligible_for_live_promotion=False,automatic_promotion=False,real_orders_enabled=False,
        changes_live_rules=False,pristine_holdout=False,
        protocol_sha256=digest(study.PROTOCOL.read_bytes()),
        audit_protocol_sha256=digest(PROTOCOL.read_bytes()),
        source_report_sha256=digest(source_raw),
        engine_sha256=digest((ROOT/'analysis_terminal/pending_entry_replay.py').read_bytes()),
        baseline_first_entries_reproduced=True,source_files_verified=len(markets),coverage=coverage,
        metrics={m:study.metrics(rows) for m,rows in groups.items()},
        stress_metrics={m:stressed_metrics(rows) for m,rows in groups.items()},
        portfolios={m:study.portfolio(rows) for m,rows in groups.items()},
        markets=markets,records=records,limitations=p['limitations'])
    report['execution_funnel'] = execution_funnel.summarize(report)
    if (digest((source_dir/'replay-report.json').read_bytes()) != digest(source_raw)
            or any(digest(path.read_bytes()) != h for path,h in hashes)):
        raise ValueError('Source mutated during evaluation')
    return report


def decision(reports):
    """Period-specific selection; never pool insufficient periods or auto-promote."""
    p = protocol()
    expected = {r['id']:(r['start_ms'],r['end_ms']) for r in p['periods']}
    seen = set()
    for r in reports:
        if (r['role'] not in expected or r['role'] in seen
                or (r['start_ms'],r['end_ms']) != expected[r['role']]
                or r['audit_protocol_sha256'] != digest(PROTOCOL.read_bytes())):
            raise ValueError('Unknown, duplicate or modified period')
        seen.add(r['role'])
    if any(r['coverage']['failed_markets'] or not r['coverage']['valid_points'] for r in reports):
        return 'BLOCKED_DATA_QUALITY'
    for r in reports:
        m = r['metrics'][study.MODEL]
        if r['period_complete'] and m['resolved'] >= 20 and (m['avg_net_r'] <= 0 or m['profit_factor'] != 'INF'
                                    and m['profit_factor'] <= 1):
            return 'REJECTED_HISTORICAL_ROBUSTNESS_NO_LIVE_CHANGE'
    if set(expected) != seen or any(not r['period_complete']
            or r['metrics'][study.MODEL]['resolved'] < 50 for r in reports):
        return 'CONTINUE_INSUFFICIENT_SAMPLE'
    for r in reports:
        m,s = r['metrics'][study.MODEL],r['stress_metrics'][study.MODEL]
        account = r['portfolios'][study.MODEL]
        comparison = r['execution_funnel']['proposal_vs_current']
        if not (m['win_rate'] >= 40 and m['avg_net_r'] > 0
                and (m['profit_factor'] == 'INF' or m['profit_factor'] >= 1.2)
                and s['avg_net_r'] > 0 and (s['profit_factor'] == 'INF' or s['profit_factor'] > 1)
                and comparison['net_filled_count_difference'] > 0
                and comparison['capped_filled_count_difference'] > 0
                and account['resolved'] >= 20 and not account['active'] and not account['uncertain']
                and account['closed_portfolio_roi_pct'] is not None
                and account['closed_portfolio_roi_pct'] > 0):
            return 'NO_QUALIFIED_REPLACEMENT'
    return 'RETROSPECTIVE_CRITERIA_MET_SEPARATE_LIVE_VALIDATION_REQUIRED'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--period-index', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from analysis_terminal import server
    report = run(args.source,args.period_index,server.analyze_contract,server.SETTINGS)
    args.output.mkdir(parents=True, exist_ok=False)
    for name,value in [('report.json',report),('execution-funnel.json',report['execution_funnel'])]:
        with (args.output/name).open('x') as f: json.dump(value,f,ensure_ascii=False,allow_nan=False)
    summary = {k:v for k,v in report.items() if k not in {'records','markets','execution_funnel'}}
    summary['comparison'] = report['execution_funnel']['proposal_vs_current']
    with (args.output/'summary.json').open('x') as f:
        json.dump(summary,f,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({'period':report['role'],'decision':decision([report]),
                      'metrics':report['metrics'],'comparison':summary['comparison']}))


if __name__ == '__main__':
    main()
