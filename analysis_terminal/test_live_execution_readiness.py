"""Read-only dedicated-account diagnosis; never cancels or closes a position."""

import json
import unittest
from unittest.mock import AsyncMock

from analysis_terminal import live_execution as live
from analysis_terminal import test_live_execution as fixtures


class ArmReadinessTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ExecutionTests.setUp
    records = fixtures.ExecutionTests.records
    state = fixtures.ExecutionTests.state

    def report(self):
        with self.db() as conn:
            return live.report(conn, self.config, now_ms=self.now)

    async def test_flat_read_only_check_records_empty_reasons_without_arming(self):
        self.config.mode = "READ_ONLY"
        await self.engine.preflight()
        report = self.report()
        self.assertEqual(report["preflight_arm_blockers"], [])
        self.assertTrue(report["preflight_arm_blockers_current"])
        self.assertFalse(report["armed"])
        self.assertFalse(report["real_orders_enabled"])
        self.assertEqual(self.exchange.creates, [])
        self.assertEqual(self.exchange.cancels, [])

    async def test_existing_position_is_reported_but_never_closed(self):
        self.exchange.positions["private-contract"] = live.decimal(".123")
        await self.engine.preflight()
        report = self.report()
        self.assertEqual(report["preflight_arm_blockers"], ["EXISTING_ACCOUNT_POSITION"])
        self.assertNotIn("private-contract", json.dumps(report))
        self.assertNotIn(".123", json.dumps(report))
        with self.assertRaisesRegex(live.ExecutionError, "DEDICATED_FLAT_ACCOUNT_REQUIRED"):
            await self.engine.arm()
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.positions["private-contract"], live.decimal(".123"))
        self.assertEqual(self.exchange.creates, [])
        self.assertEqual(self.exchange.cancels, [])

    async def test_external_orders_are_reported_but_never_cancelled(self):
        self.exchange.external_orders = [{"clientOrderId": "private-order", "price": "secret-price"}]
        await self.engine.preflight()
        report = self.report()
        self.assertEqual(report["preflight_arm_blockers"], ["ACTIVE_EXCHANGE_ORDERS"])
        self.assertNotIn("private-order", json.dumps(report))
        self.assertNotIn("secret-price", json.dumps(report))
        with self.assertRaisesRegex(live.ExecutionError, "DEDICATED_FLAT_ACCOUNT_REQUIRED"):
            await self.engine.arm()
        self.assertEqual(self.exchange.cancels, [])

    async def test_uncertain_ledger_is_reported_and_preserved(self):
        with self.db() as conn:
            live.save(conn, {"setup_id": "private-setup", "status": "SEND_UNKNOWN"}, self.now)
        await self.engine.preflight()
        self.assertEqual(self.report()["preflight_arm_blockers"], ["UNRESOLVED_EXECUTION_LEDGER"])
        self.assertNotIn("private-setup", json.dumps(self.report()))
        with self.assertRaisesRegex(live.ExecutionError, "DEDICATED_FLAT_ACCOUNT_REQUIRED"):
            await self.engine.arm()
        self.assertEqual(self.records()[0]["status"], "SEND_UNKNOWN")
        self.assertEqual(self.exchange.creates, [])

    async def test_combined_reasons_use_same_predicate_as_arm(self):
        self.exchange.positions["another-contract"] = live.decimal(-1)
        self.exchange.external_orders = [{"clientOrderId": "external"}]
        with self.db() as conn:
            live.save(conn, {"setup_id": "uncertain", "status": "PREPARED"}, self.now)
        await self.engine.preflight()
        self.assertEqual(self.report()["preflight_arm_blockers"], [
            "EXISTING_ACCOUNT_POSITION", "ACTIVE_EXCHANGE_ORDERS", "UNRESOLVED_EXECUTION_LEDGER"
        ])
        with self.assertRaisesRegex(live.ExecutionError, "DEDICATED_FLAT_ACCOUNT_REQUIRED"):
            await self.engine.arm()

    async def test_failed_fresh_check_clears_old_flat_result(self):
        await self.engine.preflight()
        self.exchange.account_error = True
        with self.assertRaises(live.ExecutionError):
            await self.engine.preflight()
        self.assertIsNone(self.report()["preflight_arm_blockers"])
        self.assertFalse(self.report()["preflight_arm_blockers_current"])

    async def test_failed_metadata_check_does_not_keep_old_blockers(self):
        self.exchange.positions["contract"] = live.decimal(1)
        await self.engine.preflight()
        self.exchange.metadata = AsyncMock(return_value={})
        with self.assertRaises(live.ExecutionError):
            await self.engine.preflight()
        self.assertIsNone(self.report()["preflight_arm_blockers"])
        self.assertFalse(self.report()["preflight_arm_blockers_current"])

    async def test_observation_age_boundary_and_clock_reversal(self):
        await self.engine.preflight()
        original = self.now
        for delta, current in [(29999, True), (30000, False), (-1, False)]:
            with self.subTest(delta=delta):
                self.now = original + delta
                self.assertEqual(self.report()["preflight_arm_blockers_current"], current)
        self.assertEqual(self.exchange.creates, [])

    async def test_recheck_after_manual_resolution_does_not_auto_arm(self):
        self.exchange.positions["contract"] = live.decimal(1)
        await self.engine.preflight()
        self.exchange.positions.clear()
        await self.engine.preflight()
        self.assertEqual(self.report()["preflight_arm_blockers"], [])
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.creates, [])

    async def test_legacy_state_without_diagnostic_is_unknown_and_kept(self):
        before = self.state()
        self.assertNotIn("last_preflight_arm_blockers", before)
        report = self.report()
        self.assertIsNone(report["preflight_arm_blockers"])
        self.assertFalse(report["preflight_arm_blockers_current"])
        with self.db() as conn:
            live.initialize(conn, now_ms=self.now + 1000)
        self.assertEqual(before, self.state())


if __name__ == "__main__":
    unittest.main()
