"""Explicit balance-linked execution policy; simulated exchange only."""

from dataclasses import replace
from decimal import Decimal
import json
import unittest

from analysis_terminal import live_execution as live
from analysis_terminal import test_live_execution as fixtures

NOW = fixtures.NOW
candidate = fixtures.candidate
configuration = fixtures.configuration


def account_policy(mode="LIVE"):
    return replace(
        configuration(mode),
        risk_pct="3",
        max_risk_usdc="ACCOUNT_RISK_PCT",
        max_notional_usdc="ACCOUNT_EQUITY",
        daily_loss_usdc="DISABLED",
    )


class PolicyTests(unittest.TestCase):
    def test_explicit_three_percent_account_policy(self):
        config = account_policy()
        self.assertEqual(config.errors(), [])
        self.assertEqual(config.risk_budget(10000), Decimal(300))
        self.assertEqual(config.risk_budget(5000), Decimal(150))
        self.assertEqual(config.notional_limit(10000, 6000), Decimal(6000))
        self.assertEqual(config.notional_limit(5000, 9000), Decimal(5000))
        self.assertIsNone(config.daily_loss_limit(None))

    def test_numeric_caps_keep_legacy_behavior(self):
        config = configuration()
        self.assertEqual(config.errors(), [])
        self.assertEqual(config.risk_budget(10000), Decimal(10))
        self.assertEqual(config.notional_limit(10000, 9000), Decimal(1000))
        self.assertEqual(config.daily_loss_limit(10000), Decimal(30))
        config.daily_loss_usdc = "500"
        self.assertEqual(config.daily_loss_limit(10000), Decimal(300))

    def test_missing_and_invalid_modes_never_disable_limits(self):
        cases = [
            ("risk_pct", value) for value in ["", "0", "-1", "3.00001", "NaN", "Infinity"]
        ] + [
            (name, value)
            for name in ["max_risk_usdc", "max_notional_usdc", "daily_loss_usdc"]
            for value in ["", "0", "-1", "NaN", "Infinity", "disabled", "NONE"]
        ] + [
            ("max_risk_usdc", "ACCOUNT_EQUITY"),
            ("max_notional_usdc", "ACCOUNT_RISK_PCT"),
            ("daily_loss_usdc", "ACCOUNT_RISK_PCT"),
        ]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                config = account_policy()
                setattr(config, name, value)
                self.assertIn("EXPLICIT_RISK_LIMITS_REQUIRED", config.errors())

    def test_policy_change_has_different_durable_identity(self):
        config = account_policy()
        for name, value in [
            ("risk_pct", "1"),
            ("max_risk_usdc", "100"),
            ("max_notional_usdc", "1000"),
            ("daily_loss_usdc", "30"),
        ]:
            with self.subTest(name=name):
                self.assertNotEqual(config.fingerprint(), replace(config, **{name: value}).fingerprint())


class AccountSizingTests(unittest.TestCase):
    # Use the original closed-candle sizing fixture without inheriting its tests.
    prepare = fixtures.SizingTests.prepare

    def test_three_percent_includes_both_fees_and_stop_slippage(self):
        for side in ["LONG", "SHORT"]:
            with self.subTest(side=side):
                row = self.prepare(side, config=account_policy(), account_changes={"available": Decimal(10000)})
                limit = Decimal(row["limit_price"])
                stop = Decimal(row["stop_price"])
                stop_exit = stop * (Decimal(".9998") if side == "LONG" else Decimal("1.0002"))
                per_unit = abs(limit - stop_exit) + (limit + stop_exit) * Decimal(".0005")
                expected = (Decimal(300) / per_unit / Decimal(".001")).to_integral_value(rounding="ROUND_FLOOR") * Decimal(".001")
                self.assertEqual(Decimal(row["quantity"]), expected)
                self.assertEqual(Decimal(row["risk_reserved_usdc"]), expected * per_unit)
                self.assertLessEqual(expected * per_unit, Decimal(300))
                self.assertEqual(row["structural_stop"], str(90 if side == "LONG" else 110))
                self.assertEqual(row["created_ms"], NOW - 30000 + 1)

    def test_remaining_equity_reduces_next_order(self):
        high = self.prepare(config=account_policy(), account_changes={"available": Decimal(10000)})
        low = self.prepare(config=account_policy(), account_changes={"equity": Decimal(5000), "available": Decimal(5000)})
        self.assertLess(Decimal(low["quantity"]), Decimal(high["quantity"]))
        self.assertLessEqual(Decimal(low["risk_reserved_usdc"]), Decimal(150))

    def test_available_collateral_and_one_times_equity_still_cap_notional(self):
        for equity, available in [(10000, 100), (5000, 10000)]:
            with self.subTest(equity=equity, available=available):
                row = self.prepare(config=account_policy(),
                    row_changes={"stop_loss": 99, "take_profit": 103},
                    account_changes={"equity": Decimal(equity), "available": Decimal(available)})
                total = Decimal(row["notional_reserved_usdc"]) * Decimal("1.0005")
                self.assertLessEqual(total, min(equity, available))
                self.assertLessEqual(Decimal(row["risk_reserved_usdc"]), Decimal(equity) * Decimal(".03"))

    def test_no_available_capital_does_not_create_quantity(self):
        with self.assertRaisesRegex(live.ExecutionError, "SIZE_BELOW_MINIMUM"):
            self.prepare(config=account_policy(), account_changes={"available": Decimal(0)})


