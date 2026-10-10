"""Offline preregistered 28-day audit of the unchanged VWAP specification."""
import argparse
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import time

import app as scanner
from analysis_terminal import pending_entry_replay as control
from analysis_terminal import vwap_reclaim_replay as vwap
from analysis_terminal.replay import rule_fingerprint, strategy_parameters

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / '.github/research/vwap_long_protocol.json'
PINNED_PROTOCOL_SHA256 = '2a19d43228ed6b999aae532548892f569fb05196d4d1690d8700cba5141488b9'
DAY = 86400000


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def protocol():
    raw = PROTOCOL.read_bytes()
    if digest(raw) != PINNED_PROTOCOL_SHA256:
        raise ValueError('Registered long audit changed')
    p = json.loads(raw)
    for name, expected in p['frozen_dependencies_sha256'].items():
        if digest((ROOT / name).read_bytes()) != expected:
            raise ValueError('Frozen dependency changed')
    app = (ROOT / 'app.py').read_bytes()
    if not (digest(app) == p['app_sha256'] or digest(app) == p['upstream_app_sha256']
            and digest(app + b'\n') == p['app_sha256']):
        raise ValueError('Frozen scanner changed')
    return p


def comparison(groups, accounts):
    # Different setup families: shared timestamp is an event, not a shared lifecycle.
    current = {(r['ticker'], r['side'], r['created_ms']) for r in groups[control.MODELS[0]]
               if r['filled_ms'] is not None}
    proposal = {(r['ticker'], r['side'], r['created_ms']) for r in groups[vwap.MODEL]
                if r['filled_ms'] is not None}
    return dict(identity_basis='ticker + direction + signal close time',
                shared_filled_setups=len(current & proposal),
                proposal_only_filled_setups=len(proposal - current),
                current_filled_missing_in_proposal=len(current - proposal),
                net_filled_count_difference=len(proposal) - len(current),
                capped_filled_count_difference=accounts[vwap.MODEL]['filled']
                    - accounts[control.MODELS[0]]['filled'], capped_shared_setup_count=None)


def run(source_dir, period_index, analyze, settings, *, now_ms=None):
    p = protocol()
    if type(period_index) is not int or not 0 <= period_index < len(p['periods']):
        raise ValueError('Unregistered long period')
    period = p['periods'][period_index]
    start, end = period['start_ms'], period['end_ms']
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if end - start != p['period_days'] * DAY or start % control.STEP or end > now_ms:
        raise ValueError('Not a complete registered long period')
    if any(start < s['end_ms'] and s['start_ms'] < end for s in p['sealed_periods']):
        raise ValueError('Reserved period')
    if strategy_parameters(settings) != p['production_parameters'] or rule_fingerprint(analyze, settings) != p['production_rule_fingerprint']:
        raise ValueError('Frozen analyzer changed')
    source_dir = Path(source_dir).resolve()
    raw = (source_dir / 'replay-report.json').read_bytes()
    seal = json.loads((source_dir.parent / 'source-seal.json').read_text())
    source = json.loads(raw)
    manifest = source['manifest']
    if (seal['source_report_sha256'] != digest(raw) or source['dataset'] != 'RETROSPECTIVE'
            or source['eligible_for_live_promotion'] is not False
            or (source['start_ms'], source['end_ms']) != (start, end)
            or manifest['parameters'] != p['production_parameters']
            or manifest['rule_fingerprint'] != p['production_rule_fingerprint']
            or manifest['indicator_windows'] != p['indicator_windows']
            or digest(canonical(manifest['universe'])) != p['source_universe_sha256']):
        raise ValueError('Source does not match registered period or initial seal')
    universe = {x['contract_name']: x for x in manifest['universe']}
    inventory = [x['ticker'] for x in manifest['sources'] + manifest['failures']]
    if (len(universe) != len(manifest['universe']) or len(set(inventory)) != len(inventory)
            or set(inventory) != set(universe)
            or seal['files'] != {x['file']: x['sha256'] for x in manifest['sources']}):
        raise ValueError('Missing or duplicate market inventory or seal')
    records, markets, hashes = [], [], []
    for item in manifest['sources']:
        path = (source_dir / item['file']).resolve()
        if not path.is_relative_to(source_dir):
            raise ValueError('Unsafe source path')
        candle_raw = path.read_bytes()
        if digest(candle_raw) != item['sha256']:
            raise ValueError('Candle checksum mismatch')
        hashes.append((path, digest(candle_raw)))
        data = json.loads(candle_raw)
        if data['contract'] != universe[item['ticker']]:
            raise ValueError('Contract inventory mismatch')
        contract = scanner.Contract(**{k: v for k, v in data['contract'].items()
                                       if k in {f.name for f in fields(scanner.Contract)}})
        monitor = [scanner.Candle(**c) for c in data['HOUR_4']]
        entries = [scanner.Candle(**c) for c in data['MINUTE_15']]
        benchmark = control.replay_market(contract, monitor, entries, analyze, settings,
                                         start_ms=start, end_ms=end)
        proposal = vwap.replay_market(contract, monitor, entries, start_ms=start, end_ms=end)
        if benchmark['valid_points'] != proposal['valid_points']:
            raise ValueError('Indicator coverage mismatch')
        records += benchmark.pop('records') + proposal.pop('records')
        markets.append(dict(ticker=item['ticker'], controls=benchmark, proposal=proposal))
    groups = {m: [r for r in records if r['model'] == m] for m in (*control.MODELS, vwap.MODEL)}
    if len({r['key'] for r in records}) != len(records):
        raise ValueError('Duplicate model setup')
    for model, archived in zip(control.MODELS[:2], ('current', 'shadow')):
        actual = {(r['setup_id'], r['created_ms'], r['trigger'], r['stop'], r['target']) for r in groups[model]}
        expected = {(r['setup_id'], r['created_ms'], r['entry'], r['stop'], r['target']) for r in source['signals'][archived]}
        if actual != expected or len(actual) != len(groups[model]):
            raise ValueError('Independent control first-entry mismatch')
    for r in records:
        if not start <= r['created_ms'] - 1 < end or r['created_ms'] != r['signal_candle_ms'] + control.STEP + 1:
            raise ValueError('Signal outside frozen chronology')
        if r['filled_ms'] is not None and not r['created_ms'] - 1 <= r['filled_ms'] < min(end, r['expires_ms']):
            raise ValueError('Fill outside frozen interval')
    coverage = dict(source['coverage'])
    if (coverage['valid_points'] != sum(m['controls']['valid_points'] for m in markets)
            or coverage['expected_points_all_markets'] != len(universe) * (end - start) // control.STEP
            or coverage['failed_markets'] != len(manifest['failures'])
            or coverage['fetched_markets'] != len(markets)):
        raise ValueError('Coverage reproduction mismatch')
    accounts = {m: control.portfolio(rows) for m, rows in groups.items()}
    if digest((source_dir / 'replay-report.json').read_bytes()) != digest(raw) or any(digest(path.read_bytes()) != h for path, h in hashes):
        raise ValueError('Source mutated during evaluation')
    return dict(protocol=p['protocol'], protocol_sha256=PINNED_PROTOCOL_SHA256,
                dataset='RETROSPECTIVE_28DAY_DIAGNOSTIC', role=period['id'], start_ms=start, end_ms=end,
                period_complete=True, pristine_holdout=False, eligible_for_live_promotion=False,
                automatic_promotion=False, real_orders_enabled=False, changes_live_rules=False,
                source_report_sha256=digest(raw), source_files_verified=len(markets), coverage=coverage,
                baseline_first_entries_reproduced=True, metrics={m: control.metrics(rows) for m, rows in groups.items()},
                stress_metrics={m: vwap.stressed_metrics(rows) for m, rows in groups.items()},
                portfolios=accounts, comparison=comparison(groups, accounts), records=records, markets=markets,
                original_future_weeks_and_live_capture_unchanged=True, limitations=p['limitations'])


