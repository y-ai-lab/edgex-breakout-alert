"""Simulated skipped-entry evidence; never calls the exchange or places real orders."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from httpx import ASGITransport, AsyncClient
from analysis_terminal import execution_entry_review as review, execution_health, server
from analysis_terminal import test_live_execution as fixtures

class ReasonTests(unittest.TestCase):
    def test_known_and_untrusted_errors_are_allowlisted(self):
        for code in review.SKIP_LABELS:
            if code != "PRE_ARM_BASELINE":
                self.assertEqual(review.rejection_code(fixtures.ExecutionError(code),execution_error=fixtures.ExecutionError),code)
        for exc in [fixtures.ExecutionError("private-token"),fixtures.ExecutionError("INVALID_QUOTE", "private-token"),fixtures.ExecutionError(["private-token"]),KeyError("private-token"),TypeError("private-token")]:
            code=review.rejection_code(exc,execution_error=fixtures.ExecutionError)
            self.assertIn(code,review.SKIP_LABELS)
            self.assertNotIn("private",code)
        self.assertNotEqual(review.rejection_code(fixtures.ExecutionError("PRE_ARM_BASELINE"),execution_error=fixtures.ExecutionError),"PRE_ARM_BASELINE")

    def test_old_and_untrusted_reason_are_not_reconstructed_as_baseline(self):
        for code in (None,"private-token",[],{},True):
            label=review.label(code)
            self.assertIn("記録なし",label)
            self.assertNotIn("基準",label)
            self.assertNotIn("private",label)

    def test_future_event_time_does_not_claim_verified_reason(self):
        h=execution_health.report([],{},now_ms=fixtures.NOW,events=[dict(status="SKIPPED",observed_ms=fixtures.NOW+1,skip_code="LIVE_RR_BELOW_2")])
        self.assertIsNone(h["recent_events"][0]["observed_ms"])
        self.assertNotIn("RR",h["recent_events"][0]["label"])

class EngineTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ExecutionTests.setUp
    records = fixtures.ExecutionTests.records
    state = fixtures.ExecutionTests.state
    armed = fixtures.ExecutionTests.armed

    async def test_rejected_ready_keeps_reason_and_is_never_retried(self):
        await self.armed()
        row=fixtures.candidate(take_profit=110)
        original=copy.deepcopy(row)
        await self.engine.cycle([row],snapshot_ms=self.now)
        record=self.records()[0]
        self.assertEqual(record["status"],"SKIPPED")
        self.assertEqual(record["skip_code"],"INVALID_STRUCTURAL_READY")
        self.assertEqual(row,original)
        self.exchange.quote=AsyncMock(side_effect=AssertionError("consumed setup must not request quote"))
        await self.engine.cycle([fixtures.candidate()],snapshot_ms=self.now)
        self.assertEqual(self.records(),[record])
        self.assertEqual(self.exchange.creates,[])
        self.assertTrue(self.state()["armed"])

    async def test_signal_deadline_stays_strict_and_scan_does_not_extend_it(self):
        await self.armed()
        self.now=fixtures.NOW+90000 # exactly 120s since confirmation close
        await self.engine.cycle([fixtures.candidate()],snapshot_ms=self.now)
        self.assertEqual(self.records()[0]["skip_code"],"STALE_OR_PRE_ARM_SIGNAL")
        self.assertEqual(self.exchange.creates,[])

    async def test_unknown_error_content_is_not_saved(self):
        await self.armed()
        self.exchange.quote=AsyncMock(side_effect=fixtures.ExecutionError("private-exchange-response"))
        await self.engine.cycle([fixtures.candidate()],snapshot_ms=self.now)
        self.assertEqual(self.records()[0]["skip_code"],"ENTRY_CHECK_UNCLASSIFIED")
        with self.db() as db:
            self.assertNotIn("private-exchange-response",json.dumps(list(db.iterdump())))
        self.assertEqual(self.exchange.creates,[])

    async def test_invalid_input_uses_generic_code_without_raw_message(self):
        await self.armed()
        self.exchange.quote=AsyncMock(side_effect=KeyError("private-account-value"))
        await self.engine.cycle([fixtures.candidate()],snapshot_ms=self.now)
        self.assertEqual(self.records()[0]["skip_code"],"ENTRY_INPUT_INVALID")
        self.assertNotIn("private-account-value",json.dumps(self.records()))

    async def test_known_quote_failure_is_preserved(self):
        await self.armed()
        self.exchange.quote=AsyncMock(side_effect=fixtures.ExecutionError("INVALID_QUOTE"))
        await self.engine.cycle([fixtures.candidate()],snapshot_ms=self.now)
        self.assertEqual(self.records()[0]["skip_code"],"INVALID_QUOTE")

    async def test_baseline_records_are_distinct_and_old_records_unchanged(self):
        row=fixtures.candidate()
        with self.db() as db:
            fixtures.live.save(db,dict(setup_id="legacy",status="SKIPPED"),self.now)
        await self.engine.arm([row])
        self.now=fixtures.NOW
        rows={r["setup_id"]:r for r in self.records()}
        self.assertEqual(rows["legacy"],dict(setup_id="legacy",status="SKIPPED"))
        self.assertEqual(rows[row["setup_id"]]["skip_code"],"PRE_ARM_BASELINE")
        await self.engine.cycle([row],snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates,[])

class APITests(unittest.IsolatedAsyncioTestCase):
    async def test_reason_join_is_read_only_sanitized_and_legacy_safe(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(server,"DB_PATH",Path(directory)/"test.db"),patch.object(server,"_live_execution_config",fixtures.configuration()),patch.object(server.time,"time",return_value=fixtures.NOW/1000):
            server._init_db()
            with server._live_execution_db() as db:
                for idx,code in enumerate(["LIVE_RR_BELOW_2","PRE_ARM_BASELINE",None,"private-response",["private-response"]]):
                    row=dict(setup_id="private-setup-"+str(idx),status="SKIPPED",ticker="private-ticker",quantity="private-size",skip_code=code)
                    fixtures.live.save(db,row,fixtures.NOW)
                db.commit();before=list(db.iterdump())
            async with AsyncClient(transport=ASGITransport(app=server.app),base_url="https://test") as client:
                for _ in range(2):
                    response=await client.get("/api/live-execution")
                    self.assertEqual(response.status_code,200)
                    events=response.json()["health"]["recent_events"]
                    self.assertEqual(len(events),5)
                    self.assertIn("RR",events[-1]["label"])
                    self.assertIn("基準",events[-2]["label"])
                    self.assertTrue(all("記録なし" in e["label"] for e in events[:3]))
                    self.assertNotIn("private-",response.text)
            with server._live_execution_db() as db:self.assertEqual(list(db.iterdump()),before)
