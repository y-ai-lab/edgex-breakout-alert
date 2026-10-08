"""Future observation provenance, migrations and isolation from source outcomes."""
from copy import deepcopy
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from analysis_terminal import pending_evidence as evidence,pending_live as live,server
from analysis_terminal import test_pending_live as fixtures
from analysis_terminal.test_pending_entry_replay import candle

STEP,START=fixtures.STEP,fixtures.START


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source=fixtures.PendingLiveTests();self.source.setUp()
        self.conn=self.source.conn
        evidence.initialize(self.conn,now_ms=START-STEP+1)

    def tearDown(self):self.source.tearDown()

    def collect(self,stamp):
        evidence.capture(self.conn,observation_ms=stamp+1000,recorded_ms=stamp+1100)

    def record(self):return live.records(self.conn)[0]

    def proof(self,now=START+3*STEP,limit=50):return evidence.review(self.conn,now_ms=now,limit=limit)

    def test_future_proof_precedes_eligible_bar_and_keeps_candidate_hash(self):
        r=self.source.capture();self.collect(START)
        self.source.cycle(START+STEP);self.collect(START+STEP)
        self.source.cycle(START+2*STEP)
        out=self.proof()['latest'][0]
        self.assertEqual(out['status'],'COMPLETE_OBSERVATION_EVIDENCE')
        self.assertEqual(out['expected_observations'],1)
        proof=out['observations'][0]
        self.assertLess(proof['recorded_ms'],r['execution_start_ms'])
        self.assertEqual(proof['identity_sha256'],evidence.identity_hash(r))

    def test_redeploy_preserves_audit_frontier_and_original_activation(self):
        original=live._meta(self.conn);audit=evidence.meta(self.conn)
        evidence.initialize(self.conn,now_ms=START+100*STEP)
        self.assertEqual(evidence.meta(self.conn),audit)
        self.assertEqual(live._meta(self.conn),original)

    def test_mid_bucket_start_never_backfills_existing_observations(self):
        self.source.capture()
        self.conn.execute('DELETE FROM pending_evidence_meta')
        evidence.initialize(self.conn,now_ms=START+2000)
        self.collect(START)
        self.source.cycle(START+STEP)
        self.source.cycle(START+2*STEP)
        result=self.proof()['latest'][0]
        self.assertEqual(result['status'],'UNAVAILABLE_BEFORE_AUDIT')
        self.assertEqual(result['verified_observations'],0)

    def test_same_bucket_retry_retains_first_timestamp(self):
        self.source.capture();self.collect(START)
        first=list(self.conn.execute('SELECT * FROM pending_evidence_states'))
        evidence.capture(self.conn,observation_ms=START+1000,recorded_ms=START+2000)
        self.assertEqual(list(self.conn.execute('SELECT * FROM pending_evidence_states')),first)
        evidence.capture(self.conn,observation_ms=START+3000,recorded_ms=START+3100)
        self.assertEqual(list(self.conn.execute('SELECT * FROM pending_evidence_states')),first)

    def test_recorded_after_bar_open_cannot_claim_prior_evidence(self):
        self.source.capture()
        with self.assertRaises(ValueError):
            evidence.capture(self.conn,observation_ms=START+1000,recorded_ms=START+STEP)
        self.assertEqual(self.proof()['summary']['stored_observations'],0)

    def test_missing_after_frontier_is_a_quality_gap_not_legacy_or_success(self):
        self.source.capture();self.source.cycle(START+STEP);self.source.cycle(START+2*STEP)
        out=self.proof()['latest'][0]
        self.assertEqual(out['status'],'MISSING_OBSERVATION_EVIDENCE')
        self.assertEqual(out['missing_observations'],1)

    def test_unclosed_eligible_bar_is_not_zero_verified_trade(self):
        self.source.capture();self.collect(START)
        out=self.proof(now=START+STEP+1000)['latest'][0]
        self.assertEqual(out['status'],'NO_CLOSED_ELIGIBLE_BAR_YET')
        self.assertEqual(out['expected_observations'],0)

    def test_hash_drift_and_conflicting_state_fail_without_overwriting(self):
        self.source.capture();self.collect(START)
        first=list(self.conn.execute('SELECT * FROM pending_evidence_states'))
        self.conn.execute('UPDATE pending_live_states SET setup_id=NULL WHERE close_ms=?',(START,))
        with self.assertRaises(ValueError):self.collect(START)
        self.assertEqual(list(self.conn.execute('SELECT * FROM pending_evidence_states')),first)
        r=self.record();r['stop']-=1;live._save(self.conn,r)
        self.source.cycle(START+STEP);self.source.cycle(START+2*STEP)
        self.assertEqual(self.proof()['latest'][0]['status'],'EVIDENCE_MISMATCH')

    def test_null_state_is_preserved_for_invalidated_result_not_hidden(self):
        self.source.capture()
        self.conn.execute('UPDATE pending_live_states SET setup_id=NULL WHERE close_ms=?',(START,))
        self.collect(START)
        self.source.cycle(START+STEP);self.source.cycle(START+2*STEP)
        self.assertEqual(self.record()['status'],'INVALIDATED')
        out=self.proof()['latest'][0]
        self.assertEqual(out['status'],'COMPLETE_OBSERVATION_EVIDENCE')
        self.assertIsNone(out['observations'][0]['observed_setup_id'])

    def test_original_state_pruning_does_not_delete_saved_proof(self):
        self.source.capture();self.collect(START)
        self.source.overrides[START+STEP]=candle(START+STEP,low=95)
        self.source.cycle(START+STEP);self.source.cycle(START+2*STEP)
        self.source.cycle(START+15*STEP)
        self.assertIsNone(self.conn.execute('SELECT setup_id FROM pending_live_states WHERE close_ms=?',(START,)).fetchone())
        self.assertEqual(self.proof(now=START+16*STEP)['latest'][0]['status'],'COMPLETE_OBSERVATION_EVIDENCE')

    def test_limit_only_changes_display_not_whole_ledger_status_counts(self):
        self.source.capture()
        r=self.record();other=deepcopy(r);other['key']+=':other'
        self.conn.execute('INSERT INTO pending_live_signals VALUES(?,?,?,?)',
                          (other['key'],other['ticker'],json.dumps(other),other['created_ms']))
        self.collect(START);self.source.cycle(START+STEP);self.source.cycle(START+2*STEP)
        a,b=self.proof(limit=1),self.proof(limit=500)
        self.assertEqual(a['summary'],b['summary']);self.assertEqual(len(a['latest']),1)
        self.assertEqual(a['summary']['total_proposal_records'],2)

    def test_outcome_ambiguous_and_roi_identical_with_and_without_proof(self):
        for status in ('TP','SL','AMBIGUOUS','DATA_GAP'):
            control=fixtures.PendingLiveTests();control.setUp()
            try:
                self.source.tearDown();self.source=fixtures.PendingLiveTests();self.source.setUp();self.conn=self.source.conn
                evidence.initialize(self.conn,now_ms=START-STEP+1)
                for f in (self.source,control):
                    f.capture()
                    f.overrides[START+STEP]=candle(START+STEP,low=89 if status=='AMBIGUOUS' else 95,high=121 if status=='AMBIGUOUS' else 102)
                    f.overrides[START+2*STEP]=candle(START+2*STEP,open=110 if status=='TP' else 95,low=109 if status=='TP' else 89,high=121 if status=='TP' else 96,close=110 if status=='TP' else 95)
                self.collect(START)
                for stamp in (START+STEP,START+2*STEP,START+3*STEP):
                    snapshots=self.source.snapshots(stamp)
                    if status=='DATA_GAP' and stamp==START+2*STEP:snapshots={}
                    self.source.cycle(stamp,snapshots=snapshots);self.collect(stamp)
                    control.cycle(stamp,snapshots=snapshots)
                self.assertEqual(live.records(self.conn),live.records(control.conn))
                self.assertEqual(live._meta(self.conn),live._meta(control.conn))
                self.assertEqual(self.source.review(),control.review())
            finally:control.tearDown()

    def test_source_commits_even_when_proof_save_and_error_logging_fail(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(server,'DB_PATH',Path(temp)/'db.sqlite'),patch.object(server,'VAPID_PRIVATE_KEY',''):
            server._init_db()
            with patch.object(evidence,'capture',side_effect=RuntimeError('not logged')):
                server._pending_live_cycle({})
            with server._db_connect() as conn:
                self.assertIsNotNone(live._meta(conn)['last_success_ms'])
                self.assertIsNone(live._meta(conn)['last_error'])
                self.assertEqual(evidence.meta(conn)['last_error'],'RuntimeError')
            with patch.object(evidence,'capture',side_effect=RuntimeError),patch.object(evidence,'record_error',side_effect=sqlite3.OperationalError):
                server._pending_live_cycle({})

    def test_read_only_apis_keep_original_metrics_and_whole_ledger_summary(self):
        self.source.capture();self.collect(START);self.conn.commit()
        before=self.conn.total_changes
        now=START+3*STEP
        with patch.object(server,'_db_connect',return_value=self.conn),patch.object(server.time,'time',return_value=now/1000):
            a=asyncio.run(server.pending_entry_shadow_api(limit=1))
            b=asyncio.run(server.pending_entry_evidence_api(limit=500))
        expected=live.review(self.conn,now_ms=now,limit=1)
        self.assertEqual({k:v for k,v in a.items() if k!='observation_evidence'},expected)
        self.assertEqual(a['observation_evidence']['summary'],b['summary'])
        self.assertEqual(self.conn.total_changes,before)

    def test_evidence_read_failure_does_not_hide_source_shadow_api(self):
        self.source.capture();self.conn.commit()
        with patch.object(server,'_db_connect',return_value=self.conn),patch.object(evidence,'review',side_effect=ValueError('not exposed')):
            result=asyncio.run(server.pending_entry_shadow_api(limit=1))
        self.assertIn('metrics',result['cohorts'][0])
        self.assertEqual(result['observation_evidence'],{'status':'UNAVAILABLE_ERROR','error_type':'ValueError'})

    def test_existing_database_migration_preserves_source_rows_and_audit_origin(self):
        self.source.capture();self.collect(START);self.conn.commit()
        before=live.records(self.conn);origin=live._meta(self.conn);proof=evidence.meta(self.conn)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'db.sqlite'
            dest=sqlite3.connect(path);self.conn.backup(dest);dest.close()
            with patch.object(server,'DB_PATH',path),patch.object(server,'VAPID_PRIVATE_KEY',''):
                server._init_db();server._init_db()
                with server._db_connect() as conn:
                    self.assertEqual(live.records(conn),before)
                    self.assertEqual(live._meta(conn),origin)
                    self.assertEqual(evidence.meta(conn),proof)
                    self.assertEqual(evidence.review(conn,now_ms=START+3*STEP)['summary']['stored_observations'],1)


if __name__=='__main__':unittest.main()
