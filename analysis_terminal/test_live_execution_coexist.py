"""Contract isolation with simulated manual positions/orders; never real orders."""

import copy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from analysis_terminal import test_live_execution as fixtures

live = fixtures.live
ExecutionError = fixtures.ExecutionError
OTHER = "10000002"


def manual(cid=OTHER):
    return dict(accountId="123", contractId=cid, clientOrderId="manual-order")


class CoexistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ExecutionTests.setUp(self)
        self.config.account_policy = "COEXISTING_CONTRACTS"
        self.config.daily_loss_usdc = "DISABLED"

    records = fixtures.ExecutionTests.records
    state = fixtures.ExecutionTests.state
    armed = fixtures.ExecutionTests.armed
    enter = fixtures.ExecutionTests.enter

    async def test_other_contract_position_and_orders_allow_arm_and_protection(self):
        self.exchange.positions[OTHER] = Decimal("-9")
        self.exchange.external_orders = [manual()]
        old = copy.deepcopy(self.exchange.external_orders)
        r = await self.enter()
        self.assertEqual(r["status"], "PROTECTED")
        self.assertTrue(r["isolated_contract"])
        self.assertEqual(self.exchange.positions[OTHER], Decimal("-9"))
        self.assertEqual(self.exchange.external_orders, old)
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])
        self.assertEqual(self.exchange.cancels, [])
        self.assertTrue(self.state()["armed"])

    async def test_same_contract_long_and_short_positions_block_without_mutation(self):
        for size in (Decimal(4), Decimal(-4)):
            with self.subTest(size=size):
                self.exchange.positions["10000001"] = size
                await self.armed()
                await self.engine.cycle([fixtures.candidate()], snapshot_ms=self.now)
                self.assertEqual(self.exchange.creates, [])
                self.assertEqual(self.exchange.cancels, [])
                self.assertEqual(self.exchange.positions["10000001"], size)
                self.assertEqual(self.records(), [])
                self.assertTrue(self.state()["armed"])

    async def test_same_contract_order_even_reduce_only_blocks(self):
        for reduce_only in (True, False):
            self.exchange.external_orders = [dict(manual("10000001"), reduceOnly=reduce_only)]
            await self.armed()
            await self.engine.cycle([fixtures.candidate()], snapshot_ms=self.now)
            self.assertEqual(self.exchange.creates, [])
            self.assertEqual(self.exchange.cancels, [])

    async def test_blocked_top_candidate_does_not_block_other_contract(self):
        self.exchange.positions[OTHER] = Decimal(2)
        await self.armed()
        blocked = fixtures.candidate(ticker="ETHUSDC", contract_id=OTHER, score=100)
        await self.engine.cycle([blocked, fixtures.candidate()], snapshot_ms=self.now)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["contract_id"], "10000001")

    async def test_zero_size_position_record_is_not_occupied(self):
        self.exchange.positions["10000001"] = Decimal(0)
        self.assertEqual((await self.enter())["status"], "PROTECTED")

    async def test_no_available_collateral_refuses_arm(self):
        self.exchange.available = Decimal(0)
        await self.engine.preflight()
        self.assertEqual(self.state()["last_preflight_arm_blockers"], ["NO_AVAILABLE_COLLATERAL"])
        with self.assertRaisesRegex(ExecutionError, "ACCOUNT_PRECONDITIONS_REQUIRED"):
            await self.engine.arm()
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.creates, [])

    async def test_unknown_external_contract_refuses_preflight(self):
        for cid in (None, "", "0", "-1", "nan", "1.0"):
            self.exchange.external_orders = [manual(cid)]
            with self.assertRaisesRegex(ExecutionError, "ACTIVE_ORDER_CONTRACT_UNKNOWN"):
                await self.engine.preflight()
            self.assertIsNone(self.state()["last_preflight_arm_blockers"])
        self.assertEqual(self.exchange.cancels, [])

    async def test_unresolved_ledger_still_refuses_arm(self):
        r = await self.enter()
        with self.assertRaisesRegex(ExecutionError, "ACCOUNT_PRECONDITIONS_REQUIRED"):
            await self.engine.arm()
        self.assertEqual(self.records()[0], r)

    async def test_manual_position_appears_during_quote_no_entry_sent(self):
        await self.armed()
        original = self.exchange.quote

        async def raced(*args):
            result = await original(*args)
            self.exchange.positions["10000001"] = Decimal(7)
            return result

        self.exchange.quote = raced
        with self.assertRaisesRegex(ExecutionError, "CONTRACT_ALREADY_OCCUPIED"):
            await self.engine.cycle([fixtures.candidate()], snapshot_ms=self.now)
        r = self.records()[0]
        self.assertEqual(r["status"], "SKIPPED")
        self.assertFalse(r.get("entry_attempted", False))
        self.assertEqual(self.exchange.creates, [])
        self.assertEqual(self.exchange.cancels, [])

    async def test_manual_order_appears_during_quote_no_entry_sent(self):
        await self.armed()
        original = self.exchange.quote

        async def raced(*args):
            result = await original(*args)
            self.exchange.external_orders = [manual("10000001")]
            return result

        self.exchange.quote = raced
        with self.assertRaisesRegex(ExecutionError, "CONTRACT_ALREADY_OCCUPIED"):
            await self.engine.cycle([fixtures.candidate()], snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates, [])
        self.assertEqual(self.records()[0]["status"], "SKIPPED")

    async def test_collateral_and_equity_drop_during_quote_refuses_frozen_size(self):
        await self.armed()
        original = self.exchange.quote

        async def raced(*args):
            result = await original(*args)
            self.exchange.available = Decimal(1)
            self.exchange.equity = Decimal(10)
            return result

        self.exchange.quote = raced
        with self.assertRaisesRegex(ExecutionError, "ACCOUNT_BUDGET_CHANGED"):
            await self.engine.cycle([fixtures.candidate()], snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates, [])

    async def test_manual_size_change_never_blindly_emergency_closes(self):
        r = await self.enter()
        self.exchange.positions["10000001"] += Decimal(3)
        await self.engine.cycle()
        changed = self.records()[0]
        self.assertTrue(changed["ownership_quarantined"])
        self.assertEqual(changed["status"], "OWNERSHIP_CONFLICT")
        self.assertNotIn("close", self.exchange.creates)
        self.assertEqual(set(self.exchange.cancels), {r["sl_client_id"], r["tp_client_id"]})
        # Clearing the manual size later cannot retroactively prove ownership.
        self.exchange.positions["10000001"] = Decimal(r["filled_size"])
        await self.engine.cycle()
        self.assertEqual(len(self.exchange.cancels), 2)
        self.assertFalse(self.state()["armed"])

    async def test_same_contract_manual_order_quarantines_only_owned_exits(self):
        r = await self.enter()
        self.exchange.external_orders = [manual("10000001"), manual()]
        with self.assertRaisesRegex(ExecutionError, "CONTRACT_OWNERSHIP_CONFLICT"):
            await self.engine.cycle()
        self.assertTrue(self.records()[0]["ownership_quarantined"])
        self.assertNotIn("close", self.exchange.creates)
        self.assertEqual(set(self.exchange.cancels), {r["sl_client_id"], r["tp_client_id"]})
        self.assertEqual(len(self.exchange.external_orders), 2)

    async def test_manual_flatten_never_infers_owned_outcome(self):
        r = await self.enter()
        self.exchange.positions["10000001"] = Decimal(0)
        await self.engine.cycle()
        self.assertEqual(self.records()[0]["status"], "OWNERSHIP_CONFLICT")
        self.assertNotIn("trade_pnl_before_funding_usdc", self.records()[0])
        self.assertNotIn("close", self.exchange.creates)
        self.assertEqual(set(self.exchange.cancels), {r["sl_client_id"], r["tp_client_id"]})

    async def test_proven_own_tp_and_sl_resolve_with_external_positions_preserved(self):
        for side, kind in (("LONG", "tp"), ("SHORT", "sl")):
            with self.subTest(side=side, kind=kind):
                self.setUp()
                self.exchange.positions[OTHER] = Decimal(-5)
                self.exchange.external_orders = [manual()]
                r = await self.enter(fixtures.candidate(side))
                self.exchange.fill_exit(r, kind)
                await self.engine.cycle()
                self.assertEqual(self.records()[0]["status"], "CLOSED")
                self.assertEqual(self.exchange.positions[OTHER], Decimal(-5))
                self.assertEqual(self.exchange.external_orders, [manual()])
                self.assertEqual(self.exchange.cancels, [r[("sl" if kind == "tp" else "tp") + "_client_id"]])

    async def test_protection_rejected_closes_only_proven_own_size(self):
        self.exchange.positions[OTHER] = Decimal(50)
        self.exchange.external_orders = [manual()]
        self.exchange.fail_kind = "sl"
        r = await self.enter()
        self.assertEqual(self.exchange.creates, ["entry", "sl", "close"])
        self.assertEqual(self.exchange.ledger[r["close_client_id"]]["size"], r["filled_size"])
        self.assertEqual(self.exchange.positions[OTHER], Decimal(50))
        self.assertEqual(self.exchange.cancels, [])

    async def test_unreadable_account_after_fill_never_guesses_close_ownership(self):
        await self.armed()
        original = self.exchange.create

        async def raced(intent, kind, **kw):
            result = await original(intent, kind, **kw)
            if kind == "entry":
                self.exchange.account_error = True
            return result

        self.exchange.create = raced
        with self.assertRaisesRegex(ExecutionError, "INCOMPLETE_ACCOUNT"):
            await self.engine.cycle([fixtures.candidate()], snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates, ["entry"])
        self.assertEqual(self.records()[0]["status"], "OWNERSHIP_UNVERIFIED")
        self.assertNotIn(self.records()[0]["status"], live.TERMINAL)
        self.exchange.account_error = False
        await self.engine.cycle()
        self.assertEqual(self.records()[0]["status"], "PROTECTED")
        self.assertFalse(self.state()["armed"])

    async def test_manual_interference_between_fill_and_protection(self):
        await self.armed()
        original = self.exchange.create

        async def raced(intent, kind, **kw):
            result = await original(intent, kind, **kw)
            if kind == "entry":
                self.exchange.positions["10000001"] += Decimal(1)
            return result

        self.exchange.create = raced
        await self.engine.cycle([fixtures.candidate()], snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates, ["entry"])
        self.assertTrue(self.records()[0]["ownership_quarantined"])
        self.assertEqual(self.exchange.cancels, [])

    async def test_owned_partial_exit_closes_only_proven_residual(self):
        self.exchange.positions[OTHER] = Decimal(-4)
        r = await self.enter()
        self.exchange.fill_exit(r, "sl", fraction=Decimal(".5"))
        await self.engine.cycle()
        r = self.records()[0]
        self.assertEqual(Decimal(r["close_size"]), Decimal(r["filled_size"]) / 2)
        self.assertEqual(self.exchange.positions[OTHER], Decimal(-4))
        self.assertEqual(self.exchange.creates.count("close"), 1)

    async def test_restart_preserves_own_protection_and_unrelated_inventory(self):
        self.exchange.positions[OTHER] = Decimal(6)
        r = await self.enter()
        restarted = live.Engine(self.db, self.config, self.exchange, clock=lambda: self.now)
        await restarted.cycle([fixtures.candidate()], snapshot_ms=self.now)
        self.assertEqual(self.records()[0], r)
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])
        self.assertEqual(self.exchange.positions[OTHER], Decimal(6))

    async def test_one_bot_position_still_prevents_second_entry(self):
        await self.enter()
        next_row = fixtures.candidate(breakout_time_ms=fixtures.NOW)
        await self.engine.cycle([next_row], snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates.count("entry"), 1)

    async def test_read_only_coexistence_never_mutates(self):
        self.config.mode = "READ_ONLY"
        self.exchange.positions[OTHER] = Decimal(8)
        self.exchange.external_orders = [manual()]
        await self.engine.preflight()
        await self.engine.cycle([fixtures.candidate()], snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates, [])
        self.assertEqual(self.exchange.cancels, [])

    async def test_account_policy_change_requires_fresh_arm(self):
        self.config.account_policy = "DEDICATED"
        await self.armed()
        self.config.account_policy = "COEXISTING_CONTRACTS"
        with self.assertRaisesRegex(ExecutionError, "ACCOUNT_OR_POLICY_CHANGED"):
            await self.engine.cycle()
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.creates, [])


    async def test_explicit_position_wide_response_rejected_missing_documented_flag_allowed(self):
        r = await self.enter()
        o = await self.exchange.order(r["sl_client_id"])
        live.verify_order(o, r, "sl")
        o["isPositionTpsl"] = False
        live.verify_order(o, r, "sl")
        o["isPositionTpsl"] = True
        with self.assertRaisesRegex(ExecutionError, "POSITION_WIDE_PROTECTION_FORBIDDEN"):
            live.verify_order(o, r, "sl")

    async def test_lost_entry_ack_restart_does_not_resend_or_touch_manual_orders(self):
        self.exchange.positions[OTHER] = Decimal(10)
        self.exchange.external_orders = [manual()]
        self.exchange.unknown_kind = "entry"
        await self.armed()
        with self.assertRaisesRegex(ExecutionError, "TRANSPORT_OR_SIGNING_UNKNOWN"):
            await self.engine.cycle([fixtures.candidate()], snapshot_ms=self.now)
        self.assertTrue(self.records()[0]["entry_attempted"])
        restarted = live.Engine(self.db, self.config, self.exchange, clock=lambda: self.now)
        await restarted.cycle()
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])
        self.assertEqual(self.records()[0]["status"], "PROTECTED")
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.external_orders, [manual()])
        self.assertEqual(self.exchange.positions[OTHER], Decimal(10))

    async def test_lost_sl_ack_uses_one_owned_close_and_no_foreign_cancel(self):
        self.exchange.positions[OTHER] = Decimal(4)
        self.exchange.external_orders = [manual()]
        self.exchange.unknown_kind = "sl"
        r = await self.enter()
        self.assertEqual(self.exchange.creates, ["entry", "sl", "close"])
        await self.engine.cycle()
        self.assertEqual(self.exchange.creates.count("close"), 1)
        self.assertEqual(self.exchange.creates.count("sl"), 1)
        self.assertFalse(self.records()[0].get("ownership_quarantined", False))
        self.assertEqual(self.exchange.positions[OTHER], Decimal(4))
        self.assertEqual(self.exchange.cancels, [])
        self.assertEqual(self.exchange.ledger[r["close_client_id"]]["size"], r["filled_size"])

    async def test_guard_account_older_than_five_seconds_cannot_send_entry(self):
        await self.armed()
        original = self.exchange.account
        calls = 0

        async def stale():
            nonlocal calls
            calls += 1
            result = await original()
            if calls >= 2:
                result["observed_ms"] -= 5000
            return result

        self.exchange.account = stale
        with self.assertRaisesRegex(ExecutionError, "STALE_ACCOUNT_OR_SCAN"):
            await self.engine.cycle([fixtures.candidate()], snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates, [])
        self.assertEqual(self.records()[0]["status"], "SKIPPED")

    async def test_explicit_position_wide_protection_causes_owned_close(self):
        original = self.exchange.order

        async def malformed(client_id):
            o = await original(client_id)
            if o and o["type"] == "STOP_MARKET":
                o["isPositionTpsl"] = True
            return o

        self.exchange.order = malformed
        await self.enter()
        self.assertEqual(self.exchange.creates, ["entry", "sl", "close"])

    async def test_public_report_exposes_policy_without_private_inventory(self):
        self.exchange.positions[OTHER] = Decimal(99)
        self.exchange.external_orders = [manual()]
        await self.armed()
        with self.db() as conn:
            r = live.report(conn, self.config, now_ms=self.now)
        self.assertEqual(r["account_policy"], "COEXISTING_CONTRACTS")
        self.assertFalse(r["same_contract_coexistence"])
        self.assertEqual(r["preflight_arm_blockers"], [])
        self.assertFalse(r["account_details_exposed"])
        for key in ("positions", "equity", "available", "account_id", "api_key", "signer_key"):
            self.assertNotIn(key, r)


