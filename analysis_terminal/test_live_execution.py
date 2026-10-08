"""Order safety tests: a simulated exchange, never real orders or credentials."""

import copy
from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

from analysis_terminal import live_execution as live
from analysis_terminal import edgex_orders as order_api
from analysis_terminal.setups import setup_identity

# The legacy Push fixture restores sys.modules after importing server. Keep
# these references with the engine's imports instead of creating second classes.
EdgeXOrders = order_api.EdgeXOrders
ExecutionError = live.ExecutionError
decimal = live.decimal

NOW = 1791417600000 + 30000
META = dict(
    contractId="10000001",
    contractName="BTCUSDC",
    quoteCoinId="1000",
    tickSize=".01",
    stepSize=".001",
    minOrderSize=".001",
    maxOrderSize="100",
    defaultMakerFeeRate=".0002",
    defaultTakerFeeRate=".0005",
    enableTrade=True,
    enableOpenPosition=True,
)


def configuration(mode="LIVE"):
    return live.Config(
        mode=mode,
        account_id="123",
        api_key="test-key",
        api_secret="test-secret",
        passphrase="test-passphrase",
        signer_key="0x" + "11" * 32,
        risk_pct=".1",
        max_risk_usdc="10",
        max_notional_usdc="1000",
        daily_loss_usdc="30",
    )


def candidate(side="LONG", **changes):
    row = dict(
        ticker="BTCUSDC",
        contract_id="10000001",
        direction=side,
        stage="READY",
        breakout_time_ms=NOW - 14400000,
        breakout_level=99,
        latest_15m_time_ms=NOW - 30000 - live.STEP,
        confirmed=True,
        retest_touched=True,
        entry_reference=100,
        stop_loss=90 if side == "LONG" else 110,
        take_profit=125 if side == "LONG" else 75,
        score=90,
    )
    row.update(changes)
    row["setup_id"] = setup_identity(row)
    return row


