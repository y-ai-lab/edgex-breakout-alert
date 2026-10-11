"""Finite historical-only audit of frozen VWAP; no live integration or orders."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import threading
import time

import app as scanner
from analysis_terminal import pending_entry_replay as control, vwap_reclaim_replay as vwap
from analysis_terminal.replay import replay_contract, rule_fingerprint, strategy_parameters
from vwap_long_robustness import comparison

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / '.github/research/historical_only_protocol.json'
PIN = 'bb06ca99eed28f90aabc05c0bcbdb376bda2c05273cf0391ba90dc6a931d72c3'
DAY, STEP = 86400000, control.STEP
CURRENT = control.MODELS[0]
MODELS = (CURRENT, vwap.MODEL)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def protocol():
    raw = PROTOCOL.read_bytes()
    if digest(raw) != PIN:
        raise ValueError('Registered historical-only protocol changed')
    p = json.loads(raw)
    for name, expected in p['frozen_dependencies_sha256'].items():
        if digest((ROOT / name).read_bytes()) != expected:
            raise ValueError('Frozen research dependency changed')
    if digest((ROOT / 'app.py').read_bytes()) not in p['scanner_hashes']:
        raise ValueError('Frozen scanner changed')
    return p


def period(index, *, now_ms=None):
    p = protocol()
    if type(index) is not int or not 0 <= index < len(p['periods']):
        raise ValueError('Unregistered historical period')
    x = p['periods'][index]
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if (x['end_ms'] - x['start_ms'] != 35 * DAY
            or x['exit_cutoff_ms'] - x['end_ms'] != 7 * DAY
            or any(x[k] % DAY for k in ('start_ms', 'end_ms', 'exit_cutoff_ms'))
            or x['exit_cutoff_ms'] > now_ms // DAY * DAY):
        raise ValueError('Incomplete or altered historical interval')
    return p, x


def history(get, contract, interval, begin, end):
    """Synchronous public transport; preserve pages and reject revisions/gaps."""
    step = scanner.INTERVAL_MS[interval]
    params = dict(contractId=contract.contract_id, klineType=interval, priceType='LAST_PRICE',
                  size='1000', filterBeginKlineTimeInclusive=str(begin), filterEndKlineTimeExclusive=str(end))
    found, tokens = {}, set()
    for _ in range(100):
        payload = get('/api/v2/public/quote/getKline', dict(params))
        if payload.get('code') != 'SUCCESS' or not isinstance(payload.get('data'), dict):
            raise ValueError('Failed public history')
        data = payload['data']
        if not isinstance(data.get('dataList'), list):
            raise ValueError('Missing public candle list')
        for raw in data['dataList']:
            c = scanner.Candle.from_payload(raw, fallback_interval=interval) if isinstance(raw, dict) else None
            if (c is None or c.contract_id != contract.contract_id or c.interval != interval
                    or raw.get('priceType') != 'LAST_PRICE' or c.time_ms % step
                    or not begin <= c.time_ms < end
                    or not all(math.isfinite(v) and v > 0 for v in (c.open, c.high, c.low, c.close))
                    or not c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high
                    or not all(math.isfinite(v) and v >= 0 for v in (c.volume, c.value))):
                raise ValueError('Invalid public candle identity, grid, OHLC or volume')
            if c.time_ms in found and found[c.time_ms] != c:
                raise ValueError('Conflicting public candle revisions')
            found[c.time_ms] = c
        token = data.get('nextPageOffsetData')
        if not token:
            return sorted(found.values(), key=lambda c: c.time_ms)
        if not isinstance(token, str) or token in tokens or not data['dataList']:
            raise ValueError('Repeated or invalid public pagination')
        tokens.add(token)
        params['offsetData'] = token
    raise ValueError('Public pagination safety bound')


def collect(index, universe_report, output, client, *, now_ms=None,
            previous_report=None, previous_source=None, analyze=None, settings=None):
    p, x = period(index, now_ms=now_ms)
    if index:
        if previous_report is None or previous_source is None:
            raise ValueError('First full historical report required before second collection')
        previous = json.loads(Path(previous_report).read_text())
        reproduced = run(previous_source, 0, analyze, settings, now_ms=now_ms)
        if previous != reproduced:
            raise ValueError('First historical ledger does not reproduce')
        if decision([previous]) in {'BLOCKED_DATA_QUALITY', 'KILL_HISTORICAL_ONLY_NO_LIVE_CHANGE'}:
            raise ValueError('First period rejected or blocked; second collection stopped')
    universe = json.loads(Path(universe_report).read_text())['manifest']['universe']
    if digest(canonical(universe)) != p['universe_sha256'] or len(universe) != p['universe_count']:
        raise ValueError('Registered public universe changed')
    contracts = [scanner.Contract(**c) for c in universe]
    if len({c.contract_id for c in contracts}) != len(contracts):
        raise ValueError('Duplicate contract identity')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'candles').mkdir()
    scan_start = x['start_ms'] - 6 * 14400000 - 4 * STEP
    begins = {'HOUR_4': (scan_start - 181 * 14400000) // 14400000 * 14400000,
              'MINUTE_15': scan_start - 180 * STEP}
    lock, next_request = threading.Lock(), [0.0]

    def get(path, params):
        for attempt in range(3):
            with lock:
                delay = max(0, next_request[0] - time.monotonic())
                if delay:
                    time.sleep(delay)
                next_request[0] = time.monotonic() + .2
            try:
                return client._get_json_sync(path, params)
            except RuntimeError:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)

    def market(c):
        try:
            frames = {k: [asdict(b) for b in history(get, c, k, start, x['exit_cutoff_ms'])]
                      for k, start in begins.items()}
            raw = canonical(dict(contract=asdict(c), **frames))
            name = f'candles/{int(c.contract_id)}.json'
            with (output / name).open('xb') as f:
                f.write(raw)
            return dict(ticker=c.contract_name, file=name, sha256=digest(raw),
                        counts={k: len(v) for k, v in frames.items()}), None
        except Exception as exc:
            return None, dict(ticker=c.contract_name, error_type=type(exc).__name__)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(market, contracts))
    manifest = dict(dataset='PUBLIC_LAST_PRICE_ARCHIVE', protocol_sha256=PIN,
                    period=x, fetched_ms=int(time.time() * 1000), parameters=p['production_parameters'],
                    rule_fingerprint=p['production_rule_fingerprint'], universe=universe,
                    sources=sorted([a for a, _ in results if a], key=lambda a: a['ticker']),
                    failures=sorted([b for _, b in results if b], key=lambda b: b['ticker']),
                    eligible_for_live_promotion=False, real_orders_enabled=False)
    raw = canonical(manifest)
    with (output / 'manifest.json').open('xb') as f:
        f.write(raw)
    with (output / 'seal.json').open('xb') as f:
        f.write(canonical(dict(manifest_sha256=digest(raw),
                              files={s['file']: s['sha256'] for s in manifest['sources']})))
    return manifest


def resume_open(record, candles, *, start_ms, end_ms):
    if record['status'] != 'OPEN':
        return dict(record)
    result = control.evaluate(record, candles, {}, start_ms=start_ms, end_ms=end_ms)
    frozen = ('key', 'model', 'setup_id', 'ticker', 'side', 'created_ms', 'signal_candle_ms',
              'expires_ms', 'filled_ms', 'trigger', 'stop', 'target', 'entry', 'net_risk', 'nominal_fill')
    if any(result.get(k) != record.get(k) for k in frozen):
        raise ValueError('Original frozen entry or fill changed during exit followup')
    return result


def stage(rows):
    groups = {m: [r for r in rows if r['model'] == m] for m in MODELS}
    accounts = {m: control.portfolio(r) for m, r in groups.items()}
    return dict(metrics={m: control.metrics(r) for m, r in groups.items()},
                stress_metrics={m: vwap.stressed_metrics(r) for m, r in groups.items()},
                portfolios=accounts, comparison=comparison(groups, accounts),
                blocked_records={m: sum(r['status'] in {'AMBIGUOUS', 'DATA_GAP', 'INVALIDATED_GAP'}
                                        for r in rs) for m, rs in groups.items()})


def run(source_dir, index, analyze, settings, *, now_ms=None):
    p, x = period(index, now_ms=now_ms)
    if strategy_parameters(settings) != p['production_parameters'] or rule_fingerprint(analyze, settings) != p['production_rule_fingerprint']:
        raise ValueError('Frozen production analyzer changed')
    source_dir = Path(source_dir).resolve()
    raw = (source_dir / 'manifest.json').read_bytes()
    seal = json.loads((source_dir / 'seal.json').read_text())
    manifest = json.loads(raw)
    if (seal['manifest_sha256'] != digest(raw) or manifest['protocol_sha256'] != PIN
            or manifest['dataset'] != 'PUBLIC_LAST_PRICE_ARCHIVE' or manifest['period'] != x
            or digest(canonical(manifest['universe'])) != p['universe_sha256']
            or manifest['parameters'] != p['production_parameters']
            or manifest['rule_fingerprint'] != p['production_rule_fingerprint']
            or manifest['real_orders_enabled'] is not False or manifest['eligible_for_live_promotion'] is not False):
        raise ValueError('Original source seal, period or universe changed')
    universe = {c['contract_name']: c for c in manifest['universe']}
    inventory = [s['ticker'] for s in manifest['sources'] + manifest['failures']]
    if (len(universe) != len(manifest['universe']) or len(set(inventory)) != len(inventory)
            or set(inventory) != set(universe)
            or seal['files'] != {s['file']: s['sha256'] for s in manifest['sources']}):
        raise ValueError('Source market inventory differs from first seal')
    records, final, markets, diagnostics, hashes = [], [], [], [], []
    for s in manifest['sources']:
        path = (source_dir / s['file']).resolve()
        if not path.is_relative_to(source_dir):
            raise ValueError('Unsafe source path')
        candle_raw = path.read_bytes()
        if digest(candle_raw) != s['sha256']:
            raise ValueError('Original candle checksum changed')
        hashes.append((path, s['sha256']))
        data = json.loads(candle_raw)
        if data['contract'] != universe[s['ticker']]:
            raise ValueError('Original candle contract changed')
        c = scanner.Contract(**data['contract'])
        w4, w15 = [scanner.Candle(**a) for a in data['HOUR_4']], [scanner.Candle(**a) for a in data['MINUTE_15']]
        b = control.replay_market(c, w4, w15, analyze, settings, start_ms=x['start_ms'], end_ms=x['end_ms'])
        legacy = replay_contract(c, w4, w15, analyze, start_ms=x['start_ms'], end_ms=x['end_ms'])
        current = [r for r in b.pop('records') if r['model'] == CURRENT]
        actual = {(r['setup_id'], r['created_ms'], r['trigger'], r['stop'], r['target']) for r in current}
        expected = {(r['setup_id'], r['created_ms'], r['entry'], r['stop'], r['target']) for r in legacy['current']}
        if actual != expected or len(actual) != len(current) or len(expected) != len(legacy['current']):
            raise ValueError('Independent current first-entry comparator mismatch')
        diagnostics.extend(legacy['shadow'])  # Preserved diagnostic; never claimed reproduced or adopted.
        v = vwap.replay_market(c, w4, w15, start_ms=x['start_ms'], end_ms=x['end_ms'])
        if b['valid_points'] != v['valid_points'] or b['valid_points'] != legacy['valid_points']:
            raise ValueError('Indicator coverage mismatch')
        rows = current + v.pop('records')
        records.extend(rows)
        final.extend(resume_open(r, w15, start_ms=x['end_ms'], end_ms=x['exit_cutoff_ms']) for r in rows)
        markets.append(dict(ticker=c.contract_name, current=b, proposal=v))
    if len({r['key'] for r in records}) != len(records):
        raise ValueError('Duplicate frozen setup identity')
    for r in records:
        if (not x['start_ms'] <= r['created_ms'] - 1 < x['end_ms']
                or r['created_ms'] != r['signal_candle_ms'] + STEP + 1
                or r['filled_ms'] is not None and not r['created_ms'] - 1 <= r['filled_ms'] < min(x['end_ms'], r['expires_ms'])):
            raise ValueError('Frozen signal or fill outside original interval')
    if digest((source_dir / 'manifest.json').read_bytes()) != digest(raw) or any(digest(path.read_bytes()) != h for path, h in hashes):
        raise ValueError('Original source mutated')
    coverage = dict(expected_market_observations=len(universe) * 35 * 96,
                    valid_market_observations=sum(m['current']['valid_points'] for m in markets),
                    requested_markets=len(universe), fetched_markets=len(markets), failed_markets=len(manifest['failures']))
    coverage['indicator_coverage_pct'] = 100 * coverage['valid_market_observations'] / coverage['expected_market_observations']
    return dict(protocol=p['protocol'], protocol_sha256=PIN, role=x['id'], **{k: x[k] for k in ('start_ms', 'end_ms', 'exit_cutoff_ms')},
                dataset='HISTORICAL_ONLY_HYPOTHETICAL', period_complete=True, backtest_execution_complete=True,
                future_dates_required=False, eligible_for_live_promotion=False, automatic_promotion=False,
                real_orders_enabled=False, changes_live_rules=False, source_manifest_sha256=digest(raw),
                source_files_verified=len(markets), current_first_entries_reproduced=True, coverage=coverage,
                initial_records_sha256=digest(canonical(records)),
                initial=stage(records), final=stage(final), records=records, final_records=final, markets=markets,
                legacy_shadow_diagnostic=diagnostics, legacy_shadow_performance_verified=False,
                funding_status='NOT_YET_VERIFIED', limitations=p['limitations'])


def decision(reports):
    p = protocol()
    expected = {x['id']: x for x in p['periods']}
    seen = set()
    for r in reports:
        if (r['role'] not in expected or r['role'] in seen or r['protocol_sha256'] != PIN
                or any(r[k] != expected[r['role']][k] for k in ('start_ms', 'end_ms', 'exit_cutoff_ms'))):
            raise ValueError('Repeated or unregistered historical report')
        seen.add(r['role'])
    if any(not r['period_complete'] or r['coverage']['failed_markets'] or not r['coverage']['valid_market_observations'] for r in reports):
        return 'BLOCKED_DATA_QUALITY'
    for r in reports:
        m = r['final']['metrics'][vwap.MODEL]
        if m['resolved'] >= 20 and (m['avg_net_r'] <= 1e-12 or m['profit_factor'] != 'INF' and m['profit_factor'] <= 1 + 1e-12):
            return 'KILL_HISTORICAL_ONLY_NO_LIVE_CHANGE'
    if seen != set(expected) or any(r['final']['metrics'][vwap.MODEL]['resolved'] < 50 for r in reports):
        return 'BACKTEST_INSUFFICIENT_SAMPLE_NO_QUALIFIED_REPLACEMENT'
    for r in reports:
        f = r['final']; m, s, a, c = f['metrics'][vwap.MODEL], f['stress_metrics'][vwap.MODEL], f['portfolios'][vwap.MODEL], f['comparison']
        if not (m['win_rate'] >= 40 and m['avg_net_r'] > 0 and (m['profit_factor'] == 'INF' or m['profit_factor'] >= 1.2)
                and s['avg_net_r'] > 0 and (s['profit_factor'] == 'INF' or s['profit_factor'] > 1)
                and c['net_filled_count_difference'] > 0 and c['capped_filled_count_difference'] > 0
                and a['resolved'] >= 20 and not a['active'] and not a['uncertain']
                and not f['blocked_records'][vwap.MODEL]
                and a['closed_portfolio_roi_pct'] is not None and a['closed_portfolio_roi_pct'] > 0):
            return 'BACKTEST_NO_QUALIFIED_REPLACEMENT'
    return 'BACKTEST_CANDIDATE_BASE_COSTS_ONLY_FUNDING_AND_REAL_FILL_UNVERIFIED'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--period-index', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from analysis_terminal import server
    report = run(args.source, args.period_index, server.analyze_contract, server.SETTINGS)
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / 'report.json').open('xb') as f:
        f.write(canonical(report))
    summary = {k: v for k, v in report.items() if k not in {'records', 'final_records', 'markets', 'legacy_shadow_diagnostic'}}
    summary['decision'] = decision([report])
    with (args.output / 'summary.json').open('xb') as f:
        f.write(canonical(summary))
    print(json.dumps(summary, allow_nan=False))


if __name__ == '__main__':
    main()