def decision(reports):
    p = protocol()
    expected = {x['id']: (x['start_ms'], x['end_ms']) for x in p['periods']}
    seen = set()
    for r in reports:
        if r['role'] not in expected or r['role'] in seen or (r['start_ms'], r['end_ms']) != expected[r['role']] or r['protocol_sha256'] != PINNED_PROTOCOL_SHA256:
            raise ValueError('Unregistered or duplicate long report')
        seen.add(r['role'])
    if any(not r['period_complete'] or r['coverage']['failed_markets'] or not r['coverage']['valid_points'] for r in reports):
        return 'BLOCKED_DATA_QUALITY'
    for r in reports:
        m = r['metrics'][vwap.MODEL]
        if m['resolved'] >= 20 and (m['avg_net_r'] <= 1e-12 or m['profit_factor'] != 'INF' and m['profit_factor'] <= 1 + 1e-12):
            return 'REJECTED_HISTORICAL_SPECIFICATION_NO_LIVE_CHANGE'
    if set(expected) != seen or any(r['metrics'][vwap.MODEL]['resolved'] < 50 for r in reports):
        return 'CONTINUE_INSUFFICIENT_SAMPLE'
    for r in reports:
        m, s, a, c = r['metrics'][vwap.MODEL], r['stress_metrics'][vwap.MODEL], r['portfolios'][vwap.MODEL], r['comparison']
        if not (m['win_rate'] >= 40 and m['avg_net_r'] > 0 and (m['profit_factor'] == 'INF' or m['profit_factor'] >= 1.2)
                and s['avg_net_r'] > 0 and (s['profit_factor'] == 'INF' or s['profit_factor'] > 1)
                and c['net_filled_count_difference'] > 0 and c['capped_filled_count_difference'] > 0
                and a['resolved'] >= 20 and not a['active'] and not a['uncertain']
                and a['closed_portfolio_roi_pct'] is not None and a['closed_portfolio_roi_pct'] > 0):
            return 'NO_QUALIFIED_REPLACEMENT'
    return 'HISTORICAL_CRITERIA_MET_ORIGINAL_FUTURE_AND_LIVE_VALIDATION_REQUIRED'


def next_period_gate(previous_report, previous_source, analyze, settings):
    original = json.loads(Path(previous_report).read_text())
    if original != run(previous_source, 0, analyze, settings):
        raise ValueError('Previous full report does not reproduce')
    if decision([original]) != 'CONTINUE_INSUFFICIENT_SAMPLE':
        raise ValueError('Previous rejection or quality block: next period stays sealed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--period-index', type=int, choices=(0, 1), required=True)
    parser.add_argument('--previous-report', type=Path)
    parser.add_argument('--previous-source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from analysis_terminal import server
    if args.period_index:
        if not args.previous_report or not args.previous_source:
            raise ValueError('Previous report and source required before opening period 2')
        next_period_gate(args.previous_report, args.previous_source, server.analyze_contract, server.SETTINGS)
    report = run(args.source, args.period_index, server.analyze_contract, server.SETTINGS)
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {k: v for k, v in report.items() if k not in {'records', 'markets'}}
    summary['decision'] = decision([report])
    for name, value in [('report.json', report), ('summary.json', summary)]:
        with (args.output / name).open('x') as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({'period': report['role'], 'decision': summary['decision'],
                      'metrics': report['metrics'][vwap.MODEL], 'comparison': report['comparison']}))


if __name__ == '__main__':
    main()
