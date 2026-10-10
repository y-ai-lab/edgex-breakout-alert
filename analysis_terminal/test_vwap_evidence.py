"""Future-only evidence, GC retention, source parity and running-state migration."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from analysis_terminal import vwap_evidence as proof, vwap_live as live, server, live_execution
from analysis_terminal import test_vwap_live as fixtures
START, SIGNAL, STEP = fixtures.START, fixtures.SIGNAL, fixtures.STEP
from analysis_terminal.test_pending_entry_replay import candle


class VwapEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source = fixtures.VwapLiveTests(); self.source.setUp(); self.conn = self.source.conn
        proof.initialize(self.conn, now_ms=START-STEP+1)

    def tearDown(self):
        self.source.tearDown()

    def capture(self, stamp):
        proof.capture(self.conn, observation_ms=stamp+1000, recorded_ms=stamp+1100)

    def review(self, now=SIGNAL+4*STEP, limit=500):
        return proof.review(self.conn, now_ms=now, limit=limit)

    def test_future_proof_preserves_exact_state_before_execution_open(self):
        r=self.source.capture(); self.capture(SIGNAL+STEP)
        state=self.conn.execute('SELECT payload FROM vwap_live_states WHERE close_ms=?', (SIGNAL+STEP,)).fetchone()[0]
        self.source.cycle(SIGNAL+3*STEP)
        record=self.review()['latest'][0]
        self.assertEqual(record['status'], 'COMPLETE_OBSERVATION_EVIDENCE')
        observed=record['observations'][0]
        self.assertEqual(observed['state_sha256'], proof.digest(state))
        self.assertEqual(observed['identity_sha256'], proof.identity_hash(r))
        self.assertLess(observed['recorded_ms'],r['execution_start_ms'])

    def test_retries_keep_first_recording_time_and_do_not_advance_audit_clock(self):
        self.source.capture(); self.capture(SIGNAL+STEP)
        old=list(self.conn.execute('SELECT * FROM vwap_evidence_states')); m=proof.meta(self.conn)
        proof.capture(self.conn, observation_ms=SIGNAL+STEP+1000, recorded_ms=SIGNAL+STEP+2000)
        proof.capture(self.conn, observation_ms=SIGNAL+STEP+3000, recorded_ms=SIGNAL+STEP+3100)
        self.assertEqual(list(self.conn.execute('SELECT * FROM vwap_evidence_states')),old)
        self.assertEqual(proof.meta(self.conn),m)

    def test_mid_bucket_installation_does_not_backfill_already_observed_states(self):
        self.source.capture(); self.conn.execute('DELETE FROM vwap_evidence_meta')
        proof.initialize(self.conn,now_ms=SIGNAL+STEP+2000)
        self.capture(SIGNAL+STEP); self.source.cycle(SIGNAL+3*STEP)
        result=self.review()['latest'][0]
        self.assertEqual(result['status'],'UNAVAILABLE_BEFORE_AUDIT')
        self.assertEqual(result['verified_observations'],0)

    def test_post_frontier_missing_is_not_legacy_or_complete(self):
        self.source.capture(); self.source.cycle(SIGNAL+3*STEP)
        r=self.review()['latest'][0]
        self.assertEqual(r['status'],'MISSING_OBSERVATION_EVIDENCE')
        self.assertEqual(r['missing_observations'],1)

    def test_proof_at_execution_open_is_rejected(self):
        r=self.source.capture()
        with self.assertRaises(ValueError):
            proof.capture(self.conn,observation_ms=SIGNAL+STEP+1000,recorded_ms=r['execution_start_ms'])
        self.assertEqual(self.review()['summary']['stored_observations'],0)

    def test_no_first_cycle_or_mismatched_state_cannot_become_proof(self):
        self.source.capture()
        self.capture(SIGNAL+2*STEP)
        self.conn.execute('UPDATE vwap_live_states SET observed_ms=observed_ms+1')
        self.capture(SIGNAL+STEP)
        self.assertEqual(self.review()['summary']['stored_observations'],0)

    def test_conflicting_state_never_overwrites_first_proof(self):
        self.source.capture();self.capture(SIGNAL+STEP)
        old=list(self.conn.execute('SELECT * FROM vwap_evidence_states'))
        self.conn.execute("UPDATE vwap_live_states SET payload='null'")
        with self.assertRaises(ValueError):self.capture(SIGNAL+STEP)
        self.assertEqual(list(self.conn.execute('SELECT * FROM vwap_evidence_states')),old)

    def test_null_state_is_a_recorded_negative_condition_not_a_missing_proof(self):
        self.source.capture();self.conn.execute("UPDATE vwap_live_states SET payload='null'")
        self.capture(SIGNAL+STEP);self.source.cycle(SIGNAL+3*STEP)
        self.assertEqual(live.records(self.conn)[0]['status'],'DATA_GAP')
        r=self.review()['latest'][0]
        self.assertEqual(r['status'],'COMPLETE_OBSERVATION_EVIDENCE')
        self.assertIsNone(r['observations'][0]['observed_state'])

    def test_hash_corruption_and_candidate_change_are_explicit_mismatches(self):
        for change in ('payload','candidate','roll_level'):
            if change!='payload':self.tearDown();self.setUp()
            r=self.source.capture();self.capture(SIGNAL+STEP);self.source.cycle(SIGNAL+3*STEP)
            if change=='payload':self.conn.execute("UPDATE vwap_evidence_states SET payload='null'")
            else:r=live.records(self.conn)[0];r['roll_level' if change=='roll_level' else 'stop']-=1;live.save_record(self.conn,r)
            self.assertEqual(self.review()['latest'][0]['status'],'EVIDENCE_MISMATCH')

    def test_gc_removes_original_states_but_retains_proof(self):
        self.source.capture();self.capture(SIGNAL+STEP);self.source.cycle(SIGNAL+3*STEP)
        self.source.cycle(SIGNAL+20*STEP)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM vwap_live_states').fetchone()[0],0)
        self.assertEqual(self.review(now=SIGNAL+21*STEP)['latest'][0]['status'],'COMPLETE_OBSERVATION_EVIDENCE')

    def test_source_origin_drift_is_refused(self):
        self.source.capture();m=live.meta(self.conn);m['capture_start_ms']+=STEP;live.save_meta(self.conn,m)
        with self.assertRaises(ValueError):self.capture(SIGNAL+STEP)
        self.assertEqual(self.review()['summary']['stored_observations'],0)
        self.assertEqual(self.review()['status'],'SOURCE_ORIGIN_MISMATCH')

    def test_unclosed_bar_is_not_a_verified_zero_result(self):
        r=self.source.capture();self.capture(SIGNAL+STEP)
        result=self.review(now=r['execution_start_ms']+1000)['latest'][0]
        self.assertEqual(result['status'],'NO_CLOSED_ELIGIBLE_BAR_YET')

    def test_limits_do_not_change_counts_and_stale_error_states_are_explicit(self):
        self.source.capture();self.capture(SIGNAL+STEP)
        self.assertEqual(self.review(limit=0)['summary'],self.review(limit=500)['summary'])
        self.assertEqual(self.review(limit=0)['latest'],[])
        self.assertEqual(self.review(now=SIGNAL+5*STEP)['status'],'STALE_OBSERVATION')
        proof.record_error(self.conn,'ValueError');self.assertEqual(self.review()['status'],'PAUSED_ERROR')

    def test_outcomes_capital_and_source_metadata_identical_with_and_without_proof(self):
        for side in ('LONG','SHORT'):
            for status in ('TP','SL','AMBIGUOUS','DATA_GAP'):
                with self.subTest(side=side,status=status):
                    self.tearDown();self.setUp();control=fixtures.VwapLiveTests();control.setUp()
                    try:
                        r=self.source.capture(side);control.capture(side);cursor=r['execution_start_ms'];px=r['trigger']
                        for f in (self.source,control):
                            f.state(r,cursor-STEP)
                            f.overrides[cursor]=candle(cursor,open=px,close=px,low=min(r['stop'],r['target'])-.01 if status=='AMBIGUOUS' else px-.01,high=max(r['stop'],r['target'])+.01 if status=='AMBIGUOUS' else px+.01)
                            level=r['target'] if status=='TP' else r['stop']
                            f.overrides[cursor+STEP]=candle(cursor+STEP,open=px,close=px,low=min(px-.01,level-.01),high=max(px+.01,level+.01))
                        self.capture(cursor-STEP)
                        for stamp in (cursor,cursor+STEP,cursor+2*STEP):
                            for f in (self.source,control):
                                snapshots=f.snapshots(stamp)
                                if status=='DATA_GAP' and stamp==cursor+STEP:snapshots={}
                                f.cycle(stamp,snapshots=snapshots)
                            self.capture(stamp)
                        self.assertEqual(live.records(self.conn),live.records(control.conn))
                        self.assertEqual(live.meta(self.conn),live.meta(control.conn))
                        self.assertEqual(live.review(self.conn,now_ms=cursor+3*STEP),live.review(control.conn,now_ms=cursor+3*STEP))
                        self.assertEqual(live.records(self.conn)[0]['status'],status)
                    finally:control.tearDown()

    def test_server_source_commit_survives_proof_failure(self):
        self.source.capture();self.conn.commit();now=SIGNAL+2*STEP+1000
        with patch.object(server,'_db_connect',return_value=self.conn),patch.object(server,'_snapshot_cache',(now/1000,self.source.snapshots(SIGNAL+2*STEP))),patch.object(server,'analyze_contract',self.source.analyze),patch.object(server,'SETTINGS',fixtures.SETTINGS),patch.object(server.time,'time',return_value=now/1000),patch.object(proof,'capture',side_effect=ValueError('private text')):
            server._vwap_live_cycle(self.source.contracts)
        self.assertEqual(live.meta(self.conn)['last_success_ms'],now)
        self.assertIsNone(live.meta(self.conn)['last_error'])
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(proof.meta(self.conn)['last_error'],'ValueError')

    def test_source_failure_does_not_capture_proof_or_report_audit_success(self):
        self.conn.commit()
        with patch.object(server,'_db_connect',return_value=self.conn),patch.object(live,'cycle',side_effect=ValueError('private')),patch.object(proof,'capture') as capture:
            server._vwap_live_cycle({})
        capture.assert_not_called()
        self.assertEqual(live.meta(self.conn)['last_error'],'ValueError')
        self.assertIsNone(proof.meta(self.conn)['last_success_ms'])

    def test_apis_are_read_only_and_failure_does_not_hide_source_metrics(self):
        self.source.capture();self.capture(SIGNAL+STEP);self.conn.commit();changes=self.conn.total_changes
        now=SIGNAL+3*STEP
        with patch.object(server,'_db_connect',return_value=self.conn),patch.object(server.time,'time',return_value=now/1000):
            a=asyncio.run(server.vwap_entry_shadow_api(limit=500))
            self.conn.rollback();b=asyncio.run(server.vwap_entry_evidence_api(limit=500))
        self.assertEqual(a['observation_evidence']['summary'],b['summary'])
        self.assertEqual({k:v for k,v in a.items() if k!='observation_evidence'},live.review(self.conn,now_ms=now,limit=500))
        self.assertEqual(self.conn.total_changes,changes);self.conn.rollback()
        with patch.object(server,'_db_connect',return_value=self.conn),patch.object(proof,'review',side_effect=ValueError('private')):
            result=asyncio.run(server.vwap_entry_shadow_api(limit=500))
        self.assertIn('cohorts',result);self.assertEqual(result['observation_evidence']['status'],'UNAVAILABLE_ERROR')

    def test_migration_preserves_audit_origin_source_ledger_and_armed_execution(self):
        self.source.capture();self.capture(SIGNAL+STEP)
        live_execution.initialize(self.conn,now_ms=START)
        s=live_execution.state(self.conn);s.update(armed=True,armed_ms=START,control_epoch=7)
        live_execution.save_state(self.conn,s);self.conn.commit()
        original=live.records(self.conn);origin=live.meta(self.conn);audit=proof.meta(self.conn)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'db.sqlite';dest=sqlite3.connect(path);self.conn.backup(dest);dest.close()
            with patch.object(server,'DB_PATH',path),patch.object(server,'VAPID_PRIVATE_KEY',''):
                server._init_db();server._init_db()
                with server._db_connect() as conn:
                    self.assertEqual(live.records(conn),original);self.assertEqual(live.meta(conn),origin)
                    self.assertEqual(proof.meta(conn),audit);self.assertEqual(live_execution.state(conn),s)


if __name__ == '__main__':unittest.main()
