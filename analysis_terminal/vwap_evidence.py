"""Append-only future VWAP pre-fill states; never used by strategy or orders."""
import hashlib
import json

from analysis_terminal import vwap_live as live, vwap_reclaim_replay as study
from analysis_terminal.pending_evidence import FIELDS

STEP = live.STEP
ORIGIN = ('activated_ms', 'capture_start_ms', 'engine_sha256', 'protocol_sha256')


def identity_hash(record):
    return digest(live.encode({k: record[k] for k in FIELDS+('roll_level',)}))


def meta(conn):
    return json.loads(conn.execute('SELECT payload FROM vwap_evidence_meta WHERE id=1').fetchone()[0])


def save_meta(conn, value):
    conn.execute('UPDATE vwap_evidence_meta SET payload=? WHERE id=1', (live.encode(value),))


def digest(payload):
    return hashlib.sha256(payload.encode()).hexdigest()


def initialize(conn, *, now_ms):
    conn.execute('CREATE TABLE IF NOT EXISTS vwap_evidence_meta(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)')
    conn.execute('''CREATE TABLE IF NOT EXISTS vwap_evidence_states(
        signal_key TEXT NOT NULL, close_ms INTEGER NOT NULL, observed_ms INTEGER NOT NULL,
        recorded_ms INTEGER NOT NULL, identity_sha256 TEXT NOT NULL,
        state_sha256 TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(signal_key,close_ms))''')
    source = live.meta(conn)
    value = dict(started_ms=now_ms, capture_from_bucket_ms=(now_ms//STEP+1)*STEP,
                 source_origin={k: source[k] for k in ORIGIN}, last_success_ms=None, last_error=None)
    conn.execute('INSERT OR IGNORE INTO vwap_evidence_meta VALUES(1,?)', (live.encode(value),))


def capture(conn, *, observation_ms, recorded_ms):
    m = meta(conn); stamp = observation_ms//STEP*STEP
    if stamp < m['capture_from_bucket_ms']:
        return
    if {k: live.meta(conn)[k] for k in ORIGIN} != m['source_origin']:
        raise ValueError('Source capture origin changed')
    cycle = conn.execute('SELECT observed_ms FROM vwap_live_cycles WHERE bucket_ms=?', (stamp,)).fetchone()
    if cycle is None or cycle[0] != observation_ms:
        return  # A same-bucket retry is not a new first observation.
    if not observation_ms <= recorded_ms < stamp+STEP:
        raise ValueError('Proof must precede the executable bar')
    for r in live.records(conn):
        if (r['model'] != study.MODEL or r['status'] != 'PENDING' or
                not r['execution_start_ms'] <= stamp+STEP < r['expires_ms']):
            continue
        state = conn.execute('SELECT observed_ms,payload FROM vwap_live_states WHERE signal_key=? AND close_ms=?',
                             (r['key'], stamp)).fetchone()
        if state is None or state[0] != observation_ms:
            continue
        payload = state[1]  # Preserve the actual observed state, including null/false.
        json.loads(payload)
        values = (r['key'], stamp, state[0], recorded_ms, identity_hash(r), digest(payload), payload)
        previous = conn.execute('SELECT * FROM vwap_evidence_states WHERE signal_key=? AND close_ms=?',
                                (r['key'], stamp)).fetchone()
        if previous is not None:
            if tuple(previous)[:3]+tuple(previous)[4:] != values[:3]+values[4:]:
                raise ValueError('Conflicting first observation proof')
            continue
        conn.execute('INSERT INTO vwap_evidence_states VALUES(?,?,?,?,?,?,?)', values)
    m.update(last_success_ms=observation_ms, last_error=None)
    save_meta(conn, m)


def record_error(conn, error_type):
    m = meta(conn); m['last_error'] = error_type; save_meta(conn, m)


def review(conn, *, now_ms, limit=50):
    m = meta(conn); observations = {}
    origin_matches = {k: live.meta(conn)[k] for k in ORIGIN} == m['source_origin']
    for row in conn.execute('SELECT * FROM vwap_evidence_states ORDER BY close_ms'):
        key, stamp, observed, recorded, identity, checksum, payload = tuple(row)
        observations.setdefault(key, {})[stamp] = dict(close_ms=stamp, observed_ms=observed,
            recorded_ms=recorded, identity_sha256=identity, state_sha256=checksum, payload=payload)
    records, counts = [], {}
    for r in live.records(conn):
        if r['model'] != study.MODEL:
            continue
        end = min(r['next_candle_ms'], now_ms//STEP*STEP, r['expires_ms'])
        if r['filled_ms'] is not None:
            end = min(end, r['filled_ms']+STEP)
        expected = list(range(r['execution_start_ms'], end, STEP))
        available = []; before = missing = invalid = 0
        for candle_ms in expected:
            proof = observations.get(r['key'], {}).get(candle_ms-STEP)
            if proof is None:
                if candle_ms-STEP < m['capture_from_bucket_ms']: before += 1
                else: missing += 1
            elif (proof['identity_sha256'] != identity_hash(r) or
                  proof['state_sha256'] != digest(proof['payload']) or
                  not candle_ms-STEP <= proof['observed_ms'] <= proof['recorded_ms'] < candle_ms or
                  proof['close_ms'] < m['capture_from_bucket_ms']): invalid += 1
            else:
                available.append({k: v for k, v in proof.items() if k != 'payload'} |
                                 {'observed_state': json.loads(proof['payload'])})
        status = ('NO_CLOSED_ELIGIBLE_BAR_YET' if not expected else 'EVIDENCE_MISMATCH' if invalid else
                  'MISSING_OBSERVATION_EVIDENCE' if missing else 'UNAVAILABLE_BEFORE_AUDIT' if before else
                  'COMPLETE_OBSERVATION_EVIDENCE')
        counts[status] = counts.get(status, 0)+1
        records.append(dict(key=r['key'], status=status, expected_observations=len(expected),
            verified_observations=len(available), unavailable_before_audit=before,
            missing_observations=missing, invalid_observations=invalid, observations=available))
    age = None if m['last_success_ms'] is None else now_ms-m['last_success_ms']
    return dict(protocol='vwap_prefill_observation_evidence_v1', meta=m,
        source_origin_matches=origin_matches,
        status='SOURCE_ORIGIN_MISMATCH' if not origin_matches else 'PAUSED_ERROR' if m['last_error'] else
               'WAITING_FOR_AUDIT_START' if now_ms < m['capture_from_bucket_ms'] else
               'STALE_OBSERVATION' if age is not None and (age < 0 or age > 2*STEP) else
               'COLLECTING' if age is not None else 'WAITING_FOR_FIRST_OBSERVATION',
        summary=dict(total_proposal_records=len(records), statuses=counts,
                     stored_observations=sum(len(v) for v in observations.values())),
        latest=list(reversed(records))[:max(0, min(limit, 500))],
        existing_performance_modified=False, historical_states_reconstructed=False,
        actual_execution_evidence=False, automatic_promotion=False,
        limitations=['Only future states saved before their executable bar are evidence.',
                     'Original missing proof remains unavailable; no source outcomes are revised.',
                     'Complete observation proof is not actual execution or profitability evidence.'])