class Exchange:
    """Records requests, applies fills, and injects realistic lost acknowledgments."""

    def __init__(self, clock):
        self.clock = clock
        self.positions = {}
        self.ledger = {}
        self.creates = []
        self.cancels = []
        self.fail_kind = None
        self.unknown_kind = None
        self.entry_status = "FILLED"
        self.fill_fraction = Decimal(1)
        self.fill_price = Decimal(100)
        self.equity = Decimal(10000)
        self.available = Decimal(10000)
        self.external_orders = []
        self.account_error = False
        self.bad_protection = False

    async def account(self):
        if self.account_error:
            raise ExecutionError("INCOMPLETE_ACCOUNT")
        return dict(
            equity=self.equity,
            available=self.available,
            positions=self.positions.copy(),
            observed_ms=self.clock(),
        )

    async def active_orders(self):
        return self.external_orders + [
            o for o in self.ledger.values() if o["status"] not in {"FILLED", "CANCELED"}
        ]

    async def metadata(self):
        return {
            "contractList": [META.copy()],
            "global": {"nativeChainId": "3343", "contractAddress": "0x" + "22" * 20},
        }

    async def quote(self, contract_id, price):
        return dict(
            ask1Price=Decimal(100),
            bid1Price=Decimal(100),
            maxBuySize=Decimal(100),
            maxSellSize=Decimal(100),
        )

    async def order(self, client_id):
        return copy.deepcopy(self.ledger.get(client_id))

    async def create(self, intent, kind, *, quantity=None):
        self.creates.append(kind)
        if kind == self.fail_kind:
            raise ExecutionError("EXCHANGE_REJECTED")
        size = decimal(quantity or intent["quantity"])
        long = intent["side"] == "LONG"
        entry = kind == "entry"
        order = dict(
            id=str(len(self.creates)),
            accountId="123",
            contractId=intent["contract_id"],
            clientOrderId=intent[kind + "_client_id"],
            side="BUY" if long == entry else "SELL",
            reduceOnly=not entry,
            size=str(size),
            cumFillSize="0",
            cumFillValue="0",
            cumFillFee="0",
            type=(
                "LIMIT"
                if entry
                else (
                    "STOP_MARKET"
                    if kind == "sl"
                    else "TAKE_PROFIT_MARKET" if kind == "tp" else "MARKET"
                )
            ),
            status=self.entry_status if entry else "UNTRIGGERED",
            timeInForce="IMMEDIATE_OR_CANCEL",
            price=intent["limit_price"] if entry else "0",
            expireTime=str(
                intent["entry_deadline_ms"]
                if entry
                else intent["protection_deadline_ms"]
            ),
            triggerPrice=intent["stop_price" if kind == "sl" else "target_price"],
            triggerPriceType="LAST_PRICE",
        )
        if entry:
            fill = size * self.fill_fraction
            order.update(
                cumFillSize=str(fill),
                cumFillValue=str(fill * self.fill_price),
                cumFillFee=str(fill * self.fill_price * Decimal(".0005")),
            )
            self.positions[intent["contract_id"]] = fill if long else -fill
        if self.bad_protection and kind == "sl":
            order["reduceOnly"] = False
        self.ledger[order["clientOrderId"]] = order
        if kind == self.unknown_kind:
            raise ExecutionError("TRANSPORT_OR_SIGNING_UNKNOWN")
        return order["id"]

    async def cancel(self, client_id):
        self.cancels.append(client_id)
        self.ledger[client_id]["status"] = "CANCELED"

    def fill_exit(self, record, kind, fraction=Decimal(1), price=None):
        o = self.ledger[record[kind + "_client_id"]]
        filled = decimal(record["filled_size"]) * fraction
        px = decimal(price or record["stop_price" if kind == "sl" else "target_price"])
        o.update(
            status="FILLED" if filled == decimal(o["size"]) else "CANCELED",
            cumFillSize=str(filled),
            cumFillValue=str(px * filled),
            cumFillFee=str(px * filled * Decimal(".0005")),
        )
        self.positions[record["contract_id"]] -= (
            filled if record["side"] == "LONG" else -filled
        )


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ledger.db"
        self.now = NOW - 60000

        @contextmanager
        def db():
            conn = sqlite3.connect(self.path)
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

        self.db = db
        with db() as conn:
            live.initialize(conn, now_ms=self.now)
        self.config = configuration()
        self.exchange = Exchange(lambda: self.now)
        self.engine = live.Engine(
            db, self.config, self.exchange, clock=lambda: self.now
        )

    async def armed(self):
        await self.engine.arm()
        self.now = NOW

    async def enter(self, row=None):
        await self.armed()
        await self.engine.cycle([row or candidate()], snapshot_ms=self.now)
        return self.records()[0]

    def records(self):
        with self.db() as conn:
            return live.orders(conn)

    def state(self):
        with self.db() as conn:
            return live.state(conn)

    async def test_default_off_never_contacts_exchange(self):
        engine = live.Engine(
            self.db, live.Config(), AsyncMock(), clock=lambda: self.now
        )
        await engine.cycle([candidate()], snapshot_ms=self.now)
        engine.adapter.assert_not_called()
        self.assertEqual(engine.adapter.mock_calls, [])
        with self.db() as conn:
            self.assertEqual(
                live.report(conn, live.Config(), now_ms=self.now)["status"], "OFF"
            )

    async def test_read_only_never_mutates_exchange(self):
        self.config.mode = "READ_ONLY"
        self.now = NOW
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates, [])
        self.assertEqual(self.exchange.cancels, [])
        with self.db() as conn:
            self.assertEqual(
                live.report(conn, self.config, now_ms=self.now)["status"], "READ_ONLY"
            )

    async def test_real_entry_and_both_verified_protective_orders(self):
        r = await self.enter()
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])
        self.assertEqual(r["status"], "PROTECTED")
        self.assertEqual(r["created_ms"], NOW - 30000 + 1)
        self.assertEqual(self.state()["armed"], True)
        for kind in ("sl", "tp"):
            self.assertTrue(self.exchange.ledger[r[kind + "_client_id"]]["reduceOnly"])

    async def test_full_engine_through_adapter_matches_exchange_wire_schema(self):
        client = AsyncMock()

        def success(value):
            return {"code": "SUCCESS", "data": value}

        async def account():
            return success(
                {
                    "account": {"id": "123"},
                    "positionList": [
                        dict(accountId="123", contractId=cid, openSize=str(size))
                        for cid, size in self.exchange.positions.items()
                    ],
                    "collateralAssetModelList": [
                        dict(
                            coinId="1000",
                            accountId="123",
                            totalEquity="10000",
                            availableAmount="10000",
                        )
                    ],
                }
            )

        async def active(params):
            return success(
                {
                    "dataList": await self.exchange.active_orders(),
                    "nextPageOffsetData": "",
                }
            )

        async def query(**kwargs):
            self.assertEqual(kwargs["method"], "GET")
            self.assertEqual(
                kwargs["path"], "/api/v2/private/order/getOrderByClientOrderId"
            )
            order = await self.exchange.order(kwargs["params"]["clientOrderIdList"])
            return success([order] if order else [])

        async def create(params):
            kind = params.client_order_id.rsplit("-", 1)[1]
            r = self.records()[0]
            oid = await self.exchange.create(r, kind, quantity=params.size)
            return success({"orderId": oid})

        client.get_account_asset.side_effect = account
        client.get_active_orders.side_effect = active
        client.get_metadata.return_value = success(await self.exchange.metadata())
        client.get_max_order_size.return_value = success(
            dict(ask1Price="100", bid1Price="100", maxBuySize="100", maxSellSize="100")
        )
        client.async_client.make_authenticated_request.side_effect = query
        client.create_order.side_effect = create
        adapter = EdgeXOrders(self.config, client=client)
        self.engine = live.Engine(self.db, self.config, adapter, clock=lambda: self.now)
        with patch.object(order_api.time, "time", side_effect=lambda: self.now / 1000):
            r = await self.enter()
        self.assertEqual(r["status"], "PROTECTED")
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])

    async def test_short_uses_sell_entry_and_buy_exits(self):
        r = await self.enter(candidate("SHORT"))
        self.assertEqual(self.exchange.ledger[r["entry_client_id"]]["side"], "SELL")
        self.assertEqual(self.exchange.ledger[r["sl_client_id"]]["side"], "BUY")
        self.assertEqual(r["status"], "PROTECTED")

    async def test_partial_terminal_entry_only_protects_filled_size(self):
        self.exchange.entry_status = "CANCELED"
        self.exchange.fill_fraction = Decimal(".5")
        r = await self.enter()
        self.assertEqual(decimal(r["filled_size"]), decimal(r["quantity"]) / 2)
        self.assertEqual(
            decimal(self.exchange.ledger[r["sl_client_id"]]["size"]),
            decimal(r["filled_size"]),
        )

    async def test_zero_fill_does_not_create_exits(self):
        self.exchange.entry_status = "CANCELED"
        self.exchange.fill_fraction = Decimal(0)
        r = await self.enter()
        self.assertEqual(r["status"], "NO_FILL")
        self.assertEqual(self.exchange.creates, ["entry"])

    async def test_nonterminal_entry_is_not_assumed_filled(self):
        self.exchange.entry_status = "OPEN"
        r = await self.enter()
        self.assertNotIn("filled_size", r)
        self.assertEqual(self.exchange.creates, ["entry"])
        self.assertEqual(self.exchange.cancels, [r["entry_client_id"]])
        self.assertFalse(self.state()["armed"])
        self.now += 10001
        await self.engine.cycle()
        self.assertEqual(self.exchange.cancels, [r["entry_client_id"]])
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])

    async def test_lost_entry_ack_restarts_without_resending_entry(self):
        await self.armed()
        self.exchange.unknown_kind = "entry"
        with self.assertRaises(ExecutionError):
            await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.assertTrue(self.records()[0]["entry_attempted"])
        self.assertFalse(self.state()["armed"])
        restarted = live.Engine(
            self.db, self.config, self.exchange, clock=lambda: self.now
        )
        await restarted.cycle([candidate()], snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])
        self.assertEqual(self.records()[0]["status"], "PROTECTED")
        self.assertFalse(self.state()["armed"])

    async def test_entry_attempt_is_committed_before_network(self):
        original = self.exchange.create

        async def assert_commit(r, kind, **kwargs):
            self.assertTrue(self.records()[0][kind + "_attempted"])
            return await original(r, kind, **kwargs)

        self.exchange.create = assert_commit
        await self.enter()

    async def test_unknown_missing_entry_keeps_funds_and_never_retries(self):
        await self.armed()
        self.exchange.fail_kind = "entry"
        with self.assertRaises(ExecutionError):
            await self.engine.cycle([candidate()], snapshot_ms=self.now)
        for _ in range(3):
            await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates, ["entry"])
        self.assertNotIn(self.records()[0]["status"], live.TERMINAL)

    async def test_deduplicates_same_setup_across_new_confirmation_candles(self):
        r = await self.enter()
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.exchange.fill_exit(r, "tp")
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])

    async def test_durable_attempt_check_blocks_stale_in_memory_record(self):
        r = await self.enter()
        stale = copy.deepcopy(r)
        stale.pop("entry_attempted")
        with self.assertRaisesRegex(ExecutionError, "DUPLICATE_SEND_BLOCKED"):
            await self.engine.send(stale, "entry")
        self.assertEqual(self.exchange.creates.count("entry"), 1)

    async def test_sl_rejection_triggers_single_reduce_only_close_and_pause(self):
        self.exchange.fail_kind = "sl"
        r = await self.enter()
        self.assertEqual(self.exchange.creates, ["entry", "sl", "close"])
        self.assertFalse(self.state()["armed"])
        self.assertEqual(r["status"], "CLOSE_PENDING")
        close = self.exchange.ledger[r["close_client_id"]]
        self.assertTrue(close["reduceOnly"])
        self.assertNotEqual(self.exchange.positions[r["contract_id"]], 0)
        for _ in range(3):
            await self.engine.cycle()
        self.assertEqual(self.exchange.creates.count("close"), 1)

    async def test_tp_rejection_and_protection_mismatch_close_position(self):
        for failure in ("tp", "bad_sl"):
            with self.subTest(failure=failure):
                self.setUp()
                if failure == "tp":
                    self.exchange.fail_kind = "tp"
                else:
                    self.exchange.bad_protection = True
                r = await self.enter()
                self.assertTrue(r["close_attempted"])
                self.assertFalse(self.state()["armed"])

    async def test_lost_close_ack_never_resends_close(self):
        self.exchange.fail_kind = "sl"
        self.exchange.unknown_kind = "close"
        await self.armed()
        with self.assertRaises(ExecutionError):
            await self.engine.cycle([candidate()], snapshot_ms=self.now)
        await self.engine.cycle()
        self.assertEqual(self.exchange.creates.count("close"), 1)
        self.assertNotIn(self.records()[0]["status"], live.TERMINAL)

    async def test_closed_requires_fill_proof_cancels_only_remaining_exit(self):
        r = await self.enter()
        self.exchange.fill_exit(r, "tp")
        await self.engine.cycle()
        final = self.records()[0]
        self.assertEqual(final["status"], "CLOSED")
        self.assertEqual(self.exchange.cancels, [r["sl_client_id"]])
        self.assertGreater(decimal(final["trade_pnl_before_funding_usdc"]), 0)

    async def test_flat_without_our_exit_proof_does_not_release_intent(self):
        r = await self.enter()
        self.exchange.positions[r["contract_id"]] = Decimal(0)
        await self.engine.cycle()
        self.assertNotIn(self.records()[0]["status"], live.TERMINAL)
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.cancels, [])

    async def test_partial_exit_closes_only_remaining_position(self):
        r = await self.enter()
        self.exchange.fill_exit(r, "sl", fraction=Decimal(".5"))
        await self.engine.cycle()
        final = self.records()[0]
        self.assertEqual(decimal(final["close_size"]), decimal(r["filled_size"]) / 2)
        self.assertTrue(self.exchange.ledger[final["close_client_id"]]["reduceOnly"])

    async def test_bad_actual_fill_closes_without_widening_sl(self):
        self.exchange.fill_price = Decimal(105)
        r = await self.enter()
        self.assertEqual(self.exchange.creates, ["entry", "close"])
        self.assertEqual(r["structural_stop"], "90")
        self.assertFalse(self.state()["armed"])

    async def test_paused_still_manages_existing_protection(self):
        r = await self.enter()
        with self.db() as conn:
            live.pause(conn, "MANUAL_PAUSE")
        self.exchange.fill_exit(r, "sl")
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.assertEqual(self.records()[0]["status"], "CLOSED")
        self.assertFalse(self.state()["armed"])

    async def test_daily_equity_loss_stops_new_entries(self):
        await self.armed()
        await self.engine.cycle()
        self.exchange.equity -= Decimal(30)
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.state()["reason"], "DAILY_EQUITY_LOSS_LIMIT")
        self.assertEqual(self.exchange.creates, [])

    async def test_rearm_does_not_reset_daily_loss_baseline(self):
        await self.armed()
        await self.engine.cycle()
        self.exchange.equity -= Decimal(30)
        with self.assertRaisesRegex(ExecutionError, "DAILY_EQUITY_LOSS_LIMIT"):
            await self.engine.arm()
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.state()["day_start_equity"], "10000")
        self.assertEqual(self.exchange.creates, [])

    async def test_account_loss_after_known_fill_attempts_close(self):
        await self.enter()
        self.exchange.account_error = True
        with self.assertRaises(ExecutionError):
            await self.engine.cycle()
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp", "close"])
        self.assertFalse(self.state()["armed"])

    async def test_missing_protection_attempts_close_never_recreates_it(self):
        r = await self.enter()
        del self.exchange.ledger[r["sl_client_id"]]
        await self.engine.cycle()
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp", "close"])
        self.assertFalse(self.state()["armed"])

    async def test_expiring_protection_is_not_silently_extended(self):
        r = await self.enter()
        self.now = r["protection_deadline_ms"] - 60000
        await self.engine.cycle()
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp", "close"])
        self.assertEqual(
            self.records()[0]["protection_deadline_ms"], r["protection_deadline_ms"]
        )

    async def test_external_activity_blocks_arm_and_no_cancel_all(self):
        self.exchange.external_orders = [{"clientOrderId": "manual"}]
        with self.assertRaisesRegex(ExecutionError, "DEDICATED_FLAT_ACCOUNT_REQUIRED"):
            await self.engine.arm()
        self.assertEqual(self.exchange.cancels, [])

    async def test_external_activity_after_arm_stops_new_entries(self):
        await self.armed()
        self.exchange.positions["another-contract"] = Decimal(1)
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.assertFalse(self.state()["armed"])
        self.assertEqual(self.exchange.creates, [])

    async def test_account_or_policy_change_never_uses_old_intent(self):
        await self.enter()
        self.config.account_id = "456"
        with self.assertRaisesRegex(ExecutionError, "ACCOUNT_OR_POLICY_CHANGED"):
            await self.engine.cycle()
        self.assertEqual(self.exchange.creates, ["entry", "sl", "tp"])

    async def test_pre_arm_signals_and_near_shadow_are_never_ordered(self):
        await self.armed()
        old = candidate(latest_15m_time_ms=NOW - 30000 - 2 * live.STEP)
        near = candidate(stage="RR_WAIT", shadow_v2_ready=True, breakout_level=98)
        await self.engine.cycle([old, near], snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates, [])

    async def test_past_paper_setups_are_baselined_on_arm(self):
        await self.engine.arm(previous_ready=[candidate()])
        self.now = NOW
        await self.engine.cycle([candidate()], snapshot_ms=self.now)
        self.assertEqual(self.records()[0]["status"], "SKIPPED")
        self.assertEqual(self.exchange.creates, [])

    async def test_report_and_ledger_never_store_secrets_or_account_id(self):
        await self.enter()
        with self.db() as conn:
            output = json.dumps(live.report(conn, self.config, now_ms=self.now))
            payload = "".join(
                x[0] for x in conn.execute("SELECT payload FROM live_execution_orders")
            )
        for value in ("test-secret", "test-passphrase", "0x" + "11" * 32, "test-key"):
            self.assertNotIn(value, output + payload + repr(self.config))
        self.assertNotIn("account_id", output + payload)
        self.assertNotIn("client_id", output)
        self.now += 30000
        with self.db() as conn:
            self.assertFalse(
                live.report(conn, self.config, now_ms=self.now)["real_orders_enabled"]
            )

    async def test_unexpected_sdk_error_never_logged_in_state(self):
        await self.armed()
        self.exchange.account = AsyncMock(side_effect=RuntimeError("SECRET-DO-NOT-LOG"))
        with self.assertRaisesRegex(ExecutionError, "EXECUTION_CYCLE_ERROR"):
            await self.engine.cycle()
        self.assertNotIn("SECRET", json.dumps(self.state()))

    def test_additive_migration_preserves_foreign_data_and_arm_time(self):
        with self.db() as conn:
            conn.execute("CREATE TABLE push_subscriptions(payload TEXT)")
            conn.execute("INSERT INTO push_subscriptions VALUES(?)", ("keep-original",))
            live.initialize(conn, now_ms=NOW)
            self.assertEqual(
                conn.execute("SELECT payload FROM push_subscriptions").fetchone()[0],
                "keep-original",
            )
            self.assertEqual(live.state(conn)["created_ms"], NOW - 60000)


