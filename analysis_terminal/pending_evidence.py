"""Append-only proof of future pre-fill observations, isolated from execution."""
import hashlib
import json

from analysis_terminal import pending_entry_replay as study

STEP = study.STEP
FIELDS = ('key','model','setup_id','ticker','side','created_ms','observed_ms',
          'execution_start_ms','expires_ms','trigger','stop','target')


def identity_hash(record):
    return hashlib.sha256(json.dumps({k:record[k] for k in FIELDS},
                                    sort_keys=True,allow_nan=False).encode()).hexdigest()


def meta(conn):
    return json.loads(conn.execute('SELECT payload FROM pending_evidence_meta WHERE id=1').fetchone()[0])


def save_meta(conn,value):
    conn.execute('UPDATE pending_evidence_meta SET payload=? WHERE id=1',
                 (json.dumps(value,sort_keys=True,allow_nan=False),))


def initialize(conn, *, now_ms):
    conn.execute('CREATE TABLE IF NOT EXISTS pending_evidence_meta(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)')
    conn.execute('''CREATE TABLE IF NOT EXISTS pending_evidence_states(
        signal_key TEXT NOT NULL, close_ms INTEGER NOT NULL, observed_ms INTEGER NOT NULL,
        recorded_ms INTEGER NOT NULL, setup_id TEXT, identity_sha256 TEXT NOT NULL,
        PRIMARY KEY(signal_key,close_ms))''')
    value=dict(started_ms=now_ms,capture_from_bucket_ms=(now_ms//STEP+1)*STEP,
               last_success_ms=None,last_error=None)
    conn.execute('INSERT OR IGNORE INTO pending_evidence_meta VALUES(1,?)',
                 (json.dumps(value,sort_keys=True),))


def capture(conn, *, observation_ms, recorded_ms):
    """Copy only this cycle's NEW state, committed before its future bar opens."""
    m=meta(conn);stamp=observation_ms//STEP*STEP
    if stamp<m['capture_from_bucket_ms']:
        return
    if not observation_ms<=recorded_ms<stamp+STEP:
        raise ValueError('Observation proof must be recorded before the executable bar')
    rows=conn.execute('SELECT payload FROM pending_live_signals').fetchall()
    for row in rows:
        r=json.loads(row[0])
        if (r['model']!=study.MODEL or r['status']!='PENDING' or
                not r['execution_start_ms']<=stamp+STEP<r['expires_ms']):
            continue
        state=conn.execute('SELECT setup_id,observed_ms FROM pending_live_states WHERE ticker=? AND close_ms=?',
                           (r['ticker'],stamp)).fetchone()
        if state is None or state[1]!=observation_ms:
            continue  # Old, missing or retried observations cannot become proof.
        values=(r['key'],stamp,state[1],recorded_ms,state[0],identity_hash(r))
        existing=conn.execute('SELECT signal_key,close_ms,observed_ms,recorded_ms,setup_id,identity_sha256 FROM pending_evidence_states WHERE signal_key=? AND close_ms=?',
                              (r['key'],stamp)).fetchone()
        if existing is not None:
            # A retry does not change the original recording time or source.
            if tuple(existing)[:3]+tuple(existing)[4:] != values[:3]+values[4:]:
                raise ValueError('Conflicting first observation proof')
            continue
        conn.execute('INSERT INTO pending_evidence_states VALUES(?,?,?,?,?,?)',values)
    m.update(last_success_ms=observation_ms,last_error=None)
    save_meta(conn,m)


def record_error(conn,error_type):
    m=meta(conn);m['last_error']=error_type;save_meta(conn,m)


def review(conn, *, now_ms, limit=50):
    m=meta(conn)
    signals=[json.loads(r[0]) for r in conn.execute('SELECT payload FROM pending_live_signals ORDER BY created_ms,signal_key')]
    observations={}
    for row in conn.execute('SELECT signal_key,close_ms,observed_ms,recorded_ms,setup_id,identity_sha256 FROM pending_evidence_states ORDER BY close_ms'):
        key,stamp,observed,recorded,setup,digest=tuple(row)
        observations.setdefault(key,{})[stamp]=dict(close_ms=stamp,observed_ms=observed,
            recorded_ms=recorded,observed_setup_id=setup,identity_sha256=digest)
    records=[];counts={}
    for r in signals:
        if r['model']!=study.MODEL:
            continue
        end=min(r['next_candle_ms'],now_ms//STEP*STEP,r['expires_ms'])
        if r['filled_ms'] is not None:end=min(end,r['filled_ms']+STEP)
        expected=list(range(r['execution_start_ms'],end,STEP))
        available=[];missing=before=invalid=0
        for candle_ms in expected:
            proof=observations.get(r['key'],{}).get(candle_ms-STEP)
            if proof is None:
                if candle_ms-STEP<m['capture_from_bucket_ms']:before+=1
                else:missing+=1
            elif (proof['identity_sha256']!=identity_hash(r) or
                  not candle_ms-STEP<=proof['observed_ms']<=proof['recorded_ms']<candle_ms or
                  proof['close_ms']<m['capture_from_bucket_ms']):invalid+=1
            else:available.append(proof)
        status=('NO_CLOSED_ELIGIBLE_BAR_YET' if not expected else 'EVIDENCE_MISMATCH' if invalid else
                'MISSING_OBSERVATION_EVIDENCE' if missing else 'UNAVAILABLE_BEFORE_AUDIT' if before else
                'COMPLETE_OBSERVATION_EVIDENCE')
        counts[status]=counts.get(status,0)+1
        records.append(dict(key=r['key'],status=status,expected_observations=len(expected),
                            verified_observations=len(available),unavailable_before_audit=before,
                            missing_observations=missing,invalid_observations=invalid,observations=available))
    return dict(protocol='pending_prefill_observation_evidence_v1',meta=m,
                status='PAUSED_ERROR' if m['last_error'] else
                       'WAITING_FOR_AUDIT_START' if now_ms<m['capture_from_bucket_ms'] else
                       'COLLECTING' if m['last_success_ms'] is not None else 'WAITING_FOR_FIRST_OBSERVATION',
                summary=dict(total_proposal_records=len(records),statuses=counts,
                             stored_observations=sum(len(v) for v in observations.values())),
                latest=list(reversed(records))[:max(0,min(limit,500))],
                existing_performance_modified=False,historical_states_reconstructed=False,
                actual_execution_evidence=False,automatic_promotion=False,
                limitations=['Observation availability does not prove a profitable trade or an actual fill.',
                             'Only future states recorded before their executable bar are evidence.',
                             'Old missing proof remains unavailable; source outcomes and capital stay unchanged.'])
