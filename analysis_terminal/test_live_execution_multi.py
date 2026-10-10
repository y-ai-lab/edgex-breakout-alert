"""Multiple isolated contracts with a simulated, collateral-aware exchange."""

import copy
from decimal import Decimal
from unittest.mock import AsyncMock

from analysis_terminal import test_live_execution as fixtures

live = fixtures.live


class MarginExchange(fixtures.Exchange):
    async def metadata(self):
        result = await super().metadata()
        result["contractList"] = [dict(fixtures.META, contractId=str(10000001 + n),
                                       contractName=f"TEST{n}USDC") for n in range(12)]
        return result

    async def create(self, intent, kind, **kwargs):
        try:
            return await super().create(intent, kind, **kwargs)
        finally:
            # Simulate 10% exchange margin, including side effects of lost ACKs.
            order = self.ledger.get(intent[kind + "_client_id"])
            if kind == "entry" and order is not None:
                self.available -= live.decimal(order["cumFillValue"]) / 10
                self.available -= live.decimal(order["cumFillFee"])


def candidate(n=0, side="LONG", **kwargs):
    return fixtures.candidate(side, ticker=f"TEST{n}USDC", contract_id=str(10000001+n), **kwargs)


class MultipleContractsTests(fixtures.unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ExecutionTests.setUp(self)
        self.config.account_policy = "COEXISTING_CONTRACTS"
        self.config.risk_pct = "3"
        self.config.max_risk_usdc = "ACCOUNT_RISK_PCT"
        self.config.max_notional_usdc = "ACCOUNT_EQUITY"
        self.config.daily_loss_usdc = "DISABLED"
        self.exchange = MarginExchange(lambda: self.now)
        self.engine = live.Engine(self.db, self.config, self.exchange, clock=lambda: self.now)

    records = fixtures.ExecutionTests.records
    state = fixtures.ExecutionTests.state
    armed = fixtures.ExecutionTests.armed

    async def enter(self, n=0, side="LONG"):
        await self.engine.cycle([candidate(n, side)], snapshot_ms=self.now)

    async def test_five_protected_contracts_without_count_ceiling(self):
        await self.armed()
        fingerprint = self.config.fingerprint()
        for n in range(5):
            await self.enter(n, "SHORT" if n % 2 else "LONG")
            self.assertEqual(len(self.records()), n+1)
            self.assertTrue(all(r["status"] == "PROTECTED" for r in self.records()))
            for r in self.records():
                self.assertLessEqual(live.decimal(r["risk_reserved_usdc"]), Decimal(300))
                for kind in ("sl", "tp"):
                    self.assertTrue(self.exchange.ledger[r[kind+"_client_id"]]["reduceOnly"])
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"]*5)
        self.assertEqual(self.config.fingerprint(), fingerprint)
        self.assertTrue(self.state()["armed"])

    async def test_one_entry_per_cycle_not_one_position(self):
        await self.armed()
        rows = [candidate(0, score=100), candidate(1, score=90), candidate(2, score=80)]
        for count in (1, 2, 3):
            await self.engine.cycle(rows, snapshot_ms=self.now)
            self.assertEqual(len(self.records()), count)
        await self.engine.cycle(rows, snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates.count("entry"), 3)

    async def test_same_contract_new_setup_never_adds(self):
        await self.armed()
        await self.enter()
        await self.engine.cycle([candidate(0, breakout_time_ms=fixtures.NOW-28800000),
                                 candidate(1)], snapshot_ms=self.now)
        self.assertEqual([r["contract_id"] for r in self.records()], ["10000001", "10000002"])

    async def test_no_collateral_does_not_send_another_entry(self):
        await self.armed()
        await self.enter()
        self.exchange.available = Decimal(0)
        await self.enter(1)
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])
        self.assertEqual(self.records()[0]["status"], "PROTECTED")

    async def test_second_size_uses_remaining_collateral(self):
        await self.armed()
        await self.enter()
        self.exchange.available = Decimal(50)
        await self.enter(1)
        second = self.records()[1]
        self.assertEqual(second["status"], "PROTECTED")
        self.assertLessEqual(live.decimal(second["notional_reserved_usdc"])*Decimal("1.0005"), 50)

    async def test_collateral_changes_after_second_quote_prevent_send(self):
        await self.armed()
        await self.enter()
        quote = self.exchange.quote
        async def raced(*args):
            result = await quote(*args)
            self.exchange.available = Decimal(1)
            return result
        self.exchange.quote = raced
        with self.assertRaisesRegex(live.ExecutionError, "ACCOUNT_BUDGET_CHANGED"):
            await self.enter(1)
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])
        self.assertEqual(self.records()[1]["status"], "SKIPPED")

    async def test_lost_entry_ack_blocks_other_contract_and_never_resends(self):
        await self.armed()
        self.exchange.unknown_kind = "entry"
        with self.assertRaises(live.ExecutionError):
            await self.enter()
        self.exchange.unknown_kind = None
        await self.enter(1)
        await self.enter(1)
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])
        self.assertFalse(self.state()["armed"])

    async def test_old_protected_label_without_new_verification_blocks(self):
        await self.armed()
        await self.enter()
        self.engine.reconcile = AsyncMock(return_value=None)
        await self.enter(1)
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])

    async def test_pending_or_unknown_ledger_blocks_even_with_verified_protection(self):
        await self.armed()
        await self.enter()
        with self.db() as conn:
            live.save(conn, {"setup_id":"pending", "contract_id":"10000003",
                             "status":"SENDING_ENTRY"}, self.now)
        self.engine.reconcile = AsyncMock(return_value=True)
        await self.enter(1)
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])

    async def test_new_reservation_during_quote_blocks_concurrent_entry(self):
        await self.armed()
        await self.enter()
        quote = self.exchange.quote
        async def raced(*args):
            result = await quote(*args)
            with self.db() as conn:
                live.save(conn, {"setup_id":"parallel", "contract_id":"10000003",
                                 "status":"PREPARED"}, self.now)
            return result
        self.exchange.quote = raced
        await self.enter(1)
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])
        self.assertEqual(len(self.records()), 2)

    async def test_broken_protection_blocks_addition_and_preserves_other_contract(self):
        await self.armed()
        await self.enter()
        await self.enter(1)
        second = copy.deepcopy(self.records()[1])
        first = self.records()[0]
        self.exchange.ledger[first["sl_client_id"]]["status"] = "CANCELED"
        await self.enter(2)
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.creates.count("entry"), 2)
        self.assertEqual(self.exchange.ledger[second["sl_client_id"]]["status"], "UNTRIGGERED")
        self.assertEqual(self.exchange.positions[second["contract_id"]], live.decimal(second["filled_size"]))

    async def test_paused_restart_tracks_all_exits_without_new_entry(self):
        await self.armed()
        await self.enter()
        await self.enter(1, "SHORT")
        with self.db() as conn:
            live.pause(conn, "USER_PAUSE")
        self.exchange.fill_exit(self.records()[0], "tp")
        self.engine = live.Engine(self.db, self.config, self.exchange, clock=lambda: self.now)
        await self.enter(2)
        self.assertEqual([r["status"] for r in self.records()], ["CLOSED", "PROTECTED"])
        self.assertEqual(self.exchange.creates.count("entry"), 2)
        self.assertFalse(self.state()["armed"])

    async def test_foreign_contract_position_and_orders_unchanged(self):
        self.exchange.positions["10000012"] = Decimal(-7)
        manual = {"contractId":"10000012", "clientOrderId":"manual"}
        self.exchange.external_orders = [manual.copy()]
        await self.armed()
        await self.enter()
        await self.enter(1)
        self.assertEqual(self.exchange.positions["10000012"], Decimal(-7))
        self.assertEqual(self.exchange.external_orders, [manual])
        self.assertEqual(self.exchange.cancels, [])

    async def test_dedicated_policy_retains_legacy_single_position(self):
        self.config.account_policy = "DEDICATED"
        await self.armed()
        await self.enter()
        await self.enter(1)
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])
        self.assertEqual(len(self.records()), 1)
