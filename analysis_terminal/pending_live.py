"""Prospective observation ledger; simulated fills only, no alert/order transport."""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from analysis_terminal import pending_entry_replay as study
from analysis_terminal.break_retest_replay import stressed_metrics
from analysis_terminal.replay import complete_window, rule_fingerprint, strategy_parameters
from analysis_terminal.setups import setup_identity

STEP, DAY, WEEK = study.STEP, 86400000, 7*86400000
PROTOCOL = Path(__file__).with_name('pending_live_protocol.json')


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def initialize(conn, *, now_ms):
    conn.execute('CREATE TABLE IF NOT EXISTS pending_live_meta (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS pending_live_markets (ticker TEXT PRIMARY KEY, close_ms INTEGER NOT NULL, continuous_from_ms INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS pending_live_seen (signal_key TEXT PRIMARY KEY, reason TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS pending_live_states (ticker TEXT NOT NULL, close_ms INTEGER NOT NULL, setup_id TEXT, observed_ms INTEGER NOT NULL, PRIMARY KEY(ticker,close_ms))')
    conn.execute('CREATE TABLE IF NOT EXISTS pending_live_signals (signal_key TEXT PRIMARY KEY, ticker TEXT NOT NULL, payload TEXT NOT NULL, created_ms INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS pending_live_cycles (bucket_ms INTEGER PRIMARY KEY, observed_ms INTEGER NOT NULL, payload TEXT NOT NULL)')
    meta = dict(activated_ms=now_ms, capture_start_ms=(now_ms//DAY+1)*DAY,
                protocol_sha256=hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
                engine_sha256=hashlib.sha256(Path(study.__file__).read_bytes()).hexdigest(),
                rule_fingerprint=None, last_success_ms=None, last_error=None, last_cycle=None)
    conn.execute('INSERT OR IGNORE INTO pending_live_meta VALUES (1,?)', (_json(meta),))
    existing = _meta(conn)
    if 'coverage_started_ms' not in existing:
        existing['coverage_started_ms'] = now_ms
        _save_meta(conn,existing)


def _meta(conn):
    return json.loads(conn.execute('SELECT payload FROM pending_live_meta WHERE id=1').fetchone()[0])


def _save_meta(conn, meta):
    conn.execute('UPDATE pending_live_meta SET payload=? WHERE id=1', (_json(meta),))


def records(conn):
    return [json.loads(r[0]) for r in conn.execute('SELECT payload FROM pending_live_signals ORDER BY created_ms,signal_key')]


def _save(conn, record):
    conn.execute('UPDATE pending_live_signals SET payload=? WHERE signal_key=?', (_json(record), record['key']))


def _consume(conn, identity, reason):
    if identity:
        for model in study.MODELS:
            conn.execute('INSERT OR IGNORE INTO pending_live_seen VALUES (?,?)', (model+':'+identity, reason))


def _closed_window(contract, candles, interval, stamp):
    step = STEP if interval == 'MINUTE_15' else 16*STEP
    closed = [c for c in candles if c.time_ms+step <= stamp]
    if any(c.contract_id != contract.contract_id or c.contract_name != contract.contract_name
           or c.interval != interval or c.time_ms % step
           or not all(math.isfinite(v) and v > 0 for v in (c.open,c.high,c.low,c.close))
           or not c.low <= min(c.open,c.close) <= max(c.open,c.close) <= c.high for c in closed):
        return None
    if any(a.time_ms >= b.time_ms for a,b in zip(closed,closed[1:])):
        return None
    return complete_window(closed, len(closed), 180, step, stamp+1)


def _advance(conn, snapshots, contracts, *, end_ms):
    by_name = {c.contract_name:c for c in contracts.values()}
    for original in records(conn):
        if original['status'] not in {'PENDING','OPEN'}:
            continue
        contract = by_name.get(original['ticker'])
        cs = snapshots.get((contract.contract_id,'MINUTE_15'),[]) if contract else []
        cursor = original['next_candle_ms']
        closed = [c for c in cs if c.time_ms+STEP <= end_ms]
        duplicate_times = {stamp for stamp,count in Counter(c.time_ms for c in closed).items() if count>1}
        series = {c.time_ms:c for c in closed if c.time_ms not in duplicate_times}
        r = dict(original)
        while cursor+STEP <= end_ms and r['status'] in {'PENDING','OPEN'}:
            candle = series.get(cursor)
            if candle is not None and (candle.contract_id != contract.contract_id
                    or candle.contract_name != original['ticker'] or candle.interval != 'MINUTE_15'
                    or not all(math.isfinite(v) and v > 0 for v in (candle.open,candle.high,candle.low,candle.close))
                    or not candle.low <= min(candle.open,candle.close) <= max(candle.open,candle.close) <= candle.high):
                candle = None
            states = {}
            # Observations necessarily arrive after their close. For this candle,
            # use the prior close's state, captured strictly before its open.
            prior = conn.execute('SELECT setup_id,observed_ms FROM pending_live_states WHERE ticker=? AND close_ms=?', (r['ticker'],cursor-STEP)).fetchone()
            if prior is not None and prior[1] < cursor:
                states[cursor] = prior[0]
            if r['status'] == 'PENDING':
                adapted = dict(r, created_ms=cursor+1)
                r = study.evaluate(adapted, [candle] if candle else [], states, end_ms=cursor+STEP)
                r['created_ms'] = original['created_ms']
            else:
                r = study.evaluate(r, [candle] if candle else [], {}, start_ms=cursor, end_ms=cursor+STEP)
            cursor += STEP
        if r['status'] == 'PENDING' and r['expires_ms'] <= end_ms:
            r.update(status='EXPIRED',outcome_ms=r['expires_ms'])
        r['next_candle_ms'] = cursor
        _save(conn, r)


def cycle(conn, *, contracts, snapshots, analyze, settings, now_ms):
    meta = _meta(conn)
    p = json.loads(PROTOCOL.read_text())
    original_protocol = json.loads(study.PROTOCOL.read_text())
    if (hashlib.sha256(PROTOCOL.read_bytes()).hexdigest() != meta['protocol_sha256']
            or hashlib.sha256(Path(study.__file__).read_bytes()).hexdigest() != p['frozen_engine_sha256']
            or meta['engine_sha256'] != p['frozen_engine_sha256']
            or hashlib.sha256(study.PROTOCOL.read_bytes()).hexdigest() != p['frozen_research_protocol_sha256']
            or strategy_parameters(settings) != original_protocol['production_parameters']):
        raise ValueError('Frozen live-capture dependency or parameter mismatch')
    fingerprint = rule_fingerprint(analyze,settings)
    if meta['rule_fingerprint'] not in (None,fingerprint):
        raise ValueError('Live capture analyzer changed; do not mix cohorts')
    meta['rule_fingerprint'] = fingerprint
    stamp = now_ms//STEP*STEP
    _advance(conn, snapshots, contracts, end_ms=stamp)
    quality = Counter()
    for cid,contract in contracts.items():
        w4 = _closed_window(contract,snapshots.get((cid,'HOUR_4'),[]),'HOUR_4',stamp)
        w15 = _closed_window(contract,snapshots.get((cid,'MINUTE_15'),[]),'MINUTE_15',stamp)
        if w4 is None or w15 is None:
            quality['INCOMPLETE_INDICATOR_WINDOW'] += 1
            continue
        quality['VALID_INDICATOR_WINDOWS'] += 1
        row = analyze(contract,w4,w15,as_of_ms=stamp+1)
        identity = setup_identity(row)
        if identity:
            quality['IDENTIFIED_SETUP'] += 1
        if row.get('retest_touched') is True and row.get('confirmed') is True:
            quality['CONFIRMED_AFTER_RETEST'] += 1
            if study.candidate(row,study.MODEL,candle_ms=w15[-1].time_ms) is None:
                quality['CONFIRMED_NO_ELIGIBLE_NET_ENTRY'] += 1
        previous = conn.execute('SELECT close_ms,continuous_from_ms FROM pending_live_markets WHERE ticker=?', (contract.contract_name,)).fetchone()
        if previous and previous[0] >= stamp:
            quality['ALREADY_OBSERVED'] += 1
            continue
        baseline = (previous is None or previous[0]+STEP != stamp
                    or now_ms-stamp > p['rules']['max_capture_delay_ms']
                    or stamp < meta['capture_start_ms'])
        if baseline:
            _consume(conn,identity,'BASELINED_OR_MISSED_OBSERVATION')
            quality['BASELINED_OR_MISSED_OBSERVATION'] += 1
        else:
            quality['CONTIGUOUS_FRESH_OBSERVATION'] += 1
        continuous_from = stamp if baseline else previous[1]
        conn.execute('INSERT INTO pending_live_markets VALUES (?,?,?) ON CONFLICT(ticker) DO UPDATE SET close_ms=excluded.close_ms,continuous_from_ms=excluded.continuous_from_ms', (contract.contract_name,stamp,continuous_from))
        conn.execute('INSERT OR IGNORE INTO pending_live_states VALUES (?,?,?,?)', (contract.contract_name,stamp,identity,now_ms))
        for model in study.MODELS:
            r = study.candidate(row,model,candle_ms=w15[-1].time_ms)
            if r is None:
                continue
            quality['QUALIFIED_'+model] += 1
            inserted = conn.execute('INSERT OR IGNORE INTO pending_live_seen VALUES (?,?)', (r['key'],'FIRST_QUALIFICATION')).rowcount
            if not inserted:
                quality['ALREADY_CONSUMED_'+model] += 1
                continue
            breakout_close = row['breakout_time_ms']+16*STEP
            if baseline or breakout_close < meta['capture_start_ms'] or breakout_close <= continuous_from:
                quality['PRE_CAPTURE_SETUP'] += 1
                quality['EXCLUDED_SETUP_ORIGIN_'+model] += 1
                continue
            r.update(observed_ms=now_ms, execution_start_ms=stamp+STEP,
                     next_candle_ms=stamp+STEP, step_size=contract.step_size,
                     min_order_size=contract.min_order_size,max_order_size=contract.max_order_size,
                     dataset='LIVE_CAPTURE_HYPOTHETICAL', frozen_source=row,
                     cohort_start_ms=meta['capture_start_ms']+(stamp-meta['capture_start_ms'])//WEEK*WEEK)
            conn.execute('INSERT OR IGNORE INTO pending_live_signals VALUES (?,?,?,?)', (r['key'],r['ticker'],_json(r),r['created_ms']))
            quality['CAPTURED_'+model] += 1
    # Only four pending bars need states; outcome cursors do not reconstruct them.
    conn.execute('DELETE FROM pending_live_states WHERE close_ms < ?', (stamp-12*STEP,))
    meta.update(last_success_ms=now_ms,last_error=None,last_cycle=dict(quality),last_bucket_ms=stamp)
    conn.execute('INSERT OR IGNORE INTO pending_live_cycles VALUES (?,?,?)',
                 (stamp,now_ms,_json(dict(bucket_ms=stamp,observed_ms=now_ms,
                    requested_markets=len(contracts),valid_markets=quality['VALID_INDICATOR_WINDOWS'],
                    capture_delay_ms=now_ms-stamp,quality=dict(quality)))))
    _save_meta(conn,meta)


def record_error(conn, error_type):
    meta = _meta(conn)
    meta['last_error'] = error_type
    _save_meta(conn,meta)


def coverage(conn, *, meta, now_ms):
    """First recorded attempts only; absent history is never reconstructed."""
    origin = meta['capture_start_ms']
    overview = dict(total_recorded_buckets=conn.execute('SELECT COUNT(*) FROM pending_live_cycles').fetchone()[0],
                    latest_observations=[json.loads(r[0]) for r in conn.execute(
                        'SELECT payload FROM pending_live_cycles ORDER BY bucket_ms DESC LIMIT 20')])
    if now_ms < origin:
        return dict(status='WAITING_FOR_CAPTURE_START',started_ms=meta['coverage_started_ms'],cohorts=[],**overview)
    last = now_ms//STEP*STEP
    evidence = {r[0]:json.loads(r[1]) for r in conn.execute(
        'SELECT bucket_ms,payload FROM pending_live_cycles WHERE bucket_ms>=? AND bucket_ms<=?',(origin,last))}
    reports = []
    for start in range(origin,last+1,WEEK):
        end = min(last,start+WEEK-STEP)
        expected = list(range(start,end+1,STEP))
        found = [evidence[b] for b in expected if b in evidence]
        overdue = [b for b in expected if b not in evidence and b+120000 <= now_ms]
        unknown = [b for b in overdue if b < meta['coverage_started_ms']//STEP*STEP]
        quality = Counter()
        for item in found:quality.update(item['quality'])
        requested = sum(item['requested_markets'] for item in found)
        valid = sum(item['valid_markets'] for item in found)
        reports.append(dict(start_ms=start,end_ms=start+WEEK,period_complete=now_ms>=start+WEEK,
                            expected_buckets=len(expected),recorded_buckets=len(found),
                            overdue_missing_buckets=len(overdue),unrecorded_before_audit_buckets=len(unknown),
                            pending_current_buckets=len(expected)-len(found)-len(overdue),
                            bucket_coverage_pct=100*len(found)/len(expected),
                            requested_market_observations=requested,valid_market_observations=valid,
                            indicator_coverage_pct=100*valid/requested if requested else None,
                            late_recorded_buckets=sum(i['capture_delay_ms']>120000 for i in found),
                            quality=dict(quality),
                            status='OBSERVATION_HISTORY_INCOMPLETE' if overdue else
                                   'WAITING_FOR_OBSERVATION' if not found else 'OBSERVATIONS_RECORDED'))
    return dict(status='OBSERVATION_HISTORY_INCOMPLETE' if any(r['overdue_missing_buckets'] for r in reports) else
                'OBSERVATIONS_RECORDED' if evidence else 'WAITING_FOR_OBSERVATION',
                started_ms=meta['coverage_started_ms'],basis='FIRST_RECORDED_ATTEMPT_PER_BUCKET',cohorts=reports,**overview)


def review(conn, *, now_ms, limit=50):
    meta, rows = _meta(conn), records(conn)
    grouped = {}
    for r in rows:
        grouped.setdefault(r['cohort_start_ms'],[]).append(r)
    cohorts = []
    for start,cohort in sorted(grouped.items()):
        metrics = {m:study.metrics([r for r in cohort if r['model']==m]) for m in study.MODELS}
        portfolios = {m:study.portfolio([dict(r,created_ms=r['observed_ms']+1) for r in cohort if r['model']==m]) for m in study.MODELS}
        stress = {m:stressed_metrics([r for r in cohort if r['model']==m]) for m in study.MODELS}
        cohorts.append(dict(start_ms=start,end_ms=start+WEEK,period_complete=now_ms>=start+WEEK,
                            metrics=metrics,portfolios=portfolios,stress_metrics=stress,
                            net_filled_count_difference=metrics[study.MODEL]['filled']-metrics['current_next_open']['filled']))
    age = max(0,now_ms-meta['last_success_ms']) if meta['last_success_ms'] is not None else None
    status = ('PAUSED_ERROR' if meta['last_error'] else
              'WAITING_FOR_CAPTURE_START' if now_ms<meta['capture_start_ms'] else
              'WAITING_FOR_FIRST_OBSERVATION' if age is None else
              'STALE_OBSERVATION' if age>2*STEP else 'COLLECTING')
    continuous = {m:study.portfolio([dict(r,created_ms=r['observed_ms']+1)
                  for r in rows if r['model']==m]) for m in study.MODELS}
    return dict(protocol='pending_entry_live_capture_v1',mode='SHADOW_ONLY',dataset='LIVE_CAPTURE_HYPOTHETICAL',
                real_orders_enabled=False,automatic_promotion=False,eligible_for_live_promotion=False,
                notifications_enabled=False,current_entry_status=False,
                status=status,observation_age_seconds=age/1000 if age is not None else None,
                meta=meta,total_records=len(rows),cohorts=cohorts,latest=list(reversed(rows))[:limit],
                coverage=coverage(conn,meta=meta,now_ms=now_ms),
                cohort_portfolio_scope='INDEPENDENT_PERIOD_SIMULATION',
                continuous_portfolios=continuous,
                continuous_capped_filled_count_difference=continuous[study.MODEL]['filled']-continuous['current_next_open']['filled'],
                sample_status='INSUFFICIENT SAMPLE' if not cohorts or any(c['metrics'][study.MODEL]['resolved']<20 for c in cohorts) else 'REVIEW_REQUIRED',
                limitations=json.loads(PROTOCOL.read_text())['interpretation']['limitations'])
