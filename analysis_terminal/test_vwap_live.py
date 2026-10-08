"""Observed-before-open capture, frozen evaluator parity and isolated SQLite."""
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
import unittest

import app
from analysis_terminal import vwap_live as live, vwap_reclaim_replay as study
from analysis_terminal import test_vwap_reclaim_replay as fixtures
from analysis_terminal.test_pending_entry_replay import CONTRACT, candle

STEP, START, SIGNAL = live.STEP, fixtures.ORIGIN, fixtures.SIGNAL
SETTINGS = app.Settings.from_env(dry_run_override=True)


class VwapLiveTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        live.initialize(self.conn,now_ms=START-STEP+1000)
        self.monitor,self.entries = fixtures.market()
        self.overrides = {}
        self.current = dict(ticker=CONTRACT.contract_name,stage="TREND_WAIT",direction=None)

        def analyze(contract,monitor,entries,*,as_of_ms):
            return deepcopy(self.current)
        self.analyze = analyze
        self.contracts = {CONTRACT.contract_id:CONTRACT}

    def tearDown(self):
        self.conn.close()

    def snapshots(self,stamp):
        cs = {c.time_ms:c for c in self.entries}
        for t in range(SIGNAL+STEP,stamp,STEP):
            cs[t] = candle(t,open=100.1,high=100.2,low=100.09,close=100.15,volume=10)
        cs.update(self.overrides)
        return {(CONTRACT.contract_id,"HOUR_4"):self.monitor,
                (CONTRACT.contract_id,"MINUTE_15"):[c for _,c in sorted(cs.items())]}

    def cycle(self,stamp,delay=1000,snapshots=None):
        live.cycle(self.conn,contracts=self.contracts,snapshots=self.snapshots(stamp) if snapshots is None else snapshots,
                   analyze=self.analyze,settings=SETTINGS,now_ms=stamp+delay)

    def capture(self,side="LONG"):
        self.monitor,self.entries = fixtures.market(side)
        for stamp in range(START-STEP,SIGNAL+STEP+1,STEP):self.cycle(stamp)
        return live.records(self.conn)[0]

    def state(self,r,stamp):
        good = dict(setup_id=r["setup_id"],confirmation_color_ok=True,confirmation_level_ok=True,retest_touched=True,stop_valid=True)
        self.conn.execute("INSERT OR REPLACE INTO vwap_live_states VALUES(?,?,?,?)", (r["key"],stamp,stamp+1000,live.encode(good)))

    def test_activation_is_persistent_independent_and_all_execution_flags_false(self):
        before = live.meta(self.conn)
        live.initialize(self.conn,now_ms=START+10*live.DAY)
        self.assertEqual(live.meta(self.conn),before)
        self.assertEqual(before["capture_start_ms"],START)
        r=live.review(self.conn,now_ms=START-1)
        self.assertEqual(r["status"],"WAITING_FOR_CAPTURE_START")
        for k in ("real_orders_enabled","automatic_promotion","eligible_for_live_promotion","notifications_enabled","current_entry_status"):
            self.assertFalse(r[k])
        self.assertEqual(r["cohorts"],[])

    def test_signal_capture_and_expiry_freeze_original_prices(self):
        r=self.capture()
        original=fixtures.record()
        for k in ("key","setup_id","created_ms","signal_candle_ms","expires_ms","trigger","stop","target","roll_level"):
            self.assertEqual(r[k],original[k])
        self.assertEqual(r["observed_ms"],SIGNAL+STEP+1000)
        self.assertEqual(r["execution_start_ms"],SIGNAL+2*STEP)
        self.assertLess(r["observed_ms"],r["execution_start_ms"])
        self.assertEqual(r["cohort_start_ms"],START)

    def test_signal_and_partially_elapsed_capture_candle_never_fill(self):
        r=self.capture()
        self.overrides[SIGNAL+STEP]=candle(SIGNAL+STEP,open=100,low=80,high=150)
        self.cycle(SIGNAL+2*STEP)
        actual=live.records(self.conn)[0]
        self.assertEqual(actual["status"],"PENDING")
        self.assertIsNone(actual["filled_ms"])
        self.assertEqual(actual["mfe_r"],0)
        self.assertEqual(actual["mae_r"],0)
        self.assertEqual(actual["created_ms"],r["created_ms"])

    def test_single_bar_adapter_matches_full_frozen_evaluator_long_short_tp_sl(self):
        for side in ("LONG","SHORT"):
            for outcome in ("TP","SL"):
                with self.subTest(side=side,outcome=outcome):
                    r=fixtures.record(side)
                    original=deepcopy(r)
                    r.update(execution_start_ms=r["created_ms"]-1+STEP,next_candle_ms=r["created_ms"]-1+STEP)
                    cursor=r["next_candle_ms"]
                    px=r["trigger"]
                    fill=candle(cursor,open=px,close=px,low=px-.01,high=px+.01)
                    level=r["target"] if outcome=="TP" else r["stop"]
                    exit=candle(cursor+STEP,open=px,close=px,low=min(px-.01,level-.01),high=max(px+.01,level+.01))
                    # Only chosen exit; do not touch the opposite frozen level.
                    good=dict(setup_id=r["setup_id"],confirmation_color_ok=True,confirmation_level_ok=True,retest_touched=True,stop_valid=True)
                    first=live.evaluate_bar(r,fill,good,cursor=cursor)
                    first["next_candle_ms"]=cursor+STEP
                    actual=live.evaluate_bar(first,exit,good,cursor=cursor+STEP)
                    no_touch=candle(cursor-STEP,open=px+(.1 if side=="LONG" else -.1),
                                    close=px+(.1 if side=="LONG" else -.1),
                                    low=px+(.05 if side=="LONG" else -.15),high=px+(.15 if side=="LONG" else -.05))
                    expected=study.evaluate(original,[no_touch,fill,exit],lambda _:good,end_ms=cursor+2*STEP)
                    for k in ("status","filled_ms","outcome_ms","entry","exit_price","final_net_r","mfe_r","mae_r","created_ms","signal_candle_ms","expires_ms"):
                        self.assertEqual(actual[k],expected[k],k)
                    self.assertEqual(actual["status"],outcome)

    def test_fill_and_exit_same_bar_ambiguous_and_no_released_capital(self):
        r=self.capture();cursor=r["execution_start_ms"]
        self.state(r,cursor-STEP)
        self.overrides[cursor]=candle(cursor,open=r["trigger"],close=r["trigger"],low=r["stop"]-.01,high=r["target"]+.01)
        self.cycle(cursor);self.state(r,cursor-STEP);self.cycle(cursor+STEP)
        actual=live.records(self.conn)[0]
        self.assertEqual(actual["status"],"AMBIGUOUS")
        review=live.review(self.conn,now_ms=cursor+STEP+1000)
        self.assertIsNone(review["continuous_portfolios"][study.MODEL]["closed_portfolio_roi_pct"])
        self.assertEqual(review["continuous_portfolios"][study.MODEL]["uncertain"],1)

    def test_later_same_bar_tp_sl_is_ambiguous(self):
        r=fixtures.record();cursor=r["created_ms"]-1+STEP
        r.update(execution_start_ms=cursor,next_candle_ms=cursor)
        good=dict(setup_id=r["setup_id"],confirmation_color_ok=True,confirmation_level_ok=True,retest_touched=True,stop_valid=True)
        first=live.evaluate_bar(r,candle(cursor,open=r["trigger"],close=r["trigger"],low=r["trigger"]-.01,high=r["trigger"]+.01),good,cursor=cursor)
        first["next_candle_ms"]=cursor+STEP
        actual=live.evaluate_bar(first,candle(cursor+STEP,open=r["trigger"],low=r["stop"]-.01,high=r["target"]+.01),good,cursor=cursor+STEP)
        self.assertEqual(actual["status"],"AMBIGUOUS")
        self.assertIsNone(actual["final_net_r"])

    def test_missing_precapture_state_is_data_gap_no_profitable_backfill(self):
        r=self.capture();cursor=r["execution_start_ms"]
        self.conn.execute("DELETE FROM vwap_live_states")
        self.overrides[cursor]=candle(cursor,open=r["trigger"],low=r["trigger"]-.01,high=r["target"]+.1)
        self.cycle(cursor+STEP)
        self.assertEqual(live.records(self.conn)[0]["status"],"DATA_GAP")
        self.assertIsNone(live.records(self.conn)[0]["filled_ms"])

    def test_observation_at_candle_open_is_too_late(self):
        r=self.capture();cursor=r["execution_start_ms"]
        self.conn.execute("UPDATE vwap_live_states SET observed_ms=? WHERE signal_key=?",(cursor,r["key"]))
        self.cycle(cursor+STEP)
        self.assertEqual(live.records(self.conn)[0]["status"],"DATA_GAP")

    def test_pending_confirmation_loss_is_not_relaxed(self):
        r=self.capture();cursor=r["execution_start_ms"]
        bad=dict(setup_id=r["setup_id"],confirmation_color_ok=False,confirmation_level_ok=True,retest_touched=True,stop_valid=True)
        self.conn.execute("UPDATE vwap_live_states SET payload=?",(live.encode(bad),))
        self.cycle(cursor+STEP)
        self.assertEqual(live.records(self.conn)[0]["status"],"INVALIDATED")

    def test_missing_or_duplicate_price_stops_results(self):
        for duplicate in (False,True):
            if duplicate:self.tearDown();self.setUp()
            r=self.capture();cursor=r["execution_start_ms"]
            snapshots=self.snapshots(cursor+STEP);key=(CONTRACT.contract_id,"MINUTE_15")
            if duplicate:snapshots[key].append(candle(cursor,open=100.1,low=80,high=150))
            else:snapshots[key]=[c for c in snapshots[key] if c.time_ms!=cursor]
            self.cycle(cursor+STEP,snapshots=snapshots)
            self.assertEqual(live.records(self.conn)[0]["status"],"DATA_GAP")

    def test_first_late_incomplete_or_missed_session_never_backfills(self):
        for kind in ("late","missing","indicator"):
            if kind!="late":self.tearDown();self.setUp()
            self.cycle(START-STEP)
            for stamp in range(START,SIGNAL+STEP+1,STEP):
                if stamp==START+STEP:
                    if kind=="missing":continue
                    if kind=="indicator":self.cycle(stamp,snapshots={});continue
                self.cycle(stamp,delay=120000 if kind=="late" and stamp==START+STEP else 1000)
            self.assertEqual(live.records(self.conn),[])
            self.assertTrue(self.conn.execute("SELECT 1 FROM vwap_live_unknown WHERE session_ms=?",(START,)).fetchone())

    def test_low_volume_first_reclaim_consumed_before_later_qualifying_reclaim(self):
        self.entries[-1]=replace(self.entries[-1],volume=19.99)
        for stamp in range(START-STEP,SIGNAL+STEP+1,STEP):self.cycle(stamp)
        self.assertEqual(live.records(self.conn),[])
        self.overrides[SIGNAL+STEP]=candle(SIGNAL+STEP,open=100.1,low=99.8,high=100.2,close=99.9,volume=10)
        self.overrides[SIGNAL+2*STEP]=candle(SIGNAL+2*STEP,open=99.9,low=99.85,high=100.3,close=100.2,volume=100)
        self.cycle(SIGNAL+2*STEP);self.cycle(SIGNAL+3*STEP)
        self.assertEqual(live.records(self.conn),[])

    def test_expiry_remains_original_four_bars_and_no_rearm(self):
        r=self.capture();original=deepcopy(r)
        for stamp in range(SIGNAL+2*STEP,r["expires_ms"]+STEP,STEP):
            self.overrides[stamp-STEP]=candle(stamp-STEP,open=100.2,low=100.15,high=100.3,close=100.25,volume=10)
            self.cycle(stamp)
        actual=live.records(self.conn)[0]
        self.assertEqual(actual["status"],"EXPIRED")
        self.assertEqual(actual["expires_ms"],original["expires_ms"])
        self.assertEqual(actual["outcome_ms"],original["expires_ms"])
        for k in ("trigger","stop","target","created_ms","signal_candle_ms","observed_ms"):
            self.assertEqual(actual[k],original[k])

    def test_retry_bucket_never_overwrites_first_evidence_or_duplicates_candidate(self):
        self.capture()
        before=self.conn.execute("SELECT * FROM vwap_live_cycles ORDER BY bucket_ms").fetchall()
        seen=self.conn.execute("SELECT * FROM vwap_live_seen ORDER BY signal_key").fetchall()
        self.cycle(SIGNAL+STEP,delay=119000)
        self.assertEqual(self.conn.execute("SELECT * FROM vwap_live_cycles ORDER BY bucket_ms").fetchall(),before)
        self.assertEqual(self.conn.execute("SELECT * FROM vwap_live_seen ORDER BY signal_key").fetchall(),seen)
        self.assertEqual(len(live.records(self.conn)),1)

    def test_current_control_uses_same_unstarted_candle_and_original_two_bar_expiry(self):
        self.cycle(START-STEP);self.cycle(START)
        self.current=dict(ticker=CONTRACT.contract_name,direction="LONG",stage="READY",confirmed=True,retest_touched=True,
                          breakout_time_ms=START-16*STEP,breakout_level=100,entry_reference=100,stop_loss=90,take_profit=125)
        self.cycle(START+STEP)
        r=live.records(self.conn)[0]
        self.assertEqual(r["model"],"current_next_open")
        self.assertEqual(r["execution_start_ms"],START+2*STEP)
        self.assertEqual(r["expires_ms"],START+3*STEP)
        self.cycle(START+2*STEP);self.cycle(START+3*STEP)
        self.assertEqual(live.records(self.conn)[0]["filled_ms"],START+2*STEP)

    def test_current_preexisting_setup_is_not_captured(self):
        self.current=dict(ticker=CONTRACT.contract_name,direction="LONG",stage="READY",confirmed=True,retest_touched=True,
                          breakout_time_ms=START-32*STEP,breakout_level=100,entry_reference=100,stop_loss=90,take_profit=125)
        for stamp in range(START-STEP,START+3*STEP,STEP):self.cycle(stamp)
        self.assertEqual(live.records(self.conn),[])

    def test_display_limit_does_not_change_metrics_or_roi(self):
        self.capture()
        a=live.review(self.conn,now_ms=SIGNAL+STEP+1000,limit=0)
        b=live.review(self.conn,now_ms=SIGNAL+STEP+1000,limit=50)
        for k in ("cohorts","continuous_portfolios","total_records"):
            self.assertEqual(a[k],b[k])
        self.assertEqual(a["latest"],[])
        self.assertEqual(a["sample_status"],"INSUFFICIENT SAMPLE")

    def test_missing_buckets_and_indicator_coverage_not_zero_trade_results(self):
        self.cycle(START-STEP)
        r=live.review(self.conn,now_ms=START+1000)
        self.assertEqual(r["status"],"WAITING_FOR_FIRST_OBSERVATION")
        c=r["cohorts"][0]["coverage"]
        self.assertEqual(c["pending_current_buckets"],1)
        self.assertEqual(c["overdue_missing_buckets"],0)
        self.assertIsNone(c["indicator_coverage_pct"])
        r=live.review(self.conn,now_ms=START+120000)
        self.assertEqual(r["cohorts"][0]["coverage"]["overdue_missing_buckets"],1)

    def test_additive_migration_preserves_other_ledgers(self):
        self.conn.execute("CREATE TABLE push_subscriptions(id TEXT)")
        self.conn.execute("INSERT INTO push_subscriptions VALUES('existing')")
        self.conn.execute("CREATE TABLE pending_live_meta(payload TEXT)")
        self.conn.execute("INSERT INTO pending_live_meta VALUES('original-origin')")
        self.conn.execute("CREATE TABLE live_execution_state(payload TEXT)")
        self.conn.execute("INSERT INTO live_execution_state VALUES('armed-and-consumed-control')")
        live.initialize(self.conn,now_ms=START+99*STEP)
        for table,expected in (("push_subscriptions","existing"),("pending_live_meta","original-origin"),("live_execution_state","armed-and-consumed-control")):
            self.assertEqual(self.conn.execute("SELECT * FROM "+table).fetchone()[0],expected)

    def test_changed_policy_or_analyzer_stops_collection(self):
        self.capture()
        settings=replace(SETTINGS,min_rr=1.5)
        with self.assertRaisesRegex(ValueError,"frozen policy"):
            live.cycle(self.conn,contracts=self.contracts,snapshots=self.snapshots(SIGNAL+2*STEP),analyze=self.analyze,settings=settings,now_ms=SIGNAL+2*STEP+1000)
        live.record_error(self.conn,"ValueError")
        self.assertEqual(live.review(self.conn,now_ms=SIGNAL+2*STEP)["status"],"PAUSED_ERROR")

    def test_continuous_portfolio_keeps_unknown_capital_and_same_ticker_next_week(self):
        r=self.capture();cursor=r["execution_start_ms"]
        good=dict(setup_id=r["setup_id"],confirmation_color_ok=True,confirmation_level_ok=True,retest_touched=True,stop_valid=True)
        opened=live.evaluate_bar(r,candle(cursor,open=r["trigger"],close=r["trigger"],low=r["trigger"]-.01,high=r["trigger"]+.01),good,cursor=cursor)
        for status in ("OPEN","AMBIGUOUS","DATA_GAP"):
            first=dict(opened,status=status,final_net_r=None)
            live.save_record(self.conn,first)
            later=dict(opened,key=opened["key"]+":next",setup_id=opened["setup_id"]+":next",
                       cohort_start_ms=START+live.WEEK,created_ms=START+live.WEEK+1,
                       signal_candle_ms=START+live.WEEK-STEP,observed_ms=START+live.WEEK+1000,
                       filled_ms=START+live.WEEK+STEP,outcome_ms=START+live.WEEK+2*STEP,
                       status="TP",final_net_r=2,net_pnl_per_unit=2*opened["net_risk"],exit_price=opened["target"]*(1-live.control.SLIP))
            self.conn.execute("INSERT OR REPLACE INTO vwap_live_signals VALUES(?,?,?)",(later["key"],live.encode(later),later["created_ms"]))
            result=live.review(self.conn,now_ms=START+live.WEEK+3*STEP)
            self.assertEqual(len(result["cohorts"]),2)
            self.assertGreater(result["cohorts"][1]["portfolios"][study.MODEL]["closed_portfolio_roi_pct"],0)
            actual=result["continuous_portfolios"][study.MODEL]
            self.assertEqual(actual["filled"],1)
            self.assertEqual(actual["resolved"],0)
            self.assertIsNone(actual["closed_portfolio_roi_pct"])
            self.assertEqual(actual["exclusions"]["CAPACITY_OR_TICKER"],1)

    def test_open_stop_gap_is_charged_at_worse_open(self):
        r=fixtures.record();cursor=r["created_ms"]-1+STEP
        r.update(execution_start_ms=cursor,next_candle_ms=cursor)
        good=dict(setup_id=r["setup_id"],confirmation_color_ok=True,confirmation_level_ok=True,retest_touched=True,stop_valid=True)
        opened=live.evaluate_bar(r,candle(cursor,open=r["trigger"],close=r["trigger"],low=r["trigger"]-.01,high=r["trigger"]+.01),good,cursor=cursor)
        opened["next_candle_ms"]=cursor+STEP
        gap=r["stop"]-1
        result=live.evaluate_bar(opened,candle(cursor+STEP,open=gap,close=gap,low=gap-.1,high=gap+.1),None,cursor=cursor+STEP)
        self.assertEqual(result["status"],"SL")
        self.assertLess(result["final_net_r"],-1)

    def test_invalid_capture_cursor_cannot_use_signal_or_elapsed_bar(self):
        r=self.capture()
        for cursor in (r["created_ms"]-1,r["execution_start_ms"]-1,r["execution_start_ms"]+STEP):
            with self.assertRaisesRegex(ValueError,"cursor"):
                live.evaluate_bar(r,None,None,cursor=cursor)


if __name__ == "__main__":
    unittest.main()
