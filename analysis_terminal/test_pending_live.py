"""Capture before future execution; preserve frozen policy and persisted data."""
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
import unittest

import app
from analysis_terminal import pending_live as live, pending_entry_replay as study
from analysis_terminal.test_pending_entry_replay import CONTRACT,candle,row

STEP, START = live.STEP, 21000*live.DAY
SETTINGS = app.Settings.from_env(dry_run_override=True)


class PendingLiveTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        live.initialize(self.conn,now_ms=START-STEP+1000)
        self.current = dict(ticker=CONTRACT.contract_name,direction=None,stage='TREND_WAIT')
        def analyzer(contract,monitor,entries,*,as_of_ms):
            return deepcopy(self.current)
        self.analyzer = analyzer
        self.overrides = {}
        self.contracts = {CONTRACT.contract_id:CONTRACT}

    def tearDown(self):
        self.conn.close()

    def snapshots(self,stamp):
        anchor=stamp//(16*STEP)*(16*STEP)
        m=[replace(candle(anchor-(180-i)*16*STEP,open=100,high=105,low=95,close=101),interval='HOUR_4') for i in range(180)]
        e=[candle(stamp-(180-i)*STEP) for i in range(180)]
        e=[self.overrides.get(c.time_ms,c) for c in e]
        return {(CONTRACT.contract_id,'HOUR_4'):m,(CONTRACT.contract_id,'MINUTE_15'):e}

    def cycle(self,stamp,*,delay=1000,snapshots=None):
        live.cycle(self.conn,contracts=self.contracts,snapshots=self.snapshots(stamp) if snapshots is None else snapshots,
                   analyze=self.analyzer,settings=SETTINGS,now_ms=stamp+delay)

    def qualify(self,side='LONG'):
        self.current=row(side)
        self.current['breakout_time_ms']=START-16*STEP

    def capture(self,side='LONG'):
        self.cycle(START-STEP)
        self.qualify(side)
        self.cycle(START)
        return live.records(self.conn)[0]

    def review(self,limit=50,now=START+10*STEP):
        return live.review(self.conn,now_ms=now,limit=limit)

    def test_persistent_activation_baseline_and_no_import_or_notification(self):
        self.qualify();self.cycle(START-STEP);self.cycle(START)
        self.assertEqual(live.records(self.conn),[])
        first=live._meta(self.conn)
        live.initialize(self.conn,now_ms=START+100*STEP)
        self.assertEqual(live._meta(self.conn),first)
        self.assertEqual(first['capture_start_ms'],START)
        r=self.review(now=START-1)
        self.assertEqual(r['status'],'WAITING_FOR_CAPTURE_START')
        for key in ['real_orders_enabled','automatic_promotion','eligible_for_live_promotion','notifications_enabled','current_entry_status']:
            self.assertFalse(r[key])
        self.assertEqual(r['sample_status'],'INSUFFICIENT SAMPLE')

    def test_freezes_source_actual_capture_and_original_four_bar_deadline(self):
        r=self.capture();before=deepcopy(r)
        self.assertEqual(r['created_ms'],START+1)
        self.assertEqual(r['observed_ms'],START+1000)
        self.assertEqual(r['execution_start_ms'],START+STEP)
        self.assertEqual(r['expires_ms'],START+4*STEP)
        self.assertLess(r['trigger'],r['frozen_source']['entry_reference'])
        self.current['entry_reference']=110
        self.cycle(START+STEP)
        after=live.records(self.conn)[0]
        for key in ['frozen_source','created_ms','observed_ms','trigger','stop','target','expires_ms']:
            self.assertEqual(after[key],before[key])
        self.assertEqual(len(live.records(self.conn)),1)

    def test_capture_candle_and_signal_candle_never_fill_or_resolve(self):
        self.overrides[START-STEP]=candle(START-STEP,low=80,high=130)
        self.overrides[START]=candle(START,low=80,high=130)
        self.capture();self.cycle(START+STEP)
        r=live.records(self.conn)[0]
        self.assertEqual(r['status'],'PENDING');self.assertIsNone(r['filled_ms'])
        self.assertEqual(r['mfe_r'],0)

    def test_later_fill_and_exit_equal_frozen_evaluator_both_sides(self):
        for side in ['LONG','SHORT']:
            if side=='SHORT':self.tearDown();self.setUp()
            original=self.capture(side)
            fill=candle(START+STEP,open=101,low=95,high=102) if side=='LONG' else candle(START+STEP,open=99,low=98,high=105,close=99)
            exit=candle(START+2*STEP,open=110,low=109,high=121,close=120) if side=='LONG' else candle(START+2*STEP,open=90,low=79,high=91,close=80)
            self.overrides.update({fill.time_ms:fill,exit.time_ms:exit})
            self.cycle(START+STEP);self.cycle(START+2*STEP);self.cycle(START+3*STEP)
            actual=live.records(self.conn)[0]
            expected=study.evaluate(dict(original,created_ms=original['execution_start_ms']+1),[fill,exit],
                                    {fill.time_ms:original['setup_id'],exit.time_ms:original['setup_id']},end_ms=START+3*STEP)
            for key in ['status','filled_ms','outcome_ms','entry','exit_price','net_pnl_per_unit','final_net_r','mfe_r','mae_r']:
                self.assertEqual(actual[key],expected[key],key)
            self.assertAlmostEqual(actual['final_net_r'],2)
            self.assertEqual(actual['created_ms'],START+1)

    def test_fill_bar_exit_is_ambiguous_and_capital_roi_unknown(self):
        self.capture();self.overrides[START+STEP]=candle(START+STEP,low=89,high=121)
        self.cycle(START+STEP);self.cycle(START+2*STEP)
        self.assertEqual(live.records(self.conn)[0]['status'],'AMBIGUOUS')
        c=self.review()['cohorts'][0]
        self.assertIsNone(c['portfolios'][study.MODEL]['closed_portfolio_roi_pct'])
        self.assertEqual(c['metrics'][study.MODEL]['resolved'],0)

    def test_expiry_not_extended_and_candidate_never_rearmed(self):
        self.capture()
        for i in range(1,5):self.cycle(START+i*STEP)
        r=live.records(self.conn)[0]
        self.assertEqual((r['status'],r['outcome_ms']),('EXPIRED',START+4*STEP))
        self.overrides[START+4*STEP]=candle(START+4*STEP,low=95,high=130)
        self.cycle(START+5*STEP)
        self.assertEqual(live.records(self.conn)[0],r)

    def test_previously_observed_identity_used_without_future_invalidation(self):
        self.capture();self.cycle(START+STEP)
        self.overrides[START+STEP]=candle(START+STEP,low=95)
        self.current=dict(ticker=CONTRACT.contract_name,direction=None,stage='TREND_WAIT')
        self.cycle(START+2*STEP)
        self.assertEqual(live.records(self.conn)[0]['status'],'OPEN')

    def test_observed_invalidation_before_fill_is_respected(self):
        self.capture();self.current=dict(ticker=CONTRACT.contract_name,direction=None,stage='TREND_WAIT')
        self.cycle(START+STEP)
        self.overrides[START+2*STEP]=candle(START+2*STEP,low=95)
        self.cycle(START+2*STEP);self.cycle(START+3*STEP)
        self.assertEqual(live.records(self.conn)[0]['status'],'INVALIDATED')

    def test_missing_precapture_state_blocks_later_profitable_fill(self):
        self.capture()
        self.overrides[START+2*STEP]=candle(START+2*STEP,low=95,high=130)
        self.cycle(START+3*STEP)
        r=live.records(self.conn)[0]
        self.assertEqual(r['status'],'DATA_GAP');self.assertIsNone(r['filled_ms'])

    def test_duplicate_candle_or_missing_price_blocks_result(self):
        for kind in ['missing','duplicate']:
            if kind=='duplicate':self.tearDown();self.setUp()
            self.capture();self.cycle(START+STEP)
            data=self.snapshots(START+2*STEP);key=(CONTRACT.contract_id,'MINUTE_15')
            if kind=='missing':data[key]=[c for c in data[key] if c.time_ms!=START+STEP]
            else:data[key].append(candle(START+STEP,low=80,high=130))
            self.cycle(START+2*STEP,snapshots=data)
            self.assertEqual(live.records(self.conn)[0]['status'],'DATA_GAP')

    def test_late_capture_or_missed_observation_is_consumed(self):
        for kind in ['late','gap']:
            if kind=='gap':self.tearDown();self.setUp()
            self.cycle(START-STEP if kind=='late' else START-2*STEP)
            self.qualify();self.cycle(START,delay=120001 if kind=='late' else 1000)
            self.cycle(START+STEP)
            self.assertEqual(live.records(self.conn),[])

    def test_frame_gap_wrong_identity_and_insufficient_window_cannot_capture(self):
        self.cycle(START-STEP);self.qualify()
        for kind in ['short','gap','identity']:
            data=self.snapshots(START);key=(CONTRACT.contract_id,'HOUR_4')
            if kind=='short':data[key]=data[key][1:]
            elif kind=='gap':data[key][0]=replace(data[key][0],time_ms=data[key][0].time_ms-16*STEP)
            else:data[key][0]=replace(data[key][0],contract_id='wrong')
            self.cycle(START,snapshots=data)
            self.assertFalse(live.records(self.conn))

    def test_windowed_cohorts_and_metrics_do_not_shrink_with_display_limit(self):
        self.capture();one=self.review(1);full=self.review(500)
        self.assertEqual(one['cohorts'],full['cohorts'])
        self.assertFalse(full['cohorts'][0]['period_complete'])
        self.assertEqual(full['cohorts'][0]['start_ms'],START)
        self.assertEqual(full['cohorts'][0]['end_ms'],START+live.WEEK)

    def test_existing_database_rows_survive_idempotent_migration(self):
        self.conn.execute('CREATE TABLE push_subscriptions (endpoint TEXT PRIMARY KEY,payload TEXT)')
        self.conn.execute('INSERT INTO push_subscriptions VALUES (?,?)',('existing','{}'))
        live.initialize(self.conn,now_ms=START+5*STEP);live.initialize(self.conn,now_ms=START+6*STEP)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM push_subscriptions').fetchone()[0],1)

    def test_parameter_or_dependency_change_rejected_without_mixing(self):
        self.capture()
        before=live.records(self.conn)
        from unittest.mock import patch
        with patch.object(live,'strategy_parameters',return_value={}):
            with self.assertRaises(ValueError):self.cycle(START+STEP)
        self.assertEqual(live.records(self.conn),before)


    def test_continuous_capital_does_not_reset_open_reservations_at_week_boundary(self):
        self.capture()
        self.overrides[START+STEP]=candle(START+STEP,low=95)
        self.cycle(START+STEP);self.cycle(START+2*STEP)
        opened=live.records(self.conn)[0]
        self.assertEqual(opened['status'],'OPEN')
        later=dict(opened,key=opened['key']+':later',setup_id=opened['setup_id']+':later',
                   cohort_start_ms=START+live.WEEK,created_ms=START+live.WEEK+1,
                   observed_ms=START+live.WEEK+1000,filled_ms=START+live.WEEK+STEP,
                   outcome_ms=START+live.WEEK+2*STEP,status='TP',final_net_r=2,
                   net_pnl_per_unit=2*opened['net_risk'],exit_price=opened['target']*(1-study.SLIP))
        self.conn.execute('INSERT INTO pending_live_signals VALUES (?,?,?,?)',
                          (later['key'],later['ticker'],live._json(later),later['created_ms']))
        r=self.review(now=START+2*live.WEEK)
        self.assertEqual(len(r['cohorts']),2)
        self.assertGreater(r['cohorts'][1]['portfolios'][study.MODEL]['closed_portfolio_roi_pct'],0)
        continuous=r['continuous_portfolios'][study.MODEL]
        self.assertEqual(continuous['filled'],1)
        self.assertEqual(continuous['resolved'],0)
        self.assertIsNone(continuous['closed_portfolio_roi_pct'])
        self.assertEqual(continuous['exclusions']['CAPACITY_OR_TICKER'],1)


    def test_coverage_current_bucket_grace_and_missing_history_are_not_zero_results(self):
        before=self.review(now=START-1)['coverage']
        self.assertEqual(before['status'],'WAITING_FOR_CAPTURE_START')
        pending=self.review(now=START+119999)['coverage']['cohorts'][0]
        self.assertEqual(pending['overdue_missing_buckets'],0)
        self.assertEqual(pending['pending_current_buckets'],1)
        missing=self.review(now=START+120000)['coverage']['cohorts'][0]
        self.assertEqual(missing['overdue_missing_buckets'],1)
        self.assertIsNone(missing['indicator_coverage_pct'])
        self.assertEqual(missing['status'],'OBSERVATION_HISTORY_INCOMPLETE')
        self.assertEqual(self.review(now=START+120000)['cohorts'],[])

    def test_first_bucket_evidence_does_not_change_on_repeated_scan(self):
        self.capture()
        first=self.conn.execute('SELECT payload FROM pending_live_cycles WHERE bucket_ms=?',(START,)).fetchone()[0]
        self.cycle(START,delay=60000)
        second=self.conn.execute('SELECT payload FROM pending_live_cycles WHERE bucket_ms=?',(START,)).fetchone()[0]
        self.assertEqual(first,second)
        c=self.review(now=START+120000)['coverage']['cohorts'][0]
        self.assertEqual(c['expected_buckets'],1)
        self.assertEqual(c['recorded_buckets'],1)
        self.assertEqual(c['quality']['CAPTURED_'+study.MODEL],1)
        self.assertEqual(c['quality']['QUALIFIED_'+study.MODEL],1)
        self.assertEqual(c['requested_market_observations'],1)

    def test_gap_coverage_keeps_unobserved_bucket_and_late_attempt_separate(self):
        self.cycle(START-STEP)
        self.cycle(START)
        self.cycle(START+2*STEP,delay=120001)
        c=self.review(now=START+2*STEP+120001)['coverage']['cohorts'][0]
        self.assertEqual((c['expected_buckets'],c['recorded_buckets'],c['overdue_missing_buckets']),(3,2,1))
        self.assertEqual(c['late_recorded_buckets'],1)
        self.assertEqual(c['valid_market_observations'],2)

    def test_indicator_coverage_counts_declared_markets_even_without_usable_prices(self):
        self.cycle(START,snapshots={})
        c=self.review(now=START+120000)['coverage']['cohorts'][0]
        self.assertEqual(c['recorded_buckets'],1)
        self.assertEqual(c['requested_market_observations'],1)
        self.assertEqual(c['valid_market_observations'],0)
        self.assertEqual(c['indicator_coverage_pct'],0)
        self.assertEqual(c['quality']['INCOMPLETE_INDICATOR_WINDOW'],1)
        self.assertFalse(live.records(self.conn))

    def test_week_boundary_and_display_limit_cannot_merge_coverage_cohorts(self):
        self.cycle(START)
        self.cycle(START+live.WEEK)
        c=self.review(now=START+live.WEEK+120000)['coverage']
        self.assertEqual(c,self.review(limit=1,now=START+live.WEEK+120000)['coverage'])
        first,second=c['cohorts']
        self.assertEqual(first['expected_buckets'],672)
        self.assertEqual(first['recorded_buckets'],1)
        self.assertTrue(first['period_complete'])
        self.assertEqual(second['expected_buckets'],1)
        self.assertEqual(second['recorded_buckets'],1)
        self.assertFalse(second['period_complete'])

    def test_additive_audit_start_never_reconstructs_old_coverage_or_resets_activation(self):
        meta=live._meta(self.conn);meta.pop('coverage_started_ms')
        live._save_meta(self.conn,meta)
        live.initialize(self.conn,now_ms=START+2*STEP)
        current=live._meta(self.conn)
        self.assertEqual(current['activated_ms'],meta['activated_ms'])
        self.assertEqual(current['capture_start_ms'],meta['capture_start_ms'])
        self.assertEqual(current['coverage_started_ms'],START+2*STEP)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM pending_live_cycles').fetchone()[0],0)
        c=self.review(now=START+2*STEP+120000)['coverage']['cohorts'][0]
        self.assertEqual(c['overdue_missing_buckets'],3)
        self.assertEqual(c['unrecorded_before_audit_buckets'],2)


class PendingLiveStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from analysis_terminal.test_storage import StorageTests,server
        self.server=server
        StorageTests.setUp(self)

    async def test_read_only_api_limits_and_idempotent_activation(self):
        from httpx import ASGITransport,AsyncClient
        s=self.server
        async with AsyncClient(transport=ASGITransport(app=s.app),base_url='http://test') as client:
            first=(await client.get('/api/pending-entry-shadow?limit=1')).json()
            second=(await client.get('/api/pending-entry-shadow?limit=500')).json()
            self.assertEqual(first['cohorts'],second['cohorts'])
            self.assertEqual(first['meta'],second['meta'])
            self.assertFalse(first['real_orders_enabled'])
            for bad in ['0','501']:
                self.assertEqual((await client.get('/api/pending-entry-shadow?limit='+bad)).status_code,422)
            self.assertEqual((await client.post('/api/pending-entry-shadow',json={})).status_code,405)
            self.assertEqual((await client.get('/health')).status_code,200)
        s._init_db()
        with s._db_connect() as conn:
            self.assertEqual(live._meta(conn)['activated_ms'],first['meta']['activated_ms'])
        self.assertEqual(s._subscription_count(),1)

    def test_capture_failure_rollback_isolated_from_existing_data(self):
        from unittest.mock import patch
        s=self.server
        snapshot=dict(time_ms=self.now_ms,ready=0)
        s._save_market_snapshot(snapshot)
        def failing(conn,**kwargs):
            conn.execute('INSERT INTO pending_live_seen VALUES (?,?)',('should-rollback','test'))
            raise ValueError('private error body must never be logged')
        with patch.object(live,'cycle',side_effect=failing),patch('builtins.print') as log:
            s._pending_live_cycle({})
        self.assertEqual(log.call_args.args[0],'Pending Shadow capture paused: ValueError')
        with s._db_connect() as conn:
            self.assertEqual(live._meta(conn)['last_error'],'ValueError')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pending_live_seen').fetchone()[0],0)
        self.assertEqual(s._subscription_count(),1)
        self.assertEqual(s._load_market_history(),[snapshot])
        self.assertEqual(s._load_paper_signals(),[])
        self.assertEqual(s._load_shadow_v2_signals(),[])
        self.assertEqual(s._load_push_events(),[])


if __name__=='__main__':unittest.main()
