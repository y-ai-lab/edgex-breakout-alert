"""Public Funding cost diagnostic; never changes a trade or capital simulation."""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from analysis_terminal import pending_entry_replay as engine
from analysis_terminal import vwap_reclaim_replay as stress

STEP = 900000
PLAN_SHA256 = '27160b8fc94f084c1e2dc82d093b887795df82fcabfbb1ba9bd24b0cd685860e'


def number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError('Invalid funding number')
    result = float(value)
    if not math.isfinite(result) or positive and result <= 0:
        raise ValueError('Invalid funding number')
    return result


def integer(value):
    result = number(value)
    if result != int(result):
        raise ValueError('Non-integer funding clock/interval')
    return int(result)


def settlements(pages, contract_id, start_ms, end_ms):
    """Require an exhausted, uniform, complete UTC grid including cutoff."""
    if not pages or type(start_ms) is not int or type(end_ms) is not int or end_ms <= start_ms:
        raise ValueError('Missing funding period/pages')
    rows, cursors = [], set()
    for i, page in enumerate(pages):
        if page.get('code') != 'SUCCESS':
            raise ValueError('Failed funding page')
        data = page['data']
        cursor = data['nextPageOffsetData']
        if (bool(cursor) != (i < len(pages)-1) or cursor and cursor in cursors
                or not isinstance(data['dataList'], list)):
            raise ValueError('Unexhausted/repeated funding pagination')
        if cursor:
            if not isinstance(cursor, str):
                raise ValueError('Invalid funding cursor')
            cursors.add(cursor)
        for row in data['dataList']:
            if str(row['contractId']) != str(contract_id) or row['isSettlement'] is not True:
                raise ValueError('Wrong market or forecast funding')
            t = integer(row['fundingTimestamp'])
            interval = integer(row['fundingRateIntervalMin'])*60000
            if (t != integer(row['fundingTime']) or interval <= 0 or t % interval
                    or not start_ms-86400000 <= t <= end_ms):
                raise ValueError('Unverified funding timestamp/interval/range')
            rows.append(dict(time_ms=t, interval_ms=interval,
                             rate=number(row['fundingRate']), index=number(row['indexPrice'], True)))
    if not rows or len({r['time_ms'] for r in rows}) != len(rows):
        raise ValueError('Missing/duplicate funding settlements')
    intervals = {r['interval_ms'] for r in rows}
    if len(intervals) != 1:
        raise ValueError('Variable funding interval requires separate verification')
    interval = intervals.pop()
    if interval > 86400000:
        raise ValueError('Funding interval exceeds audit prefix')
    rows.sort(key=lambda r: r['time_ms'])
    times = [r['time_ms'] for r in rows]
    if any(b-a != interval for a, b in zip(times, times[1:])):
        raise ValueError('Funding settlement gap')
    expected = set(range(((start_ms+interval-1)//interval)*interval, end_ms+1, interval))
    if not expected.issubset(times):
        raise ValueError('Funding grid does not cover fixed period')
    return rows


def exposure(row, events):
    if row['status'] not in {'TP', 'SL'}:
        return dict(status='UNRESOLVED_OR_UNFILLED', funding_charge_r=None, adjusted_net_r=None)
    fill, end = row['filled_ms'], row['outcome_ms']
    if (type(fill) is not int or type(end) is not int or fill % STEP or end % STEP
            or end <= fill or row['side'] not in {'LONG', 'SHORT'}):
        raise ValueError('Invalid original funding exposure clock/side')
    risk = number(row['net_risk'], True)
    original = number(row['final_net_r'])
    core, boundary = [], []
    for event in events:
        t = event['time_ms']
        if not fill <= t <= end:
            continue
        charge = (1 if row['side'] == 'LONG' else -1)*event['index']*event['rate']/risk
        if not math.isfinite(charge):
            raise ValueError('Unrepresentable Funding cost')
        (core if fill+STEP <= t < end-STEP else boundary).append(charge)
    partial = math.fsum(core)
    if not math.isfinite(partial):
        raise ValueError('Unrepresentable Funding aggregate')
    return dict(status='TIMING_AMBIGUOUS' if boundary else 'VERIFIED_HYPOTHETICAL',
                known_held_settlements=len(core), boundary_settlements=len(boundary),
                known_held_charge_r=partial, funding_charge_r=None if boundary else partial,
                adjusted_net_r=None if boundary else original-partial)


def summarize(records, event_maps):
    if len({r['key'] for r in records}) != len(records):
        raise ValueError('Duplicate original trade')
    diagnostics = []
    for row in records:
        if row['status'] not in {'TP', 'SL'}:
            d = exposure(row, [])
        elif event_maps.get(row['ticker']) is None:
            d = dict(status='BLOCKED_FUNDING_DATA', funding_charge_r=None, adjusted_net_r=None)
        else:
            d = exposure(row, event_maps[row['ticker']])
        diagnostics.append(dict(key=row['key'], **d))
    counts = Counter(d['status'] for d in diagnostics)
    resolved = sum(r['status'] in {'TP', 'SL'} for r in records)
    complete = bool(resolved) and counts['VERIFIED_HYPOTHETICAL'] == resolved
    values = [d['adjusted_net_r'] for d in diagnostics if d['status'] == 'VERIFIED_HYPOTHETICAL']
    loss = -math.fsum(v for v in values if v < 0)
    result = dict(original_metrics=engine.metrics(records), statuses=dict(counts),
                  funding_verified_resolved=counts['VERIFIED_HYPOTHETICAL'],
                  timing_ambiguous_resolved=counts['TIMING_AMBIGUOUS'],
                  blocked_funding_resolved=counts['BLOCKED_FUNDING_DATA'],
                  full_resolved_basket_verified=complete,
                  known_held_settlements=sum(d.get('known_held_settlements', 0) for d in diagnostics),
                  boundary_settlements=sum(d.get('boundary_settlements', 0) for d in diagnostics),
                  partial_known_held_charge_r_sum=math.fsum(d.get('known_held_charge_r', 0) for d in diagnostics),
                  funding_adjusted_avg_net_r=math.fsum(values)/resolved if complete else None,
                  funding_adjusted_profit_factor=(math.fsum(v for v in values if v > 0)/loss
                      if complete and loss > 0 else None),
                  funding_adjusted_net_win_rate_pct=(100*sum(v > 0 for v in values)/resolved if complete else None),
                  portfolio_roi_pct=None, new_samples_added=0,
                  r_basis='ORIGINAL_FROZEN_NET_STOP_RISK',
                  interpretation='PUBLIC_HISTORY_HYPOTHETICAL_NOT_ACTUAL_FUNDING_OR_ACCOUNT_ROI')
    return result, diagnostics


def run(plan_path, manifest_path, report_paths):
    """Read immutable public archives and ledgers; stop on provenance mismatch."""
    raw = Path(plan_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != PLAN_SHA256:
        raise ValueError('Registered Funding audit plan changed')
    engine_path = Path(engine.__file__)
    frozen = json.loads(engine_path.with_name('pending_live_protocol.json').read_text())['frozen_engine_sha256']
    if hashlib.sha256(engine_path.read_bytes()).hexdigest() != frozen:
        raise ValueError('Frozen original research engine changed')
    plan = json.loads(raw)
    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_bytes())
    if (hashlib.sha256(raw).hexdigest() != manifest['plan_sha256']
            or (manifest['start_ms'], manifest['end_ms']) != (plan['start_ms'], plan['end_ms'])
            or len(report_paths) != len(plan['sources'])):
        raise ValueError('Funding plan/manifest scope mismatch')
    directory = manifest_file.parent.resolve()
    maps, coverage, identities = {}, [], {}
    for market in manifest['markets']:
        ticker = market['ticker']
        if ticker in maps:
            raise ValueError('Duplicate funding market')
        identities[ticker] = str(market['contract_id'])
        if (market['start_ms'], market['end_exclusive_ms']) != (plan['start_ms']-86400000, plan['end_ms']+1):
            raise ValueError('Wrong archived funding query period')
        pages = []
        for i, item in enumerate(market['pages']):
            f = (directory/item['file']).resolve()
            if not f.is_relative_to(directory):
                raise ValueError('Unsafe funding archive path')
            body = f.read_bytes()
            if hashlib.sha256(body).hexdigest() != item['sha256']:
                raise ValueError('Archived funding checksum changed')
            page = json.loads(body)
            if item['status'] != 200 or item['requested_offset'] != (pages[-1]['data']['nextPageOffsetData'] if i else ''):
                raise ValueError('Funding request cursor/HTTP mismatch')
            pages.append(page)
        try:
            if market['status'] != 'EXHAUSTED':
                raise ValueError('Funding fetch/pagination not complete')
            events = settlements(pages, identities[ticker], plan['start_ms'], plan['end_ms'])
            maps[ticker] = events
            coverage.append(dict(status='VERIFIED_GRID', interval_minutes=events[0]['interval_ms']//60000,
                                 settlement_records=len(events)))
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            maps[ticker] = None
            coverage.append(dict(status='BLOCKED_FUNDING_DATA', reason=str(exc)))
    output, audit_ledgers, expected_markets = [], {}, set()
    for spec, report_path in zip(plan['sources'], report_paths):
        body = Path(report_path).read_bytes()
        if hashlib.sha256(body).hexdigest() != spec['report_sha256']:
            raise ValueError('Original report changed')
        report = json.loads(body)
        if (report['start_ms'], report['end_ms']) != (plan['start_ms'], plan['end_ms']):
            raise ValueError('Wrong original period')
        source_dir = Path(report['source_dir']).resolve()
        source_raw = (source_dir/'replay-report.json').read_bytes()
        if hashlib.sha256(source_raw).hexdigest() != plan['source_report_sha256']:
            raise ValueError('Original source report changed')
        source = json.loads(source_raw)
        for item in source['manifest']['sources']:
            f = (source_dir/item['file']).resolve()
            if not f.is_relative_to(source_dir):
                raise ValueError('Unsafe original source path')
            data = f.read_bytes()
            if hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ValueError('Original market archive changed')
            if item['ticker'] in identities and str(json.loads(data)['contract']['contract_id']) != identities[item['ticker']]:
                raise ValueError('Funding contract identity mismatch')
        for model, metrics in report['metrics'].items():
            rows = [r for r in report['records'] if r['model'] == model]
            if (engine.metrics(rows) != metrics or engine.portfolio(rows) != report['portfolios'][model]
                    or stress.stressed_metrics(rows) != report['stress_metrics'][model]):
                raise ValueError('Original metrics/stress/portfolio mismatch')
        rows = [r for r in report['records'] if r['model'] == spec['model']]
        if any(r['status'] in {'TP','SL'} and not plan['start_ms'] <= r['filled_ms'] < r['outcome_ms'] <= plan['end_ms'] for r in rows):
            raise ValueError('Original trade outside Funding period')
        expected_markets.update(r['ticker'] for r in rows if r['filled_ms'] is not None)
        result, ledger = summarize(rows, maps)
        output.append(dict(id=spec['id'], model=spec['model'], **result))
        audit_ledgers[spec['id']] = ledger
    if expected_markets != set(maps):
        raise ValueError('Funding market scope does not conserve original filled markets')
    return dict(plan_id=plan['id'], period=[plan['start_ms'], plan['end_ms']],
                plan_sha256=PLAN_SHA256, funding_manifest_sha256=hashlib.sha256(manifest_file.read_bytes()).hexdigest(),
                funding_markets=len(maps), coverage_statuses=dict(Counter(c['status'] for c in coverage)),
                settlement_intervals_minutes=sorted({c['interval_minutes'] for c in coverage if c['status']=='VERIFIED_GRID'}),
                sources=output, new_samples_added=0, changes_live_rules=False, portfolio_roi_pct=None), audit_ledgers
