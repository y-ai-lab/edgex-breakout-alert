"""Observed execution events stay separate from migration snapshots and trade totals."""
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal import paper_execution as paper
from analysis_terminal.test_paper_execution import BASE, NOW, STEP, candle, row
from analysis_terminal import test_storage as storage

server = storage.server


class PaperEventTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        paper.initialize(self.conn, now_ms=NOW-100, min_rr=2, previous_signals=[])
        self.conn.commit()

    def tick(self, rows=(), candles=(), now=NOW):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            paper.cycle(self.conn, rows=list(rows), candles_by_ticker={"TESTUSDC":list(candles)},
                        now_ms=now, snapshot_age_ms=0)

    def events(self, **args):
        return paper.event_report(self.conn, **args)["events"]

    def test_pending_open_stop_clocks_and_restart_do_not_duplicate(self):
        self.tick([row()])
        self.tick(candles=[candle(high=105, low=85, close=90)], now=BASE+STEP+1000)
        self.assertEqual([e["status"] for e in self.events()], ["PENDING", "OPEN"])
        self.tick(candles=[candle(high=105, low=85, close=90)], now=BASE+2*STEP)
        events = self.events()
        self.assertEqual([e["status"] for e in events], ["PENDING", "OPEN", "SL"])
        self.assertEqual([e["previous_status"] for e in events], [None, "PENDING", "OPEN"])
        self.assertEqual([e["observed_ms"] for e in events], [NOW, BASE+STEP+1000, BASE+2*STEP])
        self.assertEqual([e["market_ms"] for e in events], [None, BASE+STEP, BASE+2*STEP])
        self.assertEqual({e["origin"] for e in events}, {"LIVE_CYCLE"})
        cash = paper.report(self.conn, now_ms=BASE+2*STEP)["account"]["cash_usdc"]
        paper.initialize(self.conn, now_ms=BASE+2*STEP+1, min_rr=2, previous_signals=[])
        self.conn.commit()
        self.tick([row(), row()], [candle(high=105,low=85,close=90)], BASE+3*STEP)
        self.assertEqual(self.events(), events)
        self.assertEqual(paper.report(self.conn, now_ms=BASE+3*STEP)["account"]["cash_usdc"], cash)
        self.assertEqual(paper.account(self.conn)["event_tracking_started_ms"], NOW-100)

    def test_delayed_single_cycle_preserves_fill_before_long_and_short_target(self):
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                conn = sqlite3.connect(":memory:")
                try:
                    paper.initialize(conn, now_ms=NOW-100, min_rr=2, previous_signals=[])
                    paper.cycle(conn, rows=[row(side=side)], candles_by_ticker={}, now_ms=NOW, snapshot_age_ms=0)
                    c = candle(high=132,low=95,close=130) if side=="LONG" else candle(high=105,low=68,close=70)
                    paper.cycle(conn, rows=[], candles_by_ticker={"TESTUSDC":[c]}, now_ms=BASE+2*STEP, snapshot_age_ms=0)
                    e = paper.event_report(conn)["events"]
                    self.assertEqual([x["status"] for x in e], ["PENDING", "OPEN", "TP"])
                    self.assertEqual(e[1]["observed_ms"], e[2]["observed_ms"])
                    self.assertLess(e[1]["id"], e[2]["id"])
                    self.assertNotIn("exit_price", e[1]["order"])
                    self.assertEqual(e[2]["previous_status"], "OPEN")
                    self.assertEqual(paper.report(conn,now_ms=BASE+2*STEP)["metrics"]["resolved"],1)
                finally:
                    conn.close()

    def test_gap_recovery_logs_quality_once_without_creating_extra_trade(self):
        self.tick([row()])
        self.tick(candles=[candle()],now=BASE+STEP+1)
        self.tick(candles=[],now=BASE+2*STEP)
        gap = self.events()[-1]
        self.assertEqual((gap["status"],gap["quality"],gap["reason"]), ("OPEN","HISTORY_GAP","MISSING_CLOSED_CANDLE"))
        self.tick(candles=[],now=BASE+2*STEP+1)
        self.assertEqual(self.events()[-1], gap)
        self.tick(candles=[candle()],now=BASE+2*STEP+2)
        restored = self.events()[-1]
        self.assertEqual((restored["status"],restored["quality"],restored["previous_quality"]), ("OPEN","TRACKING","HISTORY_GAP"))
        self.assertEqual(paper.report(self.conn,now_ms=BASE+2*STEP+2)["metrics"]["records"],1)

    def test_ambiguous_reject_expire_and_operator_cancel_have_explanations(self):
        self.tick([row()])
        self.tick(candles=[candle(high=140,low=80)],now=BASE+2*STEP)
        e=self.events()[-1]
        self.assertEqual((e["status"],e["reason"]),("AMBIGUOUS","TP_SL_SAME_CANDLE"))
        self.assertNotIn("net_pnl_usdc",e["order"])
        self.assertIsNone(paper.report(self.conn,now_ms=BASE+2*STEP)["account"]["cash_usdc"])
        self.tick([row(ticker="SECONDUSDC")],now=BASE+2*STEP+1)
        self.assertEqual(self.events()[-1]["reason"],"ACCOUNT_PAUSED")
        other=sqlite3.connect(":memory:")
        try:
            paper.initialize(other,now_ms=NOW-100,min_rr=2,previous_signals=[])
            paper.cycle(other,rows=[row()],candles_by_ticker={},now_ms=NOW,snapshot_age_ms=0)
            paper.set_pause(other,paused=True,now_ms=NOW+1)
            e=paper.event_report(other)["events"][-1]
            self.assertEqual((e["status"],e["reason"],e["origin"]),("CANCELLED","OPERATOR_STOP","OPERATOR_CONTROL"))
            paper.set_pause(other,paused=False,now_ms=NOW+2)
            paper.cycle(other,rows=[row(ticker="OTHERUSDC")],candles_by_ticker={},now_ms=NOW+3,snapshot_age_ms=0)
            paper.cycle(other,rows=[],candles_by_ticker={},now_ms=NOW+2*STEP+4,snapshot_age_ms=0)
            e=paper.event_report(other)["events"][-1]
            self.assertEqual((e["status"],e["reason"]),("EXPIRED","FILL_OBSERVATION_TIMEOUT"))
        finally:
            other.close()

    def test_legacy_migration_imports_state_once_preserves_cash_policy_and_pause(self):
        self.tick([row()])
        self.tick(candles=[candle(high=105,low=85,close=90)],now=BASE+2*STEP)
        before_orders=paper.orders(self.conn)
        before_state=paper.account(self.conn)
        before_state.pop("event_tracking_started_ms")
        before_state.update(paused=True,pause_reason="OPERATOR_STOP",pause_ms=BASE+2*STEP+1)
        with self.conn:
            paper._save_account(self.conn,before_state)
            self.conn.execute("DROP TABLE simulated_order_events")
        for when in (BASE+3*STEP,BASE+4*STEP):
            with self.conn:
                paper.initialize(self.conn,now_ms=when,min_rr=9,previous_signals=[])
        self.assertEqual(paper.orders(self.conn),before_orders)
        state=paper.account(self.conn)
        self.assertEqual(state.pop("event_tracking_started_ms"),BASE+3*STEP)
        self.assertEqual(state,before_state)
        e=self.events()
        self.assertEqual(len(e),1)
        self.assertEqual((e[0]["origin"],e[0]["status"],e[0]["observed_ms"]),("MIGRATION_SNAPSHOT","SL",BASE+3*STEP))
        self.assertIsNone(e[0]["previous_status"])
        self.assertEqual(e[0]["order"],before_orders[0])
        self.assertEqual(paper.event_report(self.conn)["origin_counts"],{"MIGRATION_SNAPSHOT":1})

    def test_event_write_failure_rolls_back_order_fill_fees_and_event_atomically(self):
        self.tick([row()])
        before=list(self.conn.iterdump())
        with patch.object(paper,"_append_event",side_effect=sqlite3.OperationalError("controlled event failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.tick(candles=[candle()],now=BASE+STEP+1)
        self.assertEqual(list(self.conn.iterdump()),before)

    def test_cursor_filter_and_read_only_report_cannot_mix_setups_or_trade_metrics(self):
        first=row()
        second=row(breakout=BASE-8*STEP)
        self.tick([first,second])
        self.assertEqual(paper.orders(self.conn)[1]["reason"],"TICKER_ALREADY_OPEN")
        before=list(self.conn.iterdump())
        a=paper.event_report(self.conn,limit=1)
        b=paper.event_report(self.conn,after_id=a["next_after_id"],limit=1)
        self.assertTrue(a["has_more"])
        self.assertFalse(b["has_more"])
        self.assertEqual(a["total_count"],b["total_count"])
        self.assertNotEqual(a["events"][0]["setup_id"],b["events"][0]["setup_id"])
        scoped=paper.event_report(self.conn,order_id="current:"+first["setup_id"])
        self.assertEqual(scoped["total_count"],1)
        self.assertEqual(scoped["events"][0]["status"],"PENDING")
        empty=paper.event_report(self.conn,after_id=b["next_after_id"])
        self.assertEqual(empty["events"],[])
        self.assertEqual(empty["next_after_id"],b["next_after_id"])
        self.assertEqual(paper.event_report(self.conn,order_id="' OR 1=1 --")["events"],[])
        self.assertEqual(list(self.conn.iterdump()),before)


class PaperEventApiTests(unittest.IsolatedAsyncioTestCase):
    setUp=storage.StorageTests.setUp

    async def test_http_cursor_validation_readonly_and_push_and_history_preserved(self):
        stamp=self.now_ms//STEP*STEP
        r=row(latest_15m_time_ms=stamp-STEP,breakout_time_ms=stamp-16*STEP)
        with server._db_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            paper.cycle(conn,rows=[r],candles_by_ticker={},now_ms=stamp+1,snapshot_age_ms=0)
        server._save_market_snapshot(dict(time_ms=stamp,ready=0))
        server._init_db()
        with server._db_connect() as conn:
            before=list(conn.iterdump())
        with patch.object(server,"_scan_market_rows",AsyncMock(side_effect=AssertionError("No scan from event API"))):
            async with AsyncClient(transport=ASGITransport(app=server.app),base_url="http://test") as client:
                response=await client.get("/api/paper-execution/events",params={"order_id":"current:"+r["setup_id"],"limit":1})
                self.assertEqual(response.status_code,200)
                e=response.json()
                self.assertEqual(e["events"][0]["status"],"PENDING")
                self.assertFalse(e["real_orders_enabled"])
                self.assertFalse(e["eligible_for_live_promotion"])
                self.assertEqual((await client.get("/api/paper-execution/events",params={"after_id":e["next_after_id"]})).json()["events"],[])
                for params in ({"after_id":-1},{"limit":0},{"limit":201},{"order_id":""}):
                    self.assertEqual((await client.get("/api/paper-execution/events",params=params)).status_code,422)
                self.assertEqual((await client.post("/api/paper-execution/events")).status_code,405)
        with server._db_connect() as conn:
            self.assertEqual(list(conn.iterdump()),before)
        self.assertEqual(server._subscription_count(),1)
        self.assertEqual(len(server._load_market_history()),1)
        self.assertEqual(server._load_paper_signals(),[])
        self.assertEqual(server._load_push_events(),[])
