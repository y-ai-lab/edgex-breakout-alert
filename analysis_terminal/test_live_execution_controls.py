"""Private Railway control requests: once-only, read-only check, no stale arm."""

import io
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from analysis_terminal import live_execution as live
from analysis_terminal import test_live_execution as fixtures

candidate = fixtures.candidate
NOW = fixtures.NOW


def request(action="check", token=None):
    return action + ":" + (token or str(uuid.uuid4()))


class ControlTests(unittest.IsolatedAsyncioTestCase):
    records = fixtures.ExecutionTests.records
    state = fixtures.ExecutionTests.state

    def setUp(self):
        fixtures.ExecutionTests.setUp(self)
        self.now = NOW
        with self.db() as conn:
            conn.execute("CREATE TABLE paper_signals(payload TEXT)")

    async def apply(self, command, **kwargs):
        await live.apply_control(self.engine, command, **kwargs)

    def control(self):
        with self.db() as conn:
            return live.report(conn, self.config, now_ms=self.now)["operator_control"]

    async def test_empty_request_has_no_effect_or_network(self):
        self.engine.adapter = None
        factory = Mock(side_effect=AssertionError("unexpected client"))
        await self.apply("", adapter_factory=factory)
        factory.assert_not_called()
        self.assertIsNone(self.control())

    async def test_pause_works_off_without_credentials_or_sdk(self):
        self.config.mode = "OFF"
        self.engine.adapter = None
        with self.db() as conn:
            s = live.state(conn)
            s["armed"] = True
            live.save_state(conn, s)
        factory = Mock(side_effect=AssertionError("unexpected SDK"))
        await self.apply(request("pause"), adapter_factory=factory)
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.control()["status"], "DONE")
        factory.assert_not_called()

    async def test_invalid_request_never_echoes_secret(self):
        for command in [
            "arm:SECRET_KEY",
            "unknown:" + str(uuid.uuid4()),
            "arm:" + str(uuid.uuid1()),
            "arm:extra:secret",
        ]:
            with self.subTest(command=command):
                await self.apply(command)
                self.assertFalse(self.state()["armed"])
                self.assertEqual(self.state()["reason"], "INVALID_CONTROL_REQUEST")
                self.assertIsNone(self.control())
                self.assertNotIn("SECRET", json.dumps(self.state()))

    async def test_read_only_check_never_arms_or_mutates_even_with_position(self):
        for mode in ["READ_ONLY", "LIVE"]:
            with self.subTest(mode=mode):
                self.config.mode = mode
                self.exchange.positions["10000001"] = live.decimal(1)
                await self.apply(request("check"))
                self.assertEqual(self.control()["status"], "DONE")
                self.assertFalse(self.state()["armed"])
                self.assertEqual(self.exchange.creates, [])
                self.assertEqual(self.exchange.cancels, [])

    async def test_check_lazy_client_is_created_once(self):
        self.engine.adapter = None
        factory = Mock(return_value=self.exchange)
        command = request("check")
        await self.apply(command, adapter_factory=factory)
        await self.apply(command, adapter_factory=factory)
        factory.assert_called_once_with()

    async def test_request_is_committed_before_any_connection_attempt(self):
        original = self.exchange.metadata

        async def verify_commit():
            with self.db() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM live_execution_controls"
                    ).fetchone()[0],
                    "PROCESSING",
                )
            return await original()

        self.exchange.metadata = verify_commit
        await self.apply(request("check"))
        self.assertEqual(self.control()["status"], "DONE")

    async def test_arm_baselines_old_ready_and_never_orders_it(self):
        with self.db() as conn:
            conn.execute(
                "INSERT INTO paper_signals VALUES(?)", (json.dumps(candidate()),)
            )
        await self.apply(request("arm"))
        self.assertEqual(self.control()["status"], "DONE")
        self.assertTrue(self.state()["armed"])
        self.assertEqual(self.records()[0]["status"], "SKIPPED")
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates, [])

    async def test_same_arm_is_not_repeated_after_error_or_redeploy(self):
        command = request("arm")
        await self.apply(command)
        with self.db() as conn:
            live.pause(conn, "DAILY_EQUITY_LOSS_LIMIT")
        # Simulate a new process with a still-set Railway variable.
        restarted = live.Engine(
            self.db, self.config, self.exchange, clock=lambda: self.now
        )
        await live.apply_control(restarted, command)
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.state()["reason"], "DAILY_EQUITY_LOSS_LIMIT")

    async def test_old_pause_is_not_replayed_after_new_arm(self):
        stop = request("pause")
        await self.apply(stop)
        await self.apply(request("arm"))
        await self.apply(stop)
        self.assertTrue(self.state()["armed"])

    async def test_refused_off_arm_is_consumed_not_delayed_until_live(self):
        self.config.mode = "OFF"
        command = request("arm")
        await self.apply(command)
        self.assertEqual(self.control()["status"], "REFUSED")
        self.config.mode = "LIVE"
        await self.apply(command)
        self.assertFalse(self.state()["armed"])

    async def test_read_only_arm_is_refused(self):
        self.config.mode = "READ_ONLY"
        await self.apply(request("arm"))
        self.assertEqual(self.control()["status"], "REFUSED")
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.creates, [])

    async def test_changed_action_for_same_id_cannot_arm(self):
        token = str(uuid.uuid4())
        await self.apply(request("check", token))
        await self.apply(request("arm", token))
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.state()["reason"], "CONTROL_CONTENT_CHANGED")
        self.assertEqual(self.control()["action"], "check")

    async def test_interrupted_request_is_not_retried_or_assumed_completed(self):
        token = str(uuid.uuid4())
        with self.db() as conn:
            conn.execute(
                "INSERT INTO live_execution_controls VALUES(?,?,?, ?,NULL,NULL)",
                (token, "arm", "PROCESSING", NOW),
            )
        await self.apply(request("arm", token))
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.state()["reason"], "CONTROL_INTERRUPTED")
        self.assertEqual(self.control()["status"], "PROCESSING")

    async def test_pause_during_arm_network_check_wins(self):
        original = self.exchange.account

        async def pausing():
            with self.db() as conn:
                live.pause(conn, "MANUAL_PAUSE")
            return await original()

        self.exchange.account = pausing
        with self.assertRaisesRegex(live.ExecutionError, "PAUSED_DURING_ARM_CHECK"):
            await self.engine.arm()
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.state()["reason"], "MANUAL_PAUSE")

    async def test_arm_requires_complete_signing_metadata(self):
        self.exchange.metadata = AsyncMock(return_value={"contractList": []})
        await self.apply(request("arm"))
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.control()["status"], "REFUSED")
        self.assertEqual(self.exchange.creates, [])

    async def test_stale_preflight_account_blocks_arm(self):
        original = self.exchange.account

        async def stale():
            a = await original()
            a["observed_ms"] -= 5000
            return a

        self.exchange.account = stale
        await self.apply(request("arm"))
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.control()["status"], "REFUSED")

    async def test_external_orders_or_position_block_arm_without_cancelling(self):
        self.exchange.external_orders = [{"clientOrderId": "external"}]
        await self.apply(request("arm"))
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.cancels, [])

    async def test_sdk_exception_never_enters_report_or_control_ledger(self):
        self.exchange.account = AsyncMock(
            side_effect=RuntimeError("API-SECRET-EXPOSED")
        )
        await self.apply(request("check"))
        self.assertEqual(self.control()["status"], "REFUSED")
        with self.db() as conn:
            self.assertNotIn(
                "API-SECRET", json.dumps(live.report(conn, self.config, now_ms=NOW))
            )

    async def test_control_id_not_exposed_by_public_report(self):
        command = request("check")
        await self.apply(command)
        with self.db() as conn:
            report = live.report(conn, self.config, now_ms=NOW)
        self.assertNotIn(command.split(":")[1], json.dumps(report))
        self.assertTrue(report["preflight_is_read_only"])
        self.assertFalse(report["preflight_validates_signer_authorization"])

    async def test_migration_keeps_old_state_ledger_and_control_history(self):
        command = request("check")
        await self.apply(command)
        with self.db() as conn:
            before = live.state(conn)
            live.initialize(conn, now_ms=NOW + 900000)
            self.assertEqual(live.state(conn), before)
        await self.apply(command)
        self.assertEqual(self.control()["status"], "DONE")

    def test_cli_generates_request_without_db_or_client(self):
        from analysis_terminal import live_execution_control as cli

        output = io.StringIO()
        with patch("sys.argv", ["control", "request", "--operation", "arm"]), patch(
            "sys.stdout", output
        ), patch.object(
            cli, "run", side_effect=AssertionError("unexpected DB or network")
        ):
            cli.main()
        key, value = output.getvalue().strip().split("=")
        self.assertEqual(key, "EDGEX_EXEC_CONTROL_REQUEST")
        self.assertEqual(live.parse_control(value)[0], "arm")


if __name__ == "__main__":
    unittest.main()
