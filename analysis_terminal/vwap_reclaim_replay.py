"""Preregistered session-VWAP research. No DB, notifier or order integration."""
from bisect import bisect_right
from collections import Counter
from dataclasses import fields
import hashlib
import json
import math
from pathlib import Path
import time

import app as scanner
from analysis_terminal import confirmation_zone_replay as execution, pending_entry_replay as control
from analysis_terminal.replay import complete_window, rule_fingerprint, strategy_parameters

MODEL = 'trend_session_vwap_reclaim_net2r'
PROTOCOL = Path(__file__).with_name('vwap_reclaim_protocol.json')
STEP, STEP4, DAY, WINDOW = 900000, 14400000, 86400000, 180
RULES = dict(session_minimum_bars=4, volume_lookback=20, relative_volume_minimum=2.0,
             local_structure_bars=4, atr_stop_buffer=.5, atr_target_buffer=.25,
             min_net_rr=2.0, pending_bars=4, fee_bps_each_side=5, slippage_bps_each_side=2)


def trend(monitor):
    closes = [c.close for c in monitor]
    fast, slow = scanner._ema(closes, 20), scanner._ema(closes, 50)
    return 'LONG' if closes[-1] > fast > slow else 'SHORT' if closes[-1] < fast < slow else None


def closed_clock(monitor, entries):
    if len(monitor) != WINDOW or len(entries) != WINDOW:
        raise ValueError('Exactly 180 closed indicator bars required')
    stamp = entries[-1].time_ms + STEP
    if (entries[-1].time_ms % STEP or monitor[-1].time_ms + STEP4 != stamp // STEP4 * STEP4
            or any(b.time_ms - a.time_ms != step for cs, step in ((monitor, STEP4), (entries, STEP))
                   for a, b in zip(cs, cs[1:]))):
        raise ValueError('Future, stale or incomplete indicator clock')


def session_reference(entries):
    """Exclude signal from both VWAP and the preceding volume baseline."""
    c = entries[-1]
    origin = c.time_ms // DAY * DAY
    session = [b for b in entries[:-1] if origin <= b.time_ms < c.time_ms]
    if len(session) < RULES['session_minimum_bars']:
        return None
    if (session[0].time_ms != origin or session[-1].time_ms + STEP != c.time_ms
            or any(b.time_ms - a.time_ms != STEP for a, b in zip(session, session[1:]))):
        return None
    if any(not isinstance(b.volume, (float, int)) or isinstance(b.volume, bool)
           or not math.isfinite(b.volume) or b.volume < 0 for b in entries):
        raise ValueError('Invalid base volume')
    total = sum(b.volume for b in session)
    mean_volume = sum(b.volume for b in entries[-21:-1]) / RULES['volume_lookback']
    if total <= 0 or mean_volume <= 0:
        return None
    level = sum(b.volume * (b.high + b.low + b.close) / 3 for b in session) / total
    return origin, level, c.volume / mean_volume


def candidate(contract, monitor, entries, *, direction=None):
    closed_clock(monitor, entries)
    side = trend(monitor) if direction is None else direction
    if side not in {'LONG', 'SHORT'}:
        return None, None, 'TREND_WAIT'
    reference = session_reference(entries)
    if reference is None:
        return None, None, 'SESSION_OR_VOLUME_UNAVAILABLE'
    origin, level, relative_volume = reference
    c, previous = entries[-1], entries[-2]
    long = side == 'LONG'
    confirmed = (previous.close <= level and c.low <= level and c.close > level and c.close > c.open
                 if long else previous.close >= level and c.high >= level and c.close < level and c.close < c.open)
    if not confirmed:
        return None, None, 'RECLAIM_WAIT'
    episode = (side, origin)  # Consumed even when the first reclaim fails volume or room.
    if relative_volume < RULES['relative_volume_minimum']:
        return None, episode, 'FIRST_RECLAIM_LOW_VOLUME'
    atr = scanner._atr(entries, 14)
    if not execution.positive(atr):
        return None, episode, 'INVALID_ATR'
    structure = entries[-RULES['local_structure_bars']:]
    stop = (min(b.low for b in structure) - .5 * atr if long
            else max(b.high for b in structure) + .5 * atr)
    extension = (max(b.high for b in entries[:-1]) - .25 * atr if long
                 else min(b.low for b in entries[:-1]) + .25 * atr)
    target = execution.net_target(c.close, stop, side)
    if not all(execution.positive(v) for v in (stop, extension, target)):
        return None, episode, 'INVALID_STRUCTURE'
    if not (target <= extension if long else target >= extension) and not math.isclose(target, extension, rel_tol=1e-12):
        return None, episode, 'NET_ROOM_BELOW_2R'
    identity = f'vwap-reclaim-v1:{contract.contract_name}:{side}:{origin}'
    stamp = c.time_ms + STEP
    return dict(key=MODEL + ':' + identity, model=MODEL, setup_id=identity,
                ticker=contract.contract_name, side=side, session_start_ms=origin,
                signal_candle_ms=c.time_ms, created_ms=stamp + 1, expires_ms=stamp + 4 * STEP,
                trigger=c.close, roll_level=level, stop=stop, target=target, extension_target=extension,
                relative_volume=relative_volume, atr_15m_at_signal=atr,
                trend_anchor_ms=monitor[-1].time_ms, status='PENDING', filled_ms=None,
                outcome_ms=None, final_net_r=None, mfe_r=0.0, mae_r=0.0,
                step_size=contract.step_size, min_order_size=contract.min_order_size,
                max_order_size=contract.max_order_size), episode, None