class SizingTests(unittest.TestCase):
    def prepare(
        self,
        side="LONG",
        row_changes=None,
        meta_changes=None,
        config=None,
        account_changes=None,
        quote_changes=None,
        **times
    ):
        account = dict(
            equity=Decimal(10000),
            available=Decimal(1000),
            positions={},
            observed_ms=NOW,
        )
        account.update(account_changes or {})
        quote = dict(ask1Price=100, bid1Price=100, maxBuySize=100, maxSellSize=100)
        quote.update(quote_changes or {})
        return live.prepare(
            candidate(side, **(row_changes or {})),
            dict(META, **(meta_changes or {})),
            account,
            quote,
            config or configuration(),
            now_ms=times.get("now_ms", NOW),
            armed_ms=NOW - 60000,
            snapshot_ms=times.get("snapshot_ms", NOW),
        )

    def test_cost_stop_risk_and_one_times_notional_caps(self):
        r = self.prepare(account_changes={"available": Decimal(20)})
        self.assertLessEqual(decimal(r["notional_reserved_usdc"]), 20)
        self.assertLessEqual(decimal(r["risk_reserved_usdc"]), 10)
        self.assertEqual(decimal(r["quantity"]) % Decimal(".001"), 0)

    def test_sl_rounding_never_widens_both_directions(self):
        long = self.prepare(row_changes={"stop_loss": 90.005})
        short = self.prepare("SHORT", row_changes={"stop_loss": 109.995})
        self.assertEqual(decimal(long["stop_price"]), Decimal("90.01"))
        self.assertEqual(decimal(short["stop_price"]), Decimal("109.99"))

    def test_invalid_or_stale_data_rejected(self):
        cases = [
            dict(row_changes={"confirmed": False}),
            dict(row_changes={"retest_touched": False}),
            dict(row_changes={"take_profit": 110}),
            dict(row_changes={"stop_loss": 100}),
            dict(meta_changes={"enableTrade": False}),
            dict(meta_changes={"quoteCoinId": "other"}),
            dict(meta_changes={"minOrderSize": "20"}),
            dict(meta_changes={"defaultTakerFeeRate": ".005"}),
            dict(account_changes={"observed_ms": NOW - 5000}),
            dict(snapshot_ms=NOW - 120000),
            dict(now_ms=NOW + 90000),
            dict(row_changes={"latest_15m_time_ms": NOW}),
            dict(account_changes={"positions": {"10000001": Decimal(1)}}),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ExecutionError):
                self.prepare(**case)

    def test_nonfinite_bool_prices_are_rejected(self):
        for price in ("NaN", "Infinity", False, None):
            with self.subTest(price=price), self.assertRaises(ExecutionError):
                decimal(price)

    def test_live_configuration_requires_explicit_small_risk_limits(self):
        for name, value in [
            ("risk_pct", ""),
            ("risk_pct", "3.01"),
            ("max_risk_usdc", "0"),
            ("daily_loss_usdc", "NaN"),
            ("slippage_bps", "26"),
            ("fee_bps", "0"),
            ("signer_key", ""),
        ]:
            c = configuration()
            setattr(c, name, value)
            self.assertTrue(c.errors())


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = configuration()
        self.client = AsyncMock()
        self.adapter = EdgeXOrders(self.config, client=self.client)

    async def test_transport_and_rejection_messages_do_not_escape(self):
        for value in (
            {"code": "FAIL", "msg": "secret-response"},
            RuntimeError("secret-exception"),
        ):
            if isinstance(value, Exception):
                self.client.get_account_asset.side_effect = value
            else:
                self.client.get_account_asset.return_value = value
            with self.assertRaises(ExecutionError) as caught:
                await self.adapter.account()
            self.assertNotIn("secret", str(caught.exception))

    async def test_missing_positions_are_not_interpreted_as_flat(self):
        self.client.get_account_asset.return_value = {
            "code": "SUCCESS",
            "data": {"account": {"id": "123"}},
        }
        with self.assertRaisesRegex(ExecutionError, "INCOMPLETE_ACCOUNT"):
            await self.adapter.account()

    async def test_account_schema_identity_and_decimal_positions(self):
        raw = {
            "account": {"id": "123"},
            "collateralAssetModelList": [
                dict(
                    coinId="1000",
                    accountId="123",
                    totalEquity="10000",
                    availableAmount="50",
                )
            ],
            "positionList": [
                dict(accountId="123", contractId="10000001", openSize="-.01")
            ],
        }
        self.client.get_account_asset.return_value = {"code": "SUCCESS", "data": raw}
        self.assertEqual(
            (await self.adapter.account())["positions"]["10000001"], Decimal("-.01")
        )
        raw["positionList"][0]["accountId"] = "456"
        with self.assertRaisesRegex(ExecutionError, "ACCOUNT_ID_MISMATCH"):
            await self.adapter.account()

    async def test_order_pagination_cannot_hide_external_orders(self):
        self.client.get_active_orders.side_effect = [
            {
                "code": "SUCCESS",
                "data": {"dataList": [], "nextPageOffsetData": "second"},
            },
            {
                "code": "SUCCESS",
                "data": {
                    "dataList": [dict(accountId="123", clientOrderId="manual")],
                    "nextPageOffsetData": "",
                },
            },
        ]
        self.assertEqual(len(await self.adapter.active_orders()), 1)

    async def test_client_id_query_is_get_and_never_recreates_unknown_order(self):
        request = self.client.async_client.make_authenticated_request
        request.return_value = {
            "code": "SUCCESS",
            "data": [{"accountId": "123", "clientOrderId": "fixed-id"}],
        }
        self.assertEqual(
            (await self.adapter.order("fixed-id"))["clientOrderId"], "fixed-id"
        )
        request.assert_awaited_once_with(
            method="GET",
            path="/api/v2/private/order/getOrderByClientOrderId",
            params={"accountId": "123", "clientOrderIdList": "fixed-id"},
        )
        request.return_value = {"code": "SUCCESS", "data": []}
        self.assertIsNone(await self.adapter.order("fixed-id"))
        self.client.create_order.assert_not_called()

    async def test_missing_pagination_and_loop_block(self):
        for page in ({"dataList": []}, {"dataList": [], "nextPageOffsetData": "same"}):
            self.client.get_active_orders.return_value = {
                "code": "SUCCESS",
                "data": page,
            }
            with self.assertRaises(ExecutionError):
                await self.adapter.active_orders()

    async def test_adapter_mutation_requires_live_configuration(self):
        self.config.mode = "READ_ONLY"
        with self.assertRaises(ExecutionError):
            await self.adapter.create({}, "entry")
        with self.assertRaises(ExecutionError):
            await self.adapter.cancel("test-id")
        self.client.create_order.assert_not_called()
        self.client.cancel_order.assert_not_called()

    async def test_real_sdk_params_conditional_reduce_only_fixed_ids(self):
        r = SizingTests().prepare()
        self.client.create_order.return_value = {
            "code": "SUCCESS",
            "data": {"orderId": "42"},
        }
        for kind in ("entry", "sl", "tp", "close"):
            await self.adapter.create(r, kind, quantity=".01")
            p = self.client.create_order.call_args.args[0]
            self.assertEqual(p.client_order_id, r[kind + "_client_id"])
            self.assertEqual(p.reduce_only, kind != "entry")
            self.assertEqual(p.time_in_force, "IMMEDIATE_OR_CANCEL")
            if kind in ("sl", "tp"):
                self.assertEqual(p.trigger_price_type, "LAST_PRICE")
                self.assertTrue(p.is_position_tpsl)

    async def test_real_sdk_hmac_and_eip712_signatures_without_network(self):
        import base64
        import hashlib
        import hmac
        from edgex_sdk import Client
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        config = configuration()
        config.api_secret = "fixture-secret"
        client = Client(
            base_url="https://edgex-prod-v2.edgex.exchange",
            account_id=123,
            api_key=config.api_key,
            api_secret=config.api_secret,
            api_passphrase=config.passphrase,
            trading_private_key=config.signer_key,
        )
        metadata = {
            "global": {"nativeChainId": "3343", "contractAddress": "0x" + "22" * 20},
            "contractList": [dict(META, resolution="1e6")],
            "coinList": [dict(coinId="1000", resolution="1e6")],
        }
        client.get_metadata = AsyncMock(
            return_value={"code": "SUCCESS", "data": metadata}
        )
        client._get_market_order_price = AsyncMock(return_value="100")
        bodies = []
        typed = []
        sign = client.async_client.sign_typed_data_with_trading_key

        def signing(message):
            typed.append(message)
            return sign(message)

        client.async_client.sign_typed_data_with_trading_key = signing

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self):
                return {"code": "SUCCESS", "data": {"orderId": str(len(bodies))}}

        class Session:
            closed = False

            def request(self, **kwargs):
                bodies.append(kwargs)
                return Response()

            async def close(self):
                self.closed = True

        client.async_client._session = Session()
        adapter = EdgeXOrders(config, client=client)
        r = SizingTests().prepare(config=config)
        from edgex_sdk.internal.auth import get_value

        for kind in ("entry", "sl", "tp", "close"):
            await adapter.create(r, kind, quantity=".01")
            request = bodies[-1]
            body = request["json"]
            headers = request["headers"]
            self.assertEqual(
                request["url"],
                "https://edgex-prod-v2.edgex.exchange/api/v2/private/order/createOrder",
            )
            self.assertEqual(request["method"], "POST")
            signed = Account.recover_message(
                encode_typed_data(full_message=typed[-1]), signature=body["l2Signature"]
            )
            self.assertEqual(signed, Account.from_key(config.signer_key).address)
            self.assertEqual(typed[-1]["message"]["base"]["accountId"], "123")
            message = (
                headers["X-edgeX-Timestamp"]
                + "POST/api/v2/private/order/createOrder"
                + get_value(body)
            )
            # Official Golang sdk/client.go: Base64(secret) bytes, not decode.
            expected = hmac.new(
                base64.b64encode(b"fixture-secret"), message.encode(), hashlib.sha256
            ).hexdigest()
            self.assertEqual(headers["X-edgeX-Signature"], expected)
            self.assertEqual(body["clientOrderId"], r[kind + "_client_id"])
            self.assertEqual(body["reduceOnly"], kind != "entry")
        await adapter.close()


class PublicApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from analysis_terminal import server

        self.server = server
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for name, value in [
            ("DB_PATH", Path(self.directory.name) / "test.db"),
            ("_live_execution_config", live.Config()),
        ]:
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        server._init_db()

    async def test_api_is_read_only_and_exposes_no_order_or_account_details(self):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=self.server.app), base_url="http://test"
        ) as client:
            response = await client.get("/api/live-execution")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "OFF")
            self.assertFalse(response.json()["real_orders_enabled"])
            for method in ("post", "put", "delete"):
                self.assertEqual(
                    (
                        await client.request(method, "/api/live-execution", json={})
                    ).status_code,
                    405,
                )

    async def test_default_off_worker_does_not_initialize_sdk(self):
        with patch(
            "analysis_terminal.edgex_orders.EdgeXOrders",
            side_effect=AssertionError("SDK not allowed"),
        ):
            await self.server._background_live_execution()


if __name__ == "__main__":
    unittest.main()
