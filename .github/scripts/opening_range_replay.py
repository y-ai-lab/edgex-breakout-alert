"""Preregistered offline first-hour opening-range research; no live hooks."""
import argparse
from bisect import bisect_right
from collections import Counter
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import time

import app as scanner
import historical_robustness as historical
from analysis_terminal import confirmation_zone_replay as execution
from analysis_terminal import pending_entry_replay as control
from analysis_terminal import vwap_reclaim_replay as helpers
from analysis_terminal.replay import complete_window, rule_fingerprint, strategy_parameters

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / '.github/research/opening_range_protocol.json'
PROTOCOL_SHA256 = 'bcf5639a7e24944b6957fbe433a18011190c2af8b729bb44c7c5e6c9ccc1e789'
MODEL = 'trend_utc_opening_range_first_break_net2r'
STEP, STEP4, DAY, WEEK = 900000, 14400000, 86400000, 604800000


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def protocol():
    raw = PROTOCOL.read_bytes()
    if digest(raw) != PROTOCOL_SHA256:
        raise ValueError('Preregistered protocol changed')
    p = json.loads(raw)
    for name, expected in p['frozen_dependencies_sha256'].items():
        if digest((ROOT / name).read_bytes()) != expected:
            raise ValueError('Frozen dependency changed')
    raw = (ROOT / 'app.py').read_bytes()
    s = p['scanner_sha256']
    if digest(raw) != s['local_with_one_extra_eof_newline'] and not (
            digest(raw) == s['published'] and digest(raw + b'\n') == s['local_with_one_extra_eof_newline']):
        raise ValueError('Frozen scanner changed')
    return p


def periods(p):
    return {r['id']: r for r in p['periods']}


def opening_range(entries):
    """Freeze 00:00..01:00 UTC, exclude the signal and any future bar."""
    signal = entries[-1]
    origin = signal.time_ms // DAY * DAY
    if signal.time_ms < origin + 4 * STEP:
        return None
    opening = [c for c in entries[:-1] if origin <= c.time_ms < origin + 4 * STEP]
    if [c.time_ms for c in opening] != [origin + i * STEP for i in range(4)]:
        return None
    high, low = max(c.high for c in opening), min(c.low for c in opening)
    return (origin, high, low) if high > low else None


def candidate(contract, monitor, entries, *, direction=None, price=True):
    helpers.closed_clock(monitor, entries)
    reference = opening_range(entries)
    if reference is None:
        return None, None, 'OPENING_RANGE_UNAVAILABLE'
    origin, high, low = reference
    c, previous = entries[-1], entries[-2]
    if previous.close <= high and c.low <= high and c.close > high and c.close > c.open:
        side, level = 'LONG', high
    elif previous.close >= low and c.high >= low and c.close < low and c.close < c.open:
        side, level = 'SHORT', low
    else:
        return None, None, 'STRICT_FIRST_BREAK_WAIT'
    episode = (side, origin)  # First break consumed even if trend or room fails.
    if not price:
        return None, episode, 'WARMUP_FIRST_BREAK'
    current_trend = helpers.trend(monitor) if direction is None else direction
    if current_trend != side:
        return None, episode, 'FIRST_BREAK_WRONG_4H_TREND'
    atr = scanner._atr(entries, 14)
    if not execution.positive(atr):
        return None, episode, 'INVALID_ATR'
    long = side == 'LONG'
    stop = low - .5 * atr if long else high + .5 * atr
    extension = (max(b.high for b in entries[:-1]) - .25 * atr if long
                 else min(b.low for b in entries[:-1]) + .25 * atr)
    target = execution.net_target(c.close, stop, side)
    if not all(execution.positive(v) for v in (stop, target, extension)):
        return None, episode, 'INVALID_STRUCTURE'
    if not (target <= extension if long else target >= extension):
        return None, episode, 'NET_ROOM_BELOW_2R'
    identity = f'opening-range-v1:{contract.contract_name}:{side}:{origin}'
    stamp = c.time_ms + STEP
    return dict(key=MODEL + ':' + identity, model=MODEL, setup_id=identity,
        ticker=contract.contract_name, side=side, session_start_ms=origin,
        opening_range_high=high, opening_range_low=low, roll_level=level,
        signal_candle_ms=c.time_ms, created_ms=stamp + 1, expires_ms=stamp + 4 * STEP,
        trigger=c.close, stop=stop, target=target, extension_target=extension,
        atr_15m_at_signal=atr, trend_anchor_ms=monitor[-1].time_ms,
        status='PENDING', filled_ms=None, outcome_ms=None, final_net_r=None,
        mfe_r=0., mae_r=0., step_size=contract.step_size,
        min_order_size=contract.min_order_size, max_order_size=contract.max_order_size), episode, None