class CoexistPolicyTests(unittest.TestCase):
    def test_default_fingerprint_unchanged_and_opt_in_bound(self):
        c = fixtures.configuration()
        old = {k: getattr(c, k) for k in ("account_id", "risk_pct", "max_risk_usdc", "max_notional_usdc", "daily_loss_usdc", "slippage_bps", "fee_bps")}
        fingerprint = hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest()
        self.assertEqual(c.fingerprint(), fingerprint)
        self.assertNotEqual(replace(c, account_policy="COEXISTING_CONTRACTS").fingerprint(), fingerprint)

    def test_explicit_policy_env_validation(self):
        with patch.dict(os.environ, {"EDGEX_EXEC_ACCOUNT_POLICY": "COEXISTING_CONTRACTS"}):
            self.assertEqual(live.Config.from_env().account_policy, "COEXISTING_CONTRACTS")
        for bad in ("", "COEXIST", "coexisting_contracts"):
            self.assertIn("INVALID_ACCOUNT_POLICY", replace(fixtures.configuration(), account_policy=bad).errors())

    def test_sizing_keeps_three_percent_and_available_collateral_constraints(self):
        c = replace(fixtures.configuration(), account_policy="COEXISTING_CONTRACTS", risk_pct="3", max_risk_usdc="ACCOUNT_RISK_PCT", max_notional_usdc="ACCOUNT_EQUITY")
        a = dict(equity=Decimal(100), available=Decimal(20), positions={OTHER: Decimal(-30)}, observed_ms=fixtures.NOW)
        q = dict(ask1Price=100, bid1Price=100, maxBuySize=100, maxSellSize=100)
        r = live.prepare(fixtures.candidate(), fixtures.META, a, q, c, now_ms=fixtures.NOW, armed_ms=fixtures.NOW-60000, snapshot_ms=fixtures.NOW)
        self.assertLessEqual(Decimal(r["risk_reserved_usdc"]), Decimal(3))
        self.assertLessEqual(Decimal(r["notional_reserved_usdc"]) * Decimal("1.0005"), Decimal(20))
        a["positions"]["10000001"] = Decimal(1)
        with self.assertRaisesRegex(ExecutionError, "CONTRACT_ALREADY_OCCUPIED"):
            live.prepare(fixtures.candidate(), fixtures.META, a, q, c, now_ms=fixtures.NOW, armed_ms=fixtures.NOW-60000, snapshot_ms=fixtures.NOW)


class CoexistAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_protection_is_fixed_size_reduce_only_not_position_wide(self):
        c = replace(fixtures.configuration(), account_policy="COEXISTING_CONTRACTS")
        client = AsyncMock()
        client.create_order.return_value = {"code": "SUCCESS", "data": {"orderId": "42"}}
        adapter = fixtures.EdgeXOrders(c, client=client)
        r = fixtures.SizingTests().prepare()
        r["isolated_contract"] = True
        for kind in ("sl", "tp"):
            await adapter.create(r, kind, quantity=".01")
            p = client.create_order.call_args.args[0]
            self.assertFalse(p.is_position_tpsl)
            self.assertTrue(p.reduce_only)
            self.assertEqual(p.size, ".01")

    async def test_active_order_missing_contract_on_later_page_fails_closed(self):
        c = replace(fixtures.configuration(), account_policy="COEXISTING_CONTRACTS")
        client = AsyncMock()
        client.get_active_orders.side_effect = [
            {"code": "SUCCESS", "data": {"dataList": [manual()], "nextPageOffsetData": "next"}},
            {"code": "SUCCESS", "data": {"dataList": [manual(None)], "nextPageOffsetData": ""}},
        ]
        adapter = fixtures.EdgeXOrders(c, client=client)
        with self.assertRaisesRegex(ExecutionError, "ACTIVE_ORDER_CONTRACT_UNKNOWN"):
            await adapter.active_orders()
        client.create_order.assert_not_called()
        client.cancel_order.assert_not_called()



if __name__ == "__main__":
    unittest.main()