def pending_state(record, monitor, entries, *, direction=None):
    closed_clock(monitor, entries)
    side = trend(monitor) if direction is None else direction
    c, long = entries[-1], record['side'] == 'LONG'
    return dict(setup_id=record['setup_id'] if side == record['side'] else None,
                confirmation_color_ok=c.close > c.open if long else c.close < c.open,
                confirmation_level_ok=c.close > record['roll_level'] if long else c.close < record['roll_level'],
                retest_touched=any(b.low <= record['roll_level'] if long else b.high >= record['roll_level']
                                   for b in entries[-4:]),
                stop_valid=c.close > record['stop'] if long else c.close < record['stop'])


def evaluate(record, candles, state_at, *, end_ms):
    if record['model'] != MODEL or record['key'] != MODEL + ':' + record['setup_id']:
        raise ValueError('Invalid VWAP study identity')
    result = execution.evaluate(dict(record, model=execution.MODEL), candles, state_at, end_ms=end_ms)
    result['model'] = MODEL
    return result


def replay_market(contract, monitor, entries, *, start_ms, end_ms):
    if not 0 < start_ms < end_ms or start_ms % STEP or end_ms % STEP:
        raise ValueError('Invalid study window')
    for cs, interval, step in ((monitor, 'HOUR_4', STEP4), (entries, 'MINUTE_15', STEP)):
        if any(a.time_ms >= b.time_ms for a, b in zip(cs, cs[1:])) or any(
                c.contract_id != contract.contract_id or c.interval != interval or c.time_ms % step
                or not all(execution.positive(v) for v in (c.open, c.high, c.low, c.close))
                or not c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high
                or not isinstance(c.volume, (float, int)) or isinstance(c.volume, bool)
                or not math.isfinite(c.volume) or c.volume < 0 for c in cs):
            raise ValueError('Invalid market identity, grid, OHLC or volume')
    times4, times15 = [c.time_ms + STEP4 for c in monitor], [c.time_ms + STEP for c in entries]
    frames, trends = {}, {}

    def windows_at(stamp):
        if stamp not in frames:
            w4 = complete_window(monitor, bisect_right(times4, stamp + 1), WINDOW, STEP4, stamp + 1)
            w15 = complete_window(entries, bisect_right(times15, stamp + 1), WINDOW, STEP, stamp + 1)
            if w4 is None or w15 is None:
                frames[stamp] = None
            else:
                anchor = w4[-1].time_ms
                if anchor not in trends:
                    trends[anchor] = trend(w4)
                frames[stamp] = (w4, w15, trends[anchor])
        return frames[stamp]

    records, seen, unknown_sessions, excluded = [], set(), set(), Counter()
    valid = 0
    for stamp in range(start_ms // DAY * DAY - DAY, end_ms, STEP):
        frame = windows_at(stamp)
        if frame is None:
            unknown_sessions.update(((stamp - STEP) // DAY * DAY, stamp // DAY * DAY))
            if stamp >= start_ms:
                excluded['INCOMPLETE_INDICATOR_WINDOW'] += 1
            continue
        w4, w15, side = frame
        valid += stamp >= start_ms
        r, episode, reason = candidate(contract, w4, w15, direction=side)
        if episode is None:
            if stamp >= start_ms:
                excluded[reason] += 1
            continue
        if episode in seen:
            if stamp >= start_ms:
                excluded['ALREADY_CONSUMED_SESSION_RECLAIM'] += 1
            continue
        seen.add(episode)
        if episode[1] in unknown_sessions:
            if stamp >= start_ms:
                excluded['UNOBSERVED_SESSION_FIRST_RECLAIM'] += 1
            continue
        if stamp < start_ms:
            excluded['WARMUP_FIRST_RECLAIM'] += 1
            continue
        if r is None:
            excluded[reason] += 1
            continue
        records.append(r)
    results = []
    for r in records:
        def state_at(stamp):
            frame = windows_at(stamp)
            if frame is None:
                return None
            w4, w15, side = frame
            return pending_state(r, w4, w15, direction=side)
        results.append(evaluate(r, entries, state_at, end_ms=end_ms))
    return dict(records=results, valid_points=valid, exclusions=dict(excluded))


def stressed_metrics(records):
    rows = []
    fee, slip = .001, .0005
    for original in records:
        r = dict(original)
        if r['status'] in {'TP', 'SL'}:
            d = 1 if r['side'] == 'LONG' else -1
            entry, stop = r['nominal_fill'] * (1 + d * slip), r['stop'] * (1 - d * slip)
            risk = d * (entry - stop) + fee * (entry + stop)
            nominal_exit = r['exit_price'] / (1 - d * execution.SLIP)
            exit_price = nominal_exit * (1 - d * slip)
            r['final_net_r'] = (d * (exit_price - entry) - fee * (exit_price + entry)) / risk
        rows.append(r)
    return {k: v for k, v in control.metrics(rows).items() if k not in ('avg_mfe_r', 'avg_mae_r')}


def decision(reports):
    if not reports:
        return 'INSUFFICIENT_SAMPLE'
    p = json.loads(PROTOCOL.read_text())
    seen = set()
    for r in reports:
        role = r['role']
        if role in seen:
            raise ValueError('Repeated period is not an independent sample')
        seen.add(role)
        if role == 'development':
            period = p['development']
        elif role in {'validation_1', 'validation_2'}:
            period = p['validation_periods'][int(role[-1]) - 1]
        else:
            raise ValueError('Unknown review period')
        if (r['start_ms'], r['end_ms']) != (period['start_ms'], period['end_ms']):
            raise ValueError('Review does not describe an independent registered period')
    if any(not r['period_complete'] for r in reports):
        return 'CONTINUE_INSUFFICIENT_SAMPLE'
    if any(r['coverage']['failed_markets'] for r in reports):
        return 'BLOCKED_DATA_QUALITY'
    for r in reports:
        m = r['metrics'][MODEL]
        if m['resolved'] >= 20 and (m['avg_net_r'] <= 1e-12 or m['profit_factor'] != 'INF'
                                    and m['profit_factor'] <= 1 + 1e-12):
            return 'KILL_NO_LIVE_PROMOTION'
    validations = [r for r in reports if r['role'] in {'validation_1', 'validation_2'}]
    if len(validations) < 2 or any(r['metrics'][MODEL]['resolved'] < 50 for r in validations):
        return 'CONTINUE_INSUFFICIENT_SAMPLE'
    for r in validations:
        m, stress, p = r['metrics'][MODEL], r['stress_metrics'][MODEL], r['portfolios'][MODEL]
        if (not r['period_complete'] or m['win_rate'] < 40 or m['avg_net_r'] <= 0
                or m['profit_factor'] != 'INF' and m['profit_factor'] < 1.2
                or stress['avg_net_r'] <= 0 or stress['profit_factor'] != 'INF' and stress['profit_factor'] <= 1
                or r['comparison']['net_filled_count_difference'] <= 0
                or r['comparison']['capped_filled_count_difference'] <= 0):
            return 'PIVOT_NO_PRACTICAL_MARGIN'
        if p['resolved'] < 20:
            return 'CONTINUE_INSUFFICIENT_CAPITAL_SAMPLE'
        if p['closed_portfolio_roi_pct'] is None:
            return 'CONTINUE_ROI_UNVERIFIED'
        if p['closed_portfolio_roi_pct'] <= 0:
            return 'PIVOT_NO_CAPITAL_PROFIT'
    return 'LIVE_CAPTURE_SHADOW_REQUIRED'


def validation_gate(path, protocol):
    """Never open unused periods after a development KILL or changed evidence."""
    if path is None:
        raise ValueError('Validation requires the unchanged development report')
    r = json.loads(path.read_text())
    period = protocol['development']
    if (r['role'] != 'development' or (r['start_ms'], r['end_ms']) != (period['start_ms'], period['end_ms'])
            or r['protocol_sha256'] != hashlib.sha256(PROTOCOL.read_bytes()).hexdigest()
            or r['engine_sha256'] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            or r['source_manifest_sha256'] != period['source_manifest_sha256']):
        raise ValueError('Development provenance changed')
    for model in (*control.MODELS, MODEL):
        records = [row for row in r['records'] if row['model'] == model]
        if (control.metrics(records) != r['metrics'][model] or control.portfolio(records) != r['portfolios'][model]
                or stressed_metrics(records) != r['stress_metrics'][model]):
            raise ValueError('Development economics do not reproduce')
    if decision([r]) in {'KILL_NO_LIVE_PROMOTION', 'BLOCKED_DATA_QUALITY'}:
        raise ValueError('Unused validation stays sealed after negative or blocked development')


def build(source_dir, analyze, settings, *, role, baseline_path=None, development_path=None, now_ms=None):
    if (role == 'development') != (baseline_path is not None):
        raise ValueError('Development requires its original frozen comparator')
    p_raw = PROTOCOL.read_bytes()
    p = json.loads(p_raw)
    if (p['model'] != MODEL or any(p['rules'][k] != v for k, v in RULES.items())
            or p['production_parameters'] != strategy_parameters(settings)
            or p['cost_stress']['fee_bps_each_side'] != 10 or p['cost_stress']['slippage_bps_each_side'] != 5):
        raise ValueError('Implementation differs from registered protocol')
    for name, digest in p['frozen_dependencies_sha256'].items():
        if hashlib.sha256(PROTOCOL.with_name(name).read_bytes()).hexdigest() != digest:
            raise ValueError('Frozen control dependency changed')
    if role == 'development':
        period = p['development']
    elif role in {'validation_1', 'validation_2'}:
        validation_gate(development_path, p)
        period = p['validation_periods'][int(role[-1]) - 1]
    else:
        raise ValueError('Unregistered study role')
    raw = (source_dir / 'replay-report.json').read_bytes()
    source = json.loads(raw)
    start, end = source['start_ms'], source['end_ms']
    cutoff = int(time.time() * 1000) if now_ms is None else now_ms
    if (start, end) != (period['start_ms'], period['end_ms']) or end > cutoff // DAY * DAY:
        raise ValueError('Unregistered or incomplete period')
    if role == 'development' and hashlib.sha256(raw).hexdigest() != period['source_manifest_sha256']:
        raise ValueError('Frozen development source changed')
    manifest = source['manifest']
    if (source.get('dataset') != 'RETROSPECTIVE' or source.get('eligible_for_live_promotion') is not False
            or manifest['parameters'] != p['production_parameters'] or manifest['indicator_windows'] != p['indicator_windows']
            or manifest['rule_fingerprint'] not in {rule_fingerprint(analyze, settings),
                    '5c9e20d6b1deeba5ed2a77daef934e1d4ced10c6162bed1aaabe02eadb95ee2d'}
            or hashlib.sha256(json.dumps(manifest['universe'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                    != p['source_universe_sha256']):
        raise ValueError('Source rules, universe or windows changed')
    original = baseline_path.read_bytes() if baseline_path is not None else None
    if original and hashlib.sha256(original).hexdigest() not in {
            p['development']['control_ledger_sha256'], p['development']['published_control_ledger_sha256']}:
        raise ValueError('Frozen comparator changed')
    baseline = json.loads(original) if original else None
    if baseline:
        semantic = {k: v for k, v in baseline.items() if k != 'engine_sha256'}
        digest = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        if digest != p['development']['control_content_except_engine_provenance_sha256']:
            raise ValueError('Frozen comparator content changed')
    allowed = {f.name for f in fields(scanner.Contract)}
    universe = {c['contract_id']: scanner.Contract(**{k: v for k, v in c.items() if k in allowed}) for c in manifest['universe']}
    if len(universe) != len(manifest['universe']):
        raise ValueError('Duplicate universe contract')
    found, benchmark, records, exclusions = set(), [], [], Counter()
    valid = 0
    for item in manifest['sources']:
        path = (source_dir / item['file']).resolve()
        if item['ticker'] in found or not path.is_relative_to(source_dir.resolve()):
            raise ValueError('Duplicate or unsafe source')
        found.add(item['ticker'])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('Candle checksum changed')
        data = json.loads(raw)
        contract = scanner.Contract(**{k: v for k, v in data['contract'].items() if k in allowed})
        if contract.contract_name != item['ticker'] or universe.get(contract.contract_id) != contract:
            raise ValueError('Contract identity mismatch')
        monitor, entries = ([scanner.Candle(**c) for c in data[i]] for i in ('HOUR_4', 'MINUTE_15'))
        controls = control.replay_market(contract, monitor, entries, analyze, settings, start_ms=start, end_ms=end)
        benchmark.extend(controls['records'])
        result = replay_market(contract, monitor, entries, start_ms=start, end_ms=end)
        records.extend(result['records'])
        valid += result['valid_points']
        exclusions.update(result['exclusions'])
    failures = [f['ticker'] for f in manifest['failures']]
    if (len(set(failures)) != len(failures) or found & set(failures)
            or found | set(failures) != {c.contract_name for c in universe.values()}):
        raise ValueError('Universe coverage accounting mismatch')
    for model, archived in zip(control.MODELS[:2], ('current', 'shadow')):
        actual = [(r['setup_id'], r['created_ms'], r['trigger'], r['stop'], r['target']) for r in benchmark if r['model'] == model]
        expected = [(r['setup_id'], r['created_ms'], r['entry'], r['stop'], r['target']) for r in source['signals'][archived]]
        if len(set(actual)) != len(actual) or len(set(expected)) != len(expected) or set(actual) != set(expected):
            raise ValueError('Original first entries did not reproduce')
    if baseline and ({r['key']: r for r in benchmark} != {r['key']: r for r in baseline['records']}
                     or len(benchmark) != len(baseline['records']) or baseline_path.read_bytes() != original):
        raise ValueError('Original comparator ledger did not reproduce')
    models, all_records = (*control.MODELS, MODEL), benchmark + records
    metrics = {m: control.metrics([r for r in all_records if r['model'] == m]) for m in models}
    portfolios = {m: control.portfolio([r for r in all_records if r['model'] == m]) for m in models}
    if baseline and any(metrics[m] != baseline['metrics'][m] or portfolios[m] != baseline['portfolios'][m] for m in control.MODELS):
        raise ValueError('Original control economics changed')
    current = [r for r in benchmark if r['model'] == 'current_next_open' and r['filled_ms'] is not None]
    filled = [r for r in records if r['filled_ms'] is not None]
    identity = lambda r: (r['ticker'], r['side'], r['created_ms'] - 1)
    a, b = {identity(r) for r in current}, {identity(r) for r in filled}
    expected = (end - start) // STEP * len(universe)
    return dict(protocol=p['protocol'], model=MODEL, dataset='ISOLATED_REGISTERED_VWAP_REPLAY', role=role,
                start_ms=start, end_ms=end, period_complete=end - start == 7 * DAY,
                protocol_sha256=hashlib.sha256(p_raw).hexdigest(),
                engine_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                source_manifest_sha256=hashlib.sha256((source_dir / 'replay-report.json').read_bytes()).hexdigest(),
                baseline_report_sha256=hashlib.sha256(original).hexdigest() if original else None,
                source_files_verified=len(found), records=all_records, metrics=metrics, portfolios=portfolios,
                stress_metrics={m: stressed_metrics([r for r in all_records if r['model'] == m]) for m in models},
                coverage=dict(valid_points=valid, expected_points_all_markets=expected, requested_markets=len(universe),
                              fetched_markets=len(found), failed_markets=len(failures), coverage_pct=100 * valid / expected),
                exclusions=dict(exclusions), comparison=dict(shared_entry_timestamps=len(a & b),
                    proposal_only_entry_timestamps=len(b - a), current_missing_entry_timestamps=len(a - b),
                    net_filled_count_difference=len(filled) - len(current),
                    capped_filled_count_difference=portfolios[MODEL]['filled'] - portfolios['current_next_open']['filled'],
                    capped_shared_entry_timestamps=None), changes_live_rules=False, real_orders_enabled=False,
                automatic_promotion=False, eligible_for_live_promotion=False, limitations=p['limitations'])


def main():
    import argparse
    from analysis_terminal import server
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--development-report', type=Path)
    parser.add_argument('--role', choices=('development', 'validation_1', 'validation_2'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.source.resolve()) or args.baseline and args.output.resolve() == args.baseline.resolve():
        raise ValueError('Do not overwrite frozen inputs')
    result = build(args.source, server.analyze_contract, server.SETTINGS, role=args.role,
                   baseline_path=args.baseline, development_path=args.development_report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(json.dumps(dict(decision=decision([result]), metrics=result['metrics'][MODEL],
                         comparison=result['comparison'], coverage=result['coverage'])))


if __name__ == '__main__':
    main()