def pending_state(record, monitor, entries, *, direction=None):
    # Same strict color + frozen reclaimed level, stop and prior-bar trend checks.
    return helpers.pending_state(record, monitor, entries, direction=direction)


def evaluate(record, candles, state_at, *, end_ms):
    if record['model'] != MODEL or record['key'] != MODEL + ':' + record['setup_id']:
        raise ValueError('Invalid opening-range identity')
    r = execution.evaluate(dict(record, model=execution.MODEL), candles, state_at, end_ms=end_ms)
    r['model'] = MODEL
    return r


def replay_market(contract, monitor, entries, *, start_ms, end_ms):
    if not 0 < start_ms < end_ms or start_ms % STEP or end_ms % STEP:
        raise ValueError('Invalid registered window clock')
    for cs, interval, step in ((monitor, 'HOUR_4', STEP4), (entries, 'MINUTE_15', STEP)):
        if any(a.time_ms >= b.time_ms for a, b in zip(cs, cs[1:])) or any(
                c.contract_id != contract.contract_id or c.interval != interval or c.time_ms % step
                or not all(execution.positive(v) for v in (c.open, c.high, c.low, c.close))
                or not c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high for c in cs):
            raise ValueError('Invalid market identity, grid or OHLC')
    times4 = [c.time_ms + STEP4 for c in monitor]
    times15 = [c.time_ms + STEP for c in entries]
    frames, trends = {}, {}

    def windows_at(stamp):
        if stamp not in frames:
            w4 = complete_window(monitor, bisect_right(times4, stamp + 1), 180, STEP4, stamp + 1)
            w15 = complete_window(entries, bisect_right(times15, stamp + 1), 180, STEP, stamp + 1)
            if w4 is None or w15 is None:
                frames[stamp] = None
            else:
                anchor = w4[-1].time_ms
                if anchor not in trends:
                    trends[anchor] = helpers.trend(w4)
                frames[stamp] = w4, w15, trends[anchor]
        return frames[stamp]

    records, seen, unknown_sessions, exclusions = [], set(), set(), Counter()
    valid = 0
    for stamp in range(start_ms // DAY * DAY - DAY, end_ms, STEP):
        frame = windows_at(stamp)
        if frame is None:
            unknown_sessions.update(((stamp - STEP) // DAY * DAY, stamp // DAY * DAY))
            if stamp >= start_ms:
                exclusions['INCOMPLETE_INDICATOR_WINDOW'] += 1
            continue
        w4, w15, side = frame
        valid += stamp >= start_ms
        r, episode, reason = candidate(contract, w4, w15, direction=side, price=stamp >= start_ms)
        if episode is None:
            if stamp >= start_ms:
                exclusions[reason] += 1
            continue
        if episode in seen:
            if stamp >= start_ms:
                exclusions['ALREADY_CONSUMED_FIRST_BREAK'] += 1
            continue
        seen.add(episode)
        if episode[1] in unknown_sessions:
            if stamp >= start_ms:
                exclusions['UNOBSERVED_SESSION_FIRST_BREAK'] += 1
            continue
        if stamp < start_ms:
            exclusions['WARMUP_FIRST_BREAK'] += 1
            continue
        if r is None:
            exclusions[reason] += 1
            continue
        records.append(r)
    results = []
    for r in records:
        def state_at(stamp):
            frame = windows_at(stamp)
            return None if frame is None else pending_state(r, frame[0], frame[1], direction=frame[2])
        results.append(evaluate(r, entries, state_at, end_ms=end_ms))
    return dict(records=results, valid_points=valid, exclusions=dict(exclusions))


def comparison(groups, accounts):
    def identities(rows):
        return {(r['ticker'], r['side'], r['created_ms']) for r in rows if r['filled_ms'] is not None}
    current, proposal = identities(groups[control.MODELS[0]]), identities(groups[MODEL])
    return dict(identity_basis='ticker + direction + signal-close time',
        shared_filled_setups=len(current & proposal), proposal_only_filled_setups=len(proposal - current),
        current_filled_missing_in_proposal=len(current - proposal),
        net_filled_count_difference=len(proposal) - len(current),
        capped_filled_count_difference=accounts[MODEL]['filled'] - accounts[control.MODELS[0]]['filled'],
        capped_shared_setup_count=None)


def decision(reports):
    p = protocol()
    registered, seen = periods(p), set()
    for r in reports:
        role = r['role']
        if role not in registered or role in seen or (r['start_ms'], r['end_ms']) != (
                registered[role]['start_ms'], registered[role]['end_ms']) or r['protocol_sha256'] != PROTOCOL_SHA256:
            raise ValueError('Unregistered or repeated period')
        seen.add(role)
    if any(r['coverage']['failed_markets'] or not r['coverage']['valid_points'] for r in reports):
        return 'BLOCKED_DATA_QUALITY'
    for r in reports:
        m = r['metrics'][MODEL]
        if r['period_complete'] and m['resolved'] >= 20 and (m['avg_net_r'] <= 1e-12
                or m['profit_factor'] != 'INF' and m['profit_factor'] <= 1 + 1e-12):
            return 'KILL_NO_LIVE_PROMOTION'
    pair = 'future_validation' if all(f'future_validation_{i}' in seen for i in (1, 2)) else 'historical_replication'
    selected = [r for r in reports if r['role'].startswith(pair)]
    if len(selected) != 2 or any(not r['period_complete'] or r['metrics'][MODEL]['resolved'] < 50 for r in selected):
        return 'CONTINUE_INSUFFICIENT_SAMPLE'
    for r in selected:
        m, s, a, c = r['metrics'][MODEL], r['stress_metrics'][MODEL], r['portfolios'][MODEL], r['comparison']
        if not (m['win_rate'] >= 40 and m['avg_net_r'] > 0 and (m['profit_factor'] == 'INF' or m['profit_factor'] >= 1.2)
                and s['avg_net_r'] > 0 and (s['profit_factor'] == 'INF' or s['profit_factor'] > 1)
                and c['net_filled_count_difference'] > 0 and c['capped_filled_count_difference'] > 0
                and a['resolved'] >= 20 and not a['active'] and not a['uncertain']
                and a['closed_portfolio_roi_pct'] is not None and a['closed_portfolio_roi_pct'] > 0):
            return 'NO_QUALIFIED_REPLACEMENT'
    return ('FUTURE_CRITERIA_MET_SEPARATE_CAPTURED_LIVE_AND_EXECUTION_VALIDATION_REQUIRED' if pair == 'future_validation'
            else 'HISTORICAL_CANDIDATE_UNTOUCHED_FUTURE_AND_SEPARATE_LIVE_VALIDATION_REQUIRED')


def validate_previous(role, previous, analyze, settings):
    order = list(periods(protocol()))
    if role not in order or [r['role'] for r in previous] != order[:order.index(role)]:
        raise ValueError('Sequential original predecessor evidence required')
    originals = []
    for r in previous:
        if r['engine_sha256'] != digest(Path(__file__).read_bytes()):
            raise ValueError('Changed predecessor engine')
        original = run(r['source_dir'], r['role'], analyze, settings, previous=originals)
        if original != r:
            raise ValueError('Full original predecessor does not reproduce')
        originals.append(original)
    if decision(previous) in {'KILL_NO_LIVE_PROMOTION', 'BLOCKED_DATA_QUALITY'}:
        raise ValueError('Predecessor killed/blocked; next period stays sealed')


def run(source_dir, role, analyze, settings, *, previous=(), now_ms=None):
    p = protocol()
    if role not in periods(p):
        raise ValueError('Unregistered role')
    validate_previous(role, previous, analyze, settings)
    period = periods(p)[role]
    start, end = period['start_ms'], period['end_ms']
    if end - start != WEEK or start % STEP or end % STEP or (now_ms if now_ms is not None else time.time() * 1000) < end:
        raise ValueError('Require complete registered period')
    if any(start < s['end_ms'] and s['start_ms'] < end for s in p['sealed_periods']):
        raise ValueError('Reserved evaluation interval')
    if rule_fingerprint(analyze, settings) != p['production_rule_fingerprint'] or strategy_parameters(settings) != p['production_parameters']:
        raise ValueError('Frozen production analysis changed')
    source_dir = Path(source_dir).resolve()
    raw = (source_dir / 'replay-report.json').read_bytes()
    source = json.loads(raw)
    if period.get('source_report_sha256') and digest(raw) != period['source_report_sha256']:
        raise ValueError('Original source archive changed')
    if not period.get('source_report_sha256'):
        seal = json.loads((source_dir.parent / 'source-seal.json').read_text())
        if seal['source_report_sha256'] != digest(raw) or seal['files'] != {
                x['file']: x['sha256'] for x in source['manifest']['sources']}:
            raise ValueError('First future source seal changed')
    manifest = source['manifest']
    if (source['start_ms'], source['end_ms']) != (start, end) or source.get('dataset') != 'RETROSPECTIVE' or (
            source.get('eligible_for_live_promotion') is not False or
            digest(canonical(manifest['universe'])) != p['source_universe_sha256'] or
            manifest['indicator_windows'] != p['indicator_windows']):
        raise ValueError('Source differs from preregistration')
    universe = {c['contract_name']: c for c in manifest['universe']}
    inventory = [x['ticker'] for x in manifest['sources'] + manifest['failures']]
    if len(universe) != len(manifest['universe']) or len(set(inventory)) != len(inventory) or set(inventory) != set(universe):
        raise ValueError('Missing or duplicate market inventory')
    # Freeze the old comparator in its own registered evaluator before this new model.
    if role == 'development':
        baseline = control.run(source_dir, analyze, settings, role='development')
    elif role.startswith('historical_replication'):
        baseline = historical.run(source_dir, int(role[-1]) - 1, analyze, settings)
    else:
        # Future periods use the same original controls; no alternate target/price guards.
        baseline = None
    records, markets, hashes = [], [], []
    for item in manifest['sources']:
        path = (source_dir / item['file']).resolve()
        if not path.is_relative_to(source_dir):
            raise ValueError('Unsafe candle path')
        candle_raw = path.read_bytes()
        if digest(candle_raw) != item['sha256']:
            raise ValueError('Candle checksum changed')
        hashes.append((path, digest(candle_raw)))
        data = json.loads(candle_raw)
        if data['contract'] != universe[item['ticker']]:
            raise ValueError('Contract inventory changed')
        contract = scanner.Contract(**{k: v for k, v in data['contract'].items() if k in {f.name for f in fields(scanner.Contract)}})
        w4 = [scanner.Candle(**c) for c in data['HOUR_4']]
        w15 = [scanner.Candle(**c) for c in data['MINUTE_15']]
        if baseline is None:
            records.extend(control.replay_market(contract, w4, w15, analyze, settings, start_ms=start, end_ms=end)['records'])
        result = replay_market(contract, w4, w15, start_ms=start, end_ms=end)
        records.extend(result.pop('records'))
        markets.append(result)
    if baseline:
        records = baseline['records'] + records
    if len({r['key'] for r in records}) != len(records):
        raise ValueError('Duplicate setup ledger')
    groups = {m: [r for r in records if r['model'] == m] for m in (*control.MODELS, MODEL)}
    for m, old in zip(control.MODELS[:2], ('current', 'shadow')):
        actual = {(r['setup_id'], r['created_ms'], r['trigger'], r['stop'], r['target']) for r in groups[m]}
        archived = {(r['setup_id'], r['created_ms'], r['entry'], r['stop'], r['target']) for r in source['signals'][old]}
        if actual != archived or len(actual) != len(groups[m]):
            raise ValueError('Original first-entry control mismatch')
    for r in records:
        if not start <= r['created_ms'] - 1 < end or r['created_ms'] != r['signal_candle_ms'] + STEP + 1:
            raise ValueError('Invalid signal chronology')
        if r['filled_ms'] is not None and not r['created_ms'] - 1 <= r['filled_ms'] < r['expires_ms']:
            raise ValueError('Invalid fill chronology')
    coverage = source['coverage']
    if (sum(m['valid_points'] for m in markets) != coverage['valid_points'] or
            coverage['expected_points_all_markets'] != len(universe) * WEEK // STEP or
            coverage['failed_markets'] != len(manifest['failures']) or coverage['fetched_markets'] != len(markets)):
        raise ValueError('Original coverage does not reproduce')
    metrics = {m: control.metrics(rows) for m, rows in groups.items()}
    accounts = {m: control.portfolio(rows) for m, rows in groups.items()}
    if baseline and any(metrics[m] != baseline['metrics'][m] or accounts[m] != baseline['portfolios'][m] for m in control.MODELS):
        raise ValueError('Original full control economics mismatch')
    if digest((source_dir / 'replay-report.json').read_bytes()) != digest(raw) or any(digest(path.read_bytes()) != h for path, h in hashes):
        raise ValueError('Original sources mutated')
    return dict(protocol=p['protocol'], protocol_sha256=PROTOCOL_SHA256,
        engine_sha256=digest(Path(__file__).read_bytes()), role=role, start_ms=start, end_ms=end,
        source_dir=str(source_dir), source_report_sha256=digest(raw), source_files_verified=len(markets),
        dataset='PREREGISTERED_PUBLIC_OHLC_HYPOTHETICAL_RESEARCH', period_complete=True,
        pristine_holdout=not period['market_already_viewed'], coverage=coverage,
        metrics=metrics, stress_metrics={m: helpers.stressed_metrics(rows) for m, rows in groups.items()},
        portfolios=accounts, comparison=comparison(groups, accounts), records=records, markets=markets,
        original_control_economics_reproduced=True, changes_live_rules=False, real_orders_enabled=False,
        automatic_promotion=False, eligible_for_live_promotion=False, forward_collection_enabled=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--role', choices=list(periods(protocol())), required=True)
    parser.add_argument('--previous-report', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from analysis_terminal import server
    previous = [json.loads(p.read_text()) for p in args.previous_report]
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        report = run(args.source, args.role, server.analyze_contract, server.SETTINGS, previous=previous)
    except ValueError:
        with (args.output / 'blocked.json').open('x') as f:
            json.dump(dict(role=args.role, status='BLOCKED_DATA_QUALITY', metrics=None, portfolios=None,
                           eligible_for_live_promotion=False, real_orders_enabled=False), f)
        raise
    summary = {k: v for k, v in report.items() if k not in {'records', 'markets', 'source_dir'}}
    summary['decision'] = decision(previous + [report])
    for name, value in [('report.json', report), ('summary.json', summary)]:
        with (args.output / name).open('x') as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({'role':args.role, 'decision':summary['decision'], 'metrics':report['metrics'][MODEL],
                      'comparison':report['comparison'], 'portfolio':report['portfolios'][MODEL]}))


if __name__ == '__main__':
    main()
