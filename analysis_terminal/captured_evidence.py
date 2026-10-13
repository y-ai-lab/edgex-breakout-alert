"""All pre-fill proofs via frozen reviewers, in isolated RAM-only projections.

Never write to the capture DB or reconstruct an observation. Project at most
500 original signal rows at a time so the existing reviewer returns every proof.
The caller holds one read transaction across source, metadata and evidence.
"""
import hashlib
import sqlite3
from contextlib import closing

from analysis_terminal import captured_ledger, pending_evidence, vwap_evidence


def attach(conn, *, source, now_ms, records, proof_review):
    if source not in captured_ledger.SOURCES:
        raise captured_ledger.LedgerError(400, 'INVALID_LEDGER_REQUEST')
    reviewer = pending_evidence if source == 'pending' else vwap_evidence
    signal_table = source+'_live_signals'
    state_table = source+'_evidence_states'
    meta_tables = [source+'_evidence_meta']
    if source == 'vwap':
        meta_tables.append('vwap_live_meta')
    signals = [tuple(r) for r in conn.execute(
        f'SELECT * FROM {signal_table} ORDER BY created_ms,signal_key')]
    states = [tuple(r) for r in conn.execute(
        f'SELECT * FROM {state_table} ORDER BY signal_key,close_ms')]
    proofs = []
    with closing(sqlite3.connect(':memory:')) as projection:
        # Exact table schema and original rows; no capture/evaluator initialization.
        for table in [signal_table, state_table]+meta_tables:
            ddl = conn.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?',
                               ('table', table)).fetchone()
            if ddl is None:
                raise captured_ledger.LedgerError(503, 'LEDGER_DATA_UNVERIFIABLE')
            projection.execute(ddl[0])
            if table != signal_table:
                rows = states if table == state_table else [tuple(r) for r in conn.execute(f'SELECT * FROM {table}')]
                if rows:
                    projection.executemany(f'INSERT INTO {table} VALUES ({",".join("?" for _ in rows[0])})', rows)
        for offset in range(0, len(signals), 500):
            batch = signals[offset:offset+500]
            projection.execute(f'DELETE FROM {signal_table}')
            projection.executemany(f'INSERT INTO {signal_table} VALUES ({",".join("?" for _ in batch[0])})', batch)
            projection.commit()
            projection.execute('PRAGMA query_only = ON')
            try:
                part = reviewer.review(projection, now_ms=now_ms, limit=500)
            finally:
                projection.execute('PRAGMA query_only = OFF')
            proofs.extend(reversed(part['latest']))
    by_key = {p['key']: p for p in proofs}
    counts = {}
    for proof in proofs:
        counts[proof['status']] = counts.get(proof['status'], 0)+1
    summary = proof_review['summary']
    if (len(signals) != len(records) or len(by_key) != len(proofs)
            or len(proofs) != summary['total_proposal_records']
            or counts != summary['statuses']
            or len(states) != summary['stored_observations']
            or list(reversed(proofs))[:500] != proof_review['latest']
            or not set(by_key).issubset({r['key'] for r in records})):
        raise captured_ledger.LedgerError(503, 'LEDGER_DATA_UNVERIFIABLE')
    # Even unused/invalid/orphan stored states bind the cursor; never hide updates
    # simply because the frozen reviewer still reports the same missing status.
    state_sha = hashlib.sha256(captured_ledger._canonical(states)).hexdigest()
    rows = [dict(r, prefill_observation_evidence=by_key.get(r['key'])) for r in records]
    scope = dict(source=source, total_evidence_records=len(proofs),
                 stored_evidence_sha256=state_sha,
                 absent_evidence_meaning='NOT_APPLICABLE_TO_COMPARISON_MODEL',
                 reviewer='FROZEN_REVIEWER_ON_ORIGINAL_ROWS',
                 historical_states_reconstructed=False, new_independent_sample=False)
    return rows, scope
