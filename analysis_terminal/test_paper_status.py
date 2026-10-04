"""Operational diagnostics include all active orders, never only a display page."""
import unittest

from analysis_terminal import paper_execution as paper
from analysis_terminal import test_paper_execution as fixtures

BASE, NOW, STEP = fixtures.BASE, fixtures.NOW, fixtures.STEP
row, candle = fixtures.row, fixtures.candle


class PaperStatusTests(unittest.TestCase):
    setUp = fixtures.PaperExecutionTests.setUp
    tick = fixtures.PaperExecutionTests.tick
    open = fixtures.PaperExecutionTests.open
    latest = fixtures.PaperExecutionTests.latest

    def test_waiting_ready_fill_open_pause_and_ambiguous_are_distinct(self):
        self.assertEqual(paper.report(self.conn, now_ms=NOW)["tracking"]["state"], "NOT_STARTED")
        self.assertEqual(self.tick()["tracking"]["state"], "WAITING_READY")
        pending = self.tick([row()])["tracking"]
        self.assertEqual(pending["state"], "WAITING_FILL")
        self.assertEqual(pending["active_status_counts"], dict(PENDING=1, OPEN=0, AMBIGUOUS=0))
        self.tick(candles={"TESTUSDC": [candle()]}, now=BASE+STEP+1)
        self.assertEqual(paper.report(self.conn, now_ms=BASE+STEP+1)["tracking"]["state"], "TRACKING")
        with self.conn:
            paper.set_pause(self.conn, paused=True, now_ms=BASE+STEP+2)
        self.assertEqual(paper.report(self.conn, now_ms=BASE+STEP+2)["tracking"]["state"], "PAUSED")
        result = self.tick(candles={"TESTUSDC": [candle(high=140, low=80)]}, now=BASE+2*STEP)
        self.assertEqual(result["tracking"]["state"], "AMBIGUOUS")
        self.assertEqual(result["tracking"]["active_status_counts"]["AMBIGUOUS"], 1)
        self.assertIsNone(result["account"]["cash_usdc"])

    def test_hidden_old_gap_blocks_diagnostics_even_when_page_shows_only_rejection(self):
        self.open()
        self.tick(now=BASE+2*STEP)
        self.tick([row(ticker="NEWUSDC", latest_15m_time_ms=BASE+STEP)], now=BASE+2*STEP+1)
        before = list(self.conn.iterdump())
        short = paper.report(self.conn, now_ms=BASE+2*STEP+1, limit=1)
        full = paper.report(self.conn, now_ms=BASE+2*STEP+1, limit=200)
        self.assertEqual(short["latest"][0]["status"], "REJECTED")
        self.assertEqual(short["tracking"], full["tracking"])
        self.assertEqual(short["tracking"]["state"], "DATA_INCOMPLETE")
        self.assertEqual(short["tracking"]["data_issue_counts"], {"HISTORY_GAP": 1})
        self.assertEqual(short["tracking"]["data_issue_count"], 1)
        self.assertEqual(short["tracking"]["active_status_counts"]["OPEN"], 1)
        self.assertEqual(list(self.conn.iterdump()), before)
        restored = self.tick(candles={"TESTUSDC": [candle()]}, now=BASE+2*STEP+2)
        self.assertEqual(restored["tracking"]["state"], "TRACKING")
        self.assertEqual(restored["tracking"]["data_issue_count"], 0)

    def test_pending_missing_fill_and_data_error_then_expired_are_classified(self):
        self.tick([row()])
        missing = self.tick(now=BASE+STEP+1)
        self.assertEqual(missing["tracking"]["state"], "DATA_INCOMPLETE")
        self.assertEqual(missing["tracking"]["data_issue_counts"], {"MISSING_FILL_CANDLE": 1})
        invalid = self.tick(candles={"TESTUSDC": [candle(cid="wrong")]}, now=BASE+STEP+2)
        self.assertEqual(invalid["tracking"]["data_issue_counts"], {"DATA_ERROR": 1})
        expired = self.tick(now=NOW+2*STEP+1)
        self.assertEqual(expired["tracking"]["state"], "WAITING_READY")
        self.assertEqual(expired["tracking"]["data_issue_count"], 0)
        self.assertEqual(expired["tracking"]["active_status_counts"], dict(PENDING=0, OPEN=0, AMBIGUOUS=0))

    def test_cycle_clock_unknown_future_and_stale_warn_without_mutating_state(self):
        self.tick()
        before = list(self.conn.iterdump())
        for now, expected in ((NOW-1, "COLLECTOR_STALE"), (NOW+2*STEP-1, "WAITING_READY"), (NOW+2*STEP, "COLLECTOR_STALE")):
            result = paper.report(self.conn, now_ms=now)
            self.assertEqual(result["tracking"]["state"], expected)
            self.assertEqual(result["account"]["collector_stale"], expected=="COLLECTOR_STALE")
        self.assertEqual(list(self.conn.iterdump()), before)