class AccountEngineTests(unittest.IsolatedAsyncioTestCase):
    # Reuse setup helpers without collecting the original 35 async tests twice.
    setUp = fixtures.ExecutionTests.setUp
    armed = fixtures.ExecutionTests.armed
    enter = fixtures.ExecutionTests.enter
    records = fixtures.ExecutionTests.records
    state = fixtures.ExecutionTests.state

    def configure(self):
        self.config = account_policy()
        self.engine.config = self.config

    async def test_disabled_daily_limit_does_not_stop_after_large_equity_loss(self):
        self.configure()
        await self.armed()
        await self.engine.cycle()
        self.exchange.equity = Decimal(8000)
        self.exchange.available = Decimal(8000)
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.assertTrue(self.state()["armed"])
        self.assertEqual(self.records()[0]["status"], "PROTECTED")
        self.assertLessEqual(Decimal(self.records()[0]["risk_reserved_usdc"]), Decimal(240))

    async def test_disabled_daily_limit_can_rearm_without_resetting_baseline(self):
        self.configure()
        await self.armed()
        await self.engine.cycle()
        with self.db() as conn:
            live.pause(conn, "MANUAL_PAUSE")
        self.exchange.equity = Decimal(7000)
        await self.engine.arm()
        self.assertTrue(self.state()["armed"])
        self.assertEqual(self.state()["day_start_equity"], "10000")
        self.assertEqual(self.exchange.creates, [])

    async def test_unarmed_policy_does_not_start_automatically(self):
        self.configure()
        self.now = NOW
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.creates, [])

    async def test_read_only_stays_read_only_with_complete_three_percent_policy(self):
        self.configure()
        self.config.mode = "READ_ONLY"
        self.now = NOW
        await self.engine.preflight()
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        with self.db() as conn:
            result = live.report(conn, self.config, now_ms=self.now)
        self.assertEqual(result["live_configuration_errors"], [])
        self.assertFalse(result["real_orders_enabled"])
        self.assertFalse(result["preflight_validates_signer_authorization"])
        self.assertEqual(self.exchange.creates, [])
        self.assertEqual(self.exchange.cancels, [])

    async def test_disabled_daily_stop_keeps_reconciliation_error_stop(self):
        self.configure()
        await self.enter()
        self.exchange.account_error = True
        with self.assertRaises(live.ExecutionError):
            await self.engine.cycle()
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp", "close"])

    async def test_disabled_daily_stop_keeps_one_position_and_protective_orders(self):
        self.configure()
        await self.enter()
        self.exchange.equity = Decimal(8000)
        await self.engine.cycle([candidate(breakout_time_ms=NOW - 28800000)], snapshot_ms=self.now)
        self.assertTrue(self.state()["armed"])
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])

    async def test_change_to_balance_policy_requires_explicit_rearm(self):
        await self.armed()
        self.configure()
        with self.assertRaisesRegex(live.ExecutionError, "ACCOUNT_OR_POLICY_CHANGED"):
            await self.engine.cycle()
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.creates, [])

    async def test_read_only_report_validates_live_keys_without_exposing_them(self):
        self.configure()
        self.config.mode = "READ_ONLY"
        for key, expected in [("", "SIGNER_KEY_REQUIRED"), ("secret-invalid", "INVALID_SIGNER_KEY")]:
            with self.subTest(key=bool(key)):
                self.config.signer_key = key
                with self.db() as conn:
                    result = live.report(conn, self.config, now_ms=self.now)
                self.assertIn(expected, result["live_configuration_errors"])
                self.assertNotIn("secret-invalid", json.dumps(result))
                self.assertEqual(result["configuration_errors"], [])
                self.assertFalse(result["real_orders_enabled"])


if __name__ == "__main__":
    unittest.main()
