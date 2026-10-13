"""Synthetic all-record proof audit, frozen semantics and unchanged source DB."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from analysis_terminal import captured_ledger as pages, server
from analysis_terminal import pending_live, pending_evidence, vwap_live, vwap_evidence
from analysis_terminal import test_captured_ledger as fixtures


class EvidenceAuditTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    tearDown = fixtures.ApiTests.tearDown
    seed = fixtures.ApiTests.seed

    def get(self, source='pending', **params):
        return self.client.get('/api/captured-shadow-ledger',
            params=dict(source=source, include_evidence=True, **params))

    def proofs(self, source, variant='valid'):
        engine, evidence = (pending_live, pending_evidence) if source == 'pending' else (vwap_live, vwap_evidence)
        with server._db_connect() as c:
            for r in engine.records(c):
                for candle in range(r['execution_start_ms'], r['expires_ms'], engine.STEP):
                    stamp = candle-engine.STEP
                    identity = evidence.identity_hash(r) if variant == 'valid' else 'invalid-synthetic-hash'
                    if source == 'pending':
                        c.execute('INSERT INTO pending_evidence_states VALUES(?,?,?,?,?,?)',
                            (r['key'], stamp, stamp+10, stamp+20, None, identity))
                    else:
                        payload = json.dumps(dict(pending_ok=False, observed_setup_id=None))
                        c.execute('INSERT INTO vwap_evidence_states VALUES(?,?,?,?,?,?,?)',
                            (r['key'], stamp, stamp+10, stamp+20, identity, evidence.digest(payload), payload))

    def test_all_721_proofs_match_frozen_review_and_keep_all_metrics(self):
        for source in pages.SOURCES:
            self.seed(source); self.proofs(source)
            legacy = self.client.get('/api/'+source+'-entry-evidence?limit=500').json()
            ordinary = self.client.get('/api/captured-shadow-ledger', params=dict(source=source, limit=500)).json()
            first = self.get(source, limit=500).json()
            second = self.get(source, limit=500, cursor=first['ledger_page']['next_cursor']).json()
            rows = first['latest']+second['latest']
            self.assertEqual(len(rows), 721)
            self.assertEqual(len({r['key'] for r in rows}), 721)
            self.assertEqual(first['evidence_audit']['total_evidence_records'], 721)
            self.assertEqual([r['prefill_observation_evidence'] for r in reversed(rows)][:500], legacy['latest'])
            self.assertEqual(first['observation_evidence'], {k:v for k,v in legacy.items() if k != 'latest'})
            for r in rows:
                self.assertEqual(r['prefill_observation_evidence']['status'], 'COMPLETE_OBSERVATION_EVIDENCE')
                if source == 'vwap':
                    self.assertEqual(r['prefill_observation_evidence']['observations'][0]['observed_state'],
                        dict(pending_ok=False, observed_setup_id=None))
            base = {k:v for k,v in first.items() if k not in ('ledger_page','evidence_audit')}; base['latest'] = []
            control = {k:v for k,v in ordinary.items() if k != 'ledger_page'}; control['latest'] = []
            self.assertEqual(base, control)
            review = {k:v for k,v in first.items() if k != 'ledger_page'}; review['latest'] = []
            expected = hashlib.sha256(pages._canonical(dict(source=source+'-evidence', review=review, records=rows))).hexdigest()
            self.assertEqual(expected, first['ledger_page']['snapshot_sha256'])
            self.assertEqual(expected, second['ledger_page']['snapshot_sha256'])

    def test_original_db_bytes_schema_rows_and_legacy_output_are_unchanged(self):
        for source in pages.SOURCES:
            self.seed(source, 3); self.proofs(source)
        before = self.path.read_bytes()
        with server._db_connect() as c: dump = list(c.iterdump())
        for source in pages.SOURCES:
            legacy = self.client.get('/api/'+source+'-entry-evidence?limit=500').json()
            first = self.get(source, limit=2).json()
            self.assertEqual(self.get(source, limit=2, cursor=first['ledger_page']['next_cursor']).status_code, 200)
            self.assertEqual(legacy, self.client.get('/api/'+source+'-entry-evidence?limit=500').json())
        self.assertEqual(before, self.path.read_bytes())
        with server._db_connect() as c: self.assertEqual(dump, list(c.iterdump()))

    def test_missing_before_audit_invalid_and_closed_time_semantics_match(self):
        for source in pages.SOURCES:
            self.seed(source, 6)
            engine, evidence = (pending_live,pending_evidence) if source == 'pending' else (vwap_live,vwap_evidence)
            with server._db_connect() as c:
                records = engine.records(c)
                for i, r in enumerate(records):
                    if i == 1:
                        r['execution_start_ms'] -= engine.STEP
                        r['next_candle_ms'] = r['execution_start_ms']+engine.STEP
                    if i == 2: r['next_candle_ms'] = r['execution_start_ms']
                    if i == 3: r['filled_ms'] = r['execution_start_ms']
                    if i == 4: r['next_candle_ms'] += 20*engine.STEP
                    c.execute(f'UPDATE {source}_live_signals SET payload=? WHERE signal_key=?', (json.dumps(r),r['key']))
                legacy = evidence.review(c, now_ms=int(server.time.time()*1000), limit=500)
            result = self.get(source).json()
            actual = [r['prefill_observation_evidence'] for r in reversed(result['latest'])]
            self.assertEqual(actual, legacy['latest'])
            filled = next(p for p in actual if p['key'].endswith('0003'))
            self.assertEqual(filled['expected_observations'], 1)  # filled bar only
            self.assertIn('UNAVAILABLE_BEFORE_AUDIT', legacy['summary']['statuses'])
            self.assertIn('NO_CLOSED_ELIGIBLE_BAR_YET', legacy['summary']['statuses'])

    def test_invalid_proof_is_not_reconstructed_or_promoted(self):
        for source in pages.SOURCES:
            self.seed(source, 3); self.proofs(source, 'invalid')
            legacy = self.client.get('/api/'+source+'-entry-evidence?limit=500').json()
            result = self.get(source).json()
            self.assertEqual([r['prefill_observation_evidence'] for r in reversed(result['latest'])], legacy['latest'])
            self.assertTrue(all(r['prefill_observation_evidence']['status'] == 'EVIDENCE_MISMATCH' for r in result['latest']))
            self.assertFalse(result['evidence_audit']['historical_states_reconstructed'])
            self.assertFalse(result['evidence_audit']['new_independent_sample'])

    def test_late_observation_recording_checksum_and_old_stamp_keep_frozen_errors(self):
        for source in pages.SOURCES:
            self.seed(source,1); self.proofs(source)
            evidence = pending_evidence if source=='pending' else vwap_evidence
            updates=[('observed_ms',self.start+pending_live.STEP),
                     ('recorded_ms',self.start+pending_live.STEP),
                     ('observed_ms',self.start-1)]
            if source=='vwap': updates.append(('state_sha256','incorrect-hash'))
            with server._db_connect() as c:
                original=list(c.execute(f'SELECT * FROM {source}_evidence_states'))
            for column,value in updates:
                with server._db_connect() as c:
                    c.execute(f'DELETE FROM {source}_evidence_states')
                    c.executemany(f'INSERT INTO {source}_evidence_states VALUES ({",".join("?" for _ in original[0])})',original)
                    c.execute(f'UPDATE {source}_evidence_states SET {column}=? WHERE close_ms=?',(value,self.start))
                    expected=evidence.review(c,now_ms=int(server.time.time()*1000),limit=500)
                actual=self.get(source).json()
                self.assertEqual([r['prefill_observation_evidence'] for r in reversed(actual['latest'])],expected['latest'])
                self.assertEqual(expected['latest'][0]['status'],'EVIDENCE_MISMATCH')

    def test_invalid_or_orphan_state_update_with_unchanged_status_still_rejects_cursor(self):
        for source in pages.SOURCES:
            self.seed(source, 3); self.proofs(source, 'invalid')
            first = self.get(source, limit=2).json()
            with server._db_connect() as c:
                c.execute(f'UPDATE {source}_evidence_states SET identity_sha256=?', ('different-invalid-hash',))
            response = self.get(source, limit=2, cursor=first['ledger_page']['next_cursor'])
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['detail'], 'LEDGER_CHANGED_RESTART_REQUIRED')

    def test_evidence_mode_and_plain_mode_cursors_cannot_be_mixed(self):
        self.seed('pending', 3)
        plain = self.client.get('/api/captured-shadow-ledger',params=dict(source='pending',limit=2)).json()
        evidence = self.get(limit=2).json()
        self.assertEqual(self.get(limit=2,cursor=plain['ledger_page']['next_cursor']).status_code,400)
        self.assertEqual(self.client.get('/api/captured-shadow-ledger',params=dict(source='pending',limit=2,
            cursor=evidence['ledger_page']['next_cursor'])).status_code,400)

    def test_empty_audit_and_comparison_models_have_no_fabricated_proof(self):
        self.assertEqual(self.get().json()['evidence_audit']['total_evidence_records'],0)
        self.seed('pending',1)
        with server._db_connect() as c:
            r = pending_live.records(c)[0]; r['model'] = 'current_next_open'
            c.execute('UPDATE pending_live_signals SET payload=?',(json.dumps(r),))
        result = self.get().json()
        self.assertIsNone(result['latest'][0]['prefill_observation_evidence'])
        self.assertEqual(result['evidence_audit']['total_evidence_records'],0)

    def test_wal_commit_mid_read_keeps_source_and_proof_in_same_snapshot(self):
        self.seed('pending',3); self.proofs('pending')
        with server._db_connect() as c: c.execute('PRAGMA journal_mode=WAL')
        original = pending_live.review
        def update(conn, **kwargs):
            result = original(conn, **kwargs)
            with server._db_connect() as writer:
                writer.execute('UPDATE pending_evidence_states SET identity_sha256=?',('later-invalid',))
            return result
        with patch.object(pending_live,'review',side_effect=update): first = self.get(limit=2).json()
        self.assertTrue(all(r['prefill_observation_evidence']['status']=='COMPLETE_OBSERVATION_EVIDENCE' for r in first['latest']))
        self.assertEqual(self.get(limit=2,cursor=first['ledger_page']['next_cursor']).status_code,409)

    def test_corrupt_state_is_safe_error_and_source_db_is_query_only(self):
        self.seed('vwap',1); self.proofs('vwap')
        with server._db_connect() as c:
            payload='synthetic-private-invalid'; checksum=vwap_evidence.digest(payload)
            c.execute('UPDATE vwap_evidence_states SET payload=?,state_sha256=?',(payload,checksum))
        response = self.get('vwap')
        self.assertEqual(response.status_code,503); self.assertNotIn('synthetic-private',response.text)
        def writer(conn, **kwargs): conn.execute('DELETE FROM pending_live_signals')
        with patch.object(server.captured_evidence,'attach',side_effect=writer): response = self.get()
        self.assertEqual(response.status_code,503)

    def test_vwap_origin_mismatch_remains_blocked(self):
        self.seed('vwap',1)
        with server._db_connect() as c:
            meta = vwap_evidence.meta(c); meta['source_origin']['capture_start_ms']+=1
            vwap_evidence.save_meta(c,meta)
        result=self.get('vwap').json()
        self.assertEqual(result['observation_evidence']['status'],'SOURCE_ORIGIN_MISMATCH')
        self.assertFalse(result['observation_evidence']['source_origin_matches'])

    def test_summary_inconsistency_fails_closed(self):
        self.seed('pending',3)
        original = pending_evidence.review
        def inconsistent(conn,**kwargs):
            value=original(conn,**kwargs)
            if kwargs.get('limit')==500 and conn.execute('PRAGMA query_only').fetchone()[0] and conn.execute('PRAGMA database_list').fetchone()[2]:
                value=deepcopy(value);value['summary']['total_proposal_records']+=1
            return value
        with patch.object(pending_evidence,'review',side_effect=inconsistent): response=self.get()
        self.assertEqual(response.status_code,503)
