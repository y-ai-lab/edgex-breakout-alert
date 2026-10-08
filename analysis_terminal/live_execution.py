"""Opt-in current-READY execution with durable, at-most-once order attempts.

Default OFF. No public write API, strategy changes or Shadow promotion. A
dedicated flat account is required to arm; only one position can be managed.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import os
import time
import uuid
from zoneinfo import ZoneInfo

from analysis_terminal.edgex_orders import ExecutionError, decimal
from analysis_terminal.setups import setup_identity

STEP = 900000
TERMINAL = {"NO_FILL", "CLOSED", "SKIPPED"}


@dataclass
class Config:
    mode: str = "OFF"
    account_id: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    passphrase: str = field(default="", repr=False)
    signer_key: str = field(default="", repr=False)
    risk_pct: str = ""
    max_risk_usdc: str = ""
    max_notional_usdc: str = ""
    daily_loss_usdc: str = ""
    slippage_bps: str = "2"
    fee_bps: str = "5"
    control_request: str = field(default="", repr=False)

    @classmethod
    def from_env(cls):
        names = dict(
            mode="MODE",
            account_id="ACCOUNT_ID",
            api_key="API_KEY",
            api_secret="API_SECRET",
            passphrase="API_PASSPHRASE",
            signer_key="SIGNER_KEY",
            risk_pct="RISK_PCT",
            max_risk_usdc="MAX_RISK_USDC",
            max_notional_usdc="MAX_NOTIONAL_USDC",
            daily_loss_usdc="DAILY_LOSS_USDC",
            slippage_bps="SLIPPAGE_BPS",
            fee_bps="FEE_BPS",
            control_request="CONTROL_REQUEST",
        )
        defaults = cls()
        return cls(
            **{
                k: os.getenv("EDGEX_EXEC_" + v, getattr(defaults, k)).strip()
                for k, v in names.items()
            }
        )

    def errors(self):
        errors = []
        if self.mode not in {"OFF", "READ_ONLY", "LIVE"}:
            errors.append("INVALID_MODE")
        if not (
            self.account_id.isdigit()
            and int(self.account_id) > 0
            and self.api_key
            and self.api_secret
            and self.passphrase
        ):
            errors.append("CREDENTIALS_REQUIRED")
        if self.mode == "LIVE":
            if not self.signer_key:
                errors.append("SIGNER_KEY_REQUIRED")
            else:
                try:
                    key = self.signer_key.removeprefix("0x")
                    if len(key) != 64 or not 0 < int(key, 16) < int(
                        "fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141",
                        16,
                    ):
                        raise ValueError()
                except ValueError:
                    errors.append("INVALID_SIGNER_KEY")
            try:
                if not 0 < decimal(self.risk_pct) <= 3:
                    raise ExecutionError("BAD")
                for name, explicit_mode in (
                    ("max_risk_usdc", "ACCOUNT_RISK_PCT"),
                    ("max_notional_usdc", "ACCOUNT_EQUITY"),
                    ("daily_loss_usdc", "DISABLED"),
                ):
                    value = getattr(self, name)
                    if value != explicit_mode and decimal(value) <= 0:
                        raise ExecutionError("BAD")
                if (
                    not 0 <= decimal(self.slippage_bps) <= 25
                    or not 0 < decimal(self.fee_bps) <= 100
                ):
                    raise ExecutionError("BAD")
            except ExecutionError:
                errors.append("EXPLICIT_RISK_LIMITS_REQUIRED")
        return errors

    def risk_budget(self, equity):
        budget = decimal(equity) * decimal(self.risk_pct) / 100
        return (
            budget
            if self.max_risk_usdc == "ACCOUNT_RISK_PCT"
            else min(budget, decimal(self.max_risk_usdc))
        )

    def notional_limit(self, equity, available):
        budget = min(decimal(equity), decimal(available))
        return (
            budget
            if self.max_notional_usdc == "ACCOUNT_EQUITY"
            else min(budget, decimal(self.max_notional_usdc))
        )

    def daily_loss_limit(self, day_start_equity):
        if self.daily_loss_usdc == "DISABLED":
            return None
        return min(
            decimal(self.daily_loss_usdc), decimal(day_start_equity) * decimal(".03")
        )

    def fingerprint(self):
        # Bind the ledger to account and risk policy, never store credentials.
        policy = {
            k: getattr(self, k)
            for k in (
                "account_id",
                "risk_pct",
                "max_risk_usdc",
                "max_notional_usdc",
                "daily_loss_usdc",
                "slippage_bps",
                "fee_bps",
            )
        }
        return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def initialize(conn, *, now_ms):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS live_execution_state(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS live_execution_orders(setup_id TEXT PRIMARY KEY,payload TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS live_execution_events(id INTEGER PRIMARY KEY AUTOINCREMENT,setup_id TEXT,observed_ms INTEGER NOT NULL,status TEXT NOT NULL)"
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS live_execution_controls (
        request_id TEXT PRIMARY KEY, action TEXT NOT NULL, status TEXT NOT NULL,
        created_ms INTEGER NOT NULL, completed_ms INTEGER, code TEXT)""")
    initial = dict(
        armed=False,
        armed_ms=None,
        reason="NOT_ARMED",
        bound_fingerprint=None,
        last_success_ms=None,
        last_error=None,
        day=None,
        day_start_equity=None,
        created_ms=now_ms,
    )
    conn.execute(
        "INSERT OR IGNORE INTO live_execution_state VALUES(1,?)", (json.dumps(initial),)
    )


def state(conn):
    return json.loads(
        conn.execute("SELECT payload FROM live_execution_state WHERE id=1").fetchone()[
            0
        ]
    )


def save_state(conn, s):
    conn.execute(
        "UPDATE live_execution_state SET payload=? WHERE id=1", (json.dumps(s),)
    )


def orders(conn):
    return [
        json.loads(x[0])
        for x in conn.execute(
            "SELECT payload FROM live_execution_orders ORDER BY rowid"
        )
    ]


def save(conn, r, now_ms):
    prior = conn.execute(
        "SELECT payload FROM live_execution_orders WHERE setup_id=?", (r["setup_id"],)
    ).fetchone()
    previous = json.loads(prior[0]) if prior else None
    conn.execute(
        "INSERT INTO live_execution_orders VALUES(?,?) ON CONFLICT(setup_id) DO UPDATE SET payload=excluded.payload",
        (r["setup_id"], json.dumps(r, allow_nan=False)),
    )
    if previous is None or previous["status"] != r["status"]:
        conn.execute(
            "INSERT INTO live_execution_events(setup_id,observed_ms,status) VALUES(?,?,?)",
            (r["setup_id"], now_ms, r["status"]),
        )


def pause(conn, reason):
    s = state(conn)
    s.update(armed=False, reason=reason, control_epoch=s.get("control_epoch", 0) + 1)
    save_state(conn, s)


def report(conn, config, *, now_ms):
    s = state(conn)
    errors = config.errors()
    items = orders(conn)
    age = (
        None if s["last_success_ms"] is None else (now_ms - s["last_success_ms"]) / 1000
    )
    active = sum(r["status"] not in TERMINAL for r in items)
    status = (
        "OFF"
        if config.mode == "OFF"
        else (
            "CONFIGURATION_REQUIRED"
            if errors
            else (
                "ERROR"
                if s["last_error"]
                else (
                    "STALE_CONNECTION"
                    if age is None or age < 0 or age >= 30
                    else (
                        "READ_ONLY"
                        if config.mode == "READ_ONLY"
                        else "RUNNING" if s["armed"] else "PAUSED"
                    )
                )
            )
        )
    )
    control = conn.execute(
        "SELECT action,status,created_ms,completed_ms,code FROM live_execution_controls ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    blockers = s.get("last_preflight_arm_blockers")
    preflight_age = (
        None if s.get("last_preflight_ms") is None
        else (now_ms - s["last_preflight_ms"]) / 1000
    )
    return dict(
        mode=config.mode,
        status=status,
        armed=s["armed"],
        real_orders_enabled=config.mode == "LIVE"
        and not errors
        and s["armed"]
        and status == "RUNNING",
        protective_management_enabled=config.mode == "LIVE" and not errors,
        source="CURRENT_READY_ONLY",
        automatic_promotion=False,
        credentials_configured="CREDENTIALS_REQUIRED" not in errors,
        signing_configured=bool(config.signer_key),
        configuration_errors=errors,
        # Validate all LIVE prerequisites without enabling orders or exposing
        # keys, balances, or the private account policy in the public report.
        live_configuration_errors=replace(config, mode="LIVE").errors(),
        last_success_ms=s["last_success_ms"],
        last_error=s["last_error"],
        reason=s["reason"],
        active_orders=active,
        status_counts=dict(Counter(r["status"] for r in items)),
        account_details_exposed=False,
        last_preflight_ms=s.get("last_preflight_ms"),
        preflight_arm_blockers=blockers,
        preflight_arm_blockers_current=blockers is not None
        and preflight_age is not None and 0 <= preflight_age < 30,
        preflight_is_read_only=True,
        preflight_validates_signer_authorization=False,
        operator_control=(
            dict(
                zip(("action", "status", "created_ms", "completed_ms", "code"), control)
            )
            if control
            else None
        ),
    )


def flat_account_blockers(account, active_exchange_orders, ledger):
    """Safe reason codes only; same dedicated-account checks used by arm."""
    blockers = []
    if any(account["positions"].values()):
        blockers.append("EXISTING_ACCOUNT_POSITION")
    if active_exchange_orders:
        blockers.append("ACTIVE_EXCHANGE_ORDERS")
    if any(r["status"] not in TERMINAL for r in ledger):
        blockers.append("UNRESOLVED_EXECUTION_LEDGER")
    return blockers


def parse_control(request):
    try:
        action, token = request.split(":")
        identifier = uuid.UUID(token)
        if (
            action not in {"check", "arm", "pause"}
            or str(identifier) != token
            or identifier.version != 4
        ):
            raise ValueError()
        return action, token
    except (ValueError, AttributeError):
        raise ExecutionError("INVALID_CONTROL_REQUEST") from None


async def apply_control(engine, request, *, adapter_factory=None):
    """Private environment command; commit once before any remote operation."""
    if not request:
        return
    try:
        action, token = parse_control(request)
    except ExecutionError:
        engine.halt("INVALID_CONTROL_REQUEST")
        return
    with engine.db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute(
            "SELECT action,status FROM live_execution_controls WHERE request_id=?",
            (token,),
        ).fetchone()
        if prior:
            if prior[0] != action:
                pause(conn, "CONTROL_CONTENT_CHANGED")
            elif prior[1] == "PROCESSING":
                pause(conn, "CONTROL_INTERRUPTED")
            return
        conn.execute(
            "INSERT INTO live_execution_controls VALUES(?,?,?, ?,NULL,NULL)",
            (token, action, "PROCESSING", engine.clock()),
        )
        if action == "pause":
            pause(conn, "MANUAL_PAUSE")
            conn.execute(
                "UPDATE live_execution_controls SET status='DONE',completed_ms=? WHERE request_id=?",
                (engine.clock(), token),
            )
            return
    try:
        if engine.config.mode == "OFF" or engine.config.errors():
            raise ExecutionError("CONFIGURATION_REQUIRED")
        if engine.adapter is None:
            if adapter_factory is None:
                raise ExecutionError("CLIENT_REQUIRED")
            engine.adapter = adapter_factory()
        if action == "arm":
            with engine.db_connect() as conn:
                previous = [
                    json.loads(x[0])
                    for x in conn.execute("SELECT payload FROM paper_signals")
                ]
            await engine.arm(previous_ready=previous)
        else:
            await engine.preflight()
        code, status = None, "DONE"
    except Exception:
        # Never persist an SDK exception or echoed credentials as a control code.
        engine.halt("OPERATOR_REQUEST_FAILED")
        code, status = "PRECONDITIONS_OR_CONNECTION_FAILED", "REFUSED"
    with engine.db_connect() as conn:
        conn.execute(
            "UPDATE live_execution_controls SET status=?,completed_ms=?,code=? WHERE request_id=?",
            (status, engine.clock(), code, token),
        )


def rounded(value, tick, up=False):
    v, t = decimal(value), decimal(tick)
    if v <= 0 or t <= 0:
        raise ExecutionError("INVALID_PRICE_GRID")
    return (v / t).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * t


def prepare(row, meta, account, quote, config, *, now_ms, armed_ms, snapshot_ms):
    identity = setup_identity(row)
    if (
        not identity
        or row.get("setup_id") != identity
        or row.get("stage") != "READY"
        or row.get("confirmed") is not True
        or row.get("retest_touched") is not True
    ):
        raise ExecutionError("CURRENT_READY_REQUIRED")
    cid = str(row.get("contract_id"))
    if (
        str(meta.get("contractId")) != cid
        or meta.get("contractName") != row["ticker"]
        or str(meta.get("quoteCoinId")) != "1000"
    ):
        raise ExecutionError("CONTRACT_MISMATCH")
    if (
        meta.get("enableTrade") is not True
        or meta.get("enableOpenPosition") is not True
    ):
        raise ExecutionError("CONTRACT_DISABLED")
    source = row.get("latest_15m_time_ms")
    if (
        not isinstance(source, int)
        or source % STEP
        or source + STEP <= armed_ms
        or not 0 <= now_ms - (source + STEP) < 120000
    ):
        raise ExecutionError("STALE_OR_PRE_ARM_SIGNAL")
    if (
        snapshot_ms is None
        or not 0 <= now_ms - snapshot_ms < 120000
        or not 0 <= now_ms - account["observed_ms"] < 5000
    ):
        raise ExecutionError("STALE_ACCOUNT_OR_SCAN")
    if any(account["positions"].values()):
        raise ExecutionError("ACCOUNT_NOT_FLAT")
    long = row["direction"] == "LONG"
    tick, step = decimal(meta["tickSize"]), decimal(meta["stepSize"])
    if tick <= 0 or step <= 0:
        raise ExecutionError("INVALID_CONTRACT_GRID")
    fee = decimal(config.fee_bps) / 10000
    if (
        max(decimal(meta["defaultTakerFeeRate"]), decimal(meta["defaultMakerFeeRate"]))
        > fee
    ):
        raise ExecutionError("FEE_BUDGET_TOO_LOW")
    price = decimal(quote["ask1Price" if long else "bid1Price"])
    slip = decimal(config.slippage_bps) / 10000
    limit = rounded(price * (1 + slip if long else 1 - slip), tick, up=not long)
    # Move SL toward entry; rounding never widens the original structural stop.
    stop = rounded(row["stop_loss"], tick, up=long)
    target = rounded(row["take_profit"], tick, up=not long)
    reference = decimal(row["entry_reference"])
    if (
        not (stop < reference < target if long else target < reference < stop)
        or abs(target - reference) / abs(reference - stop) < 2
    ):
        raise ExecutionError("INVALID_STRUCTURAL_READY")
    if not (stop < limit < target if long else target < limit < stop):
        raise ExecutionError("INVALID_LIVE_LEVELS")
    risk = abs(limit - stop)
    if abs(target - limit) / risk < 2:
        raise ExecutionError("LIVE_RR_BELOW_2")
    stop_exit = stop * (1 - slip if long else 1 + slip)
    cost_risk = abs(limit - stop_exit) + (limit + stop_exit) * fee
    budget = config.risk_budget(account["equity"])
    notional = config.notional_limit(account["equity"], account["available"])
    maximum = min(
        decimal(meta["maxOrderSize"]),
        decimal(quote["maxBuySize" if long else "maxSellSize"]),
    )
    quantity = (
        min(budget / cost_risk, notional / (limit * (1 + fee)), maximum) / step
    ).to_integral_value(rounding=ROUND_FLOOR) * step
    if quantity < decimal(meta["minOrderSize"]) or quantity <= 0:
        raise ExecutionError("SIZE_BELOW_MINIMUM")
    token = hashlib.sha256(
        (config.fingerprint() + ":" + identity).encode()
    ).hexdigest()[:32]
    return dict(
        setup_id=identity,
        contract_id=cid,
        ticker=row["ticker"],
        side=row["direction"],
        created_ms=source + STEP + 1,
        observed_ms=now_ms,
        quantity=str(quantity),
        step_size=str(step),
        limit_price=str(limit),
        stop_price=str(stop),
        target_price=str(target),
        structural_stop=str(row["stop_loss"]),
        structural_target=str(row["take_profit"]),
        risk_reserved_usdc=str(quantity * cost_risk),
        notional_reserved_usdc=str(quantity * limit),
        status="PREPARED",
        notional_limit_usdc=str(notional / (1 + fee)),
        entry_deadline_ms=source + STEP + 120000,
        protection_deadline_ms=now_ms + 21 * 86400000,
        **{
            kind + "_client_id": "ex-" + token + "-" + kind
            for kind in ("entry", "sl", "tp", "close")
        }
    )


def verify_order(order, r, kind):
    if (
        order.get("clientOrderId") != r[kind + "_client_id"]
        or str(order.get("contractId")) != r["contract_id"]
        or order.get("side")
        != ("BUY" if (r["side"] == "LONG") == (kind == "entry") else "SELL")
    ):
        raise ExecutionError("ORDER_CONTENT_MISMATCH")
    if kind != "entry" and order.get("reduceOnly") is not True:
        raise ExecutionError("UNSAFE_EXIT_ORDER")
    wanted = (
        r.get("close_size", r.get("filled_size", r["quantity"]))
        if kind == "close"
        else r.get("filled_size", r["quantity"])
    )
    if decimal(order.get("size")) != decimal(wanted) and kind != "entry":
        raise ExecutionError("EXIT_SIZE_MISMATCH")
    if kind == "entry" and decimal(order.get("size")) != decimal(r["quantity"]):
        raise ExecutionError("ENTRY_SIZE_MISMATCH")
    fill = decimal(order.get("cumFillSize"))
    if (
        fill < 0
        or fill > decimal(order.get("size"))
        or decimal(order.get("cumFillFee")) < 0
    ):
        raise ExecutionError("INVALID_FILL_SIZE")
    if fill > 0 and decimal(order.get("cumFillValue")) <= 0:
        raise ExecutionError("INVALID_FILL_VALUE")
    if kind == "entry" and (
        order.get("type") != "LIMIT"
        or order.get("timeInForce") != "IMMEDIATE_OR_CANCEL"
        or order.get("reduceOnly") is not False
        or decimal(order.get("price")) != decimal(r["limit_price"])
        or decimal(order.get("expireTime")) != decimal(r["entry_deadline_ms"])
    ):
        raise ExecutionError("ENTRY_CONTENT_MISMATCH")
    if kind == "close" and (
        order.get("type") != "MARKET"
        or order.get("timeInForce") != "IMMEDIATE_OR_CANCEL"
    ):
        raise ExecutionError("UNSAFE_EXIT_ORDER")
    if kind in ("sl", "tp"):
        if (
            order.get("type")
            != ("STOP_MARKET" if kind == "sl" else "TAKE_PROFIT_MARKET")
            or decimal(order.get("triggerPrice"))
            != decimal(r["stop_price" if kind == "sl" else "target_price"])
            or order.get("triggerPriceType") != "LAST_PRICE"
            or decimal(order.get("expireTime")) != decimal(r["protection_deadline_ms"])
        ):
            raise ExecutionError("PROTECTION_MISMATCH")
    return order


class Engine:
    def __init__(self, db_connect, config, adapter, *, clock=None):
        self.db_connect, self.config, self.adapter = db_connect, config, adapter
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.metadata = None

    def update(self, r, status=None, **fields):
        if status:
            r["status"] = status
        r.update(fields)
        with self.db_connect() as conn:
            save(conn, r, self.clock())

    def halt(self, reason):
        with self.db_connect() as conn:
            pause(conn, reason)

    async def send(self, r, kind):
        # Commit BEFORE the network call: lost ACK/restart never resends this ID.
        with self.db_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if kind == "entry" and not state(conn)["armed"]:
                raise ExecutionError("PAUSED_BEFORE_SEND")
            stored = conn.execute(
                "SELECT payload FROM live_execution_orders WHERE setup_id=?",
                (r["setup_id"],),
            ).fetchone()
            if stored is None:
                raise ExecutionError("PERSISTED_INTENT_REQUIRED")
            if r.get(kind + "_attempted") or json.loads(stored[0]).get(
                kind + "_attempted"
            ):
                raise ExecutionError("DUPLICATE_SEND_BLOCKED")
            if kind == "entry" and self.clock() >= r["entry_deadline_ms"]:
                raise ExecutionError("ENTRY_EXPIRED")
            r[kind + "_attempted"] = True
            r["status"] = "SENDING_" + kind.upper()
            save(conn, r, self.clock())
        try:
            oid = await self.adapter.create(
                r,
                kind,
                quantity=(
                    r.get("close_size") if kind == "close" else r.get("filled_size")
                ),
            )
        except ExecutionError:
            self.halt("ORDER_ACK_UNKNOWN")
            raise
        self.update(r, "ACKED_" + kind.upper(), **{kind + "_order_id": oid})

    async def emergency_close(self, r, quantity=None):
        self.halt("PROTECTION_OR_FILL_INVALID")
        if not r.get("close_attempted"):
            self.update(r, close_size=str(quantity or r["filled_size"]))
            await self.send(r, "close")
        close = await self.adapter.order(r["close_client_id"])
        if close is not None:
            verify_order(close, r, "close")
        # Never call a market order filled/flat from its acknowledgement.
        self.update(r, "CLOSE_PENDING")

    async def reconcile(self, r):
        entry = await self.adapter.order(r["entry_client_id"])
        if entry is None:
            self.halt("ENTRY_ACK_UNRESOLVED")
            return
        verify_order(entry, r, "entry")
        filled = decimal(entry.get("cumFillSize"))
        if filled < 0 or filled > decimal(r["quantity"]):
            raise ExecutionError("INVALID_FILL_SIZE")
        status = entry.get("status")
        if status not in {"FILLED", "CANCELED"}:
            if (filled > 0 or self.clock() - r["observed_ms"] >= 10000) and not r.get(
                "entry_cancel_attempted"
            ):
                self.update(r, entry_cancel_attempted=True)
                await self.adapter.cancel(r["entry_client_id"])
                self.halt("ENTRY_NOT_TERMINAL")
            return  # Do not assume IOC is terminal or risk changed by cancellation.
        if not filled:
            if status == "FILLED":
                raise ExecutionError("INVALID_FILL_SIZE")
            self.update(r, "NO_FILL")
            return
        if r.get("filled_size") and decimal(r["filled_size"]) != filled:
            raise ExecutionError("FILL_CHANGED_AFTER_TERMINAL")
        value = decimal(entry.get("cumFillValue"))
        fee = decimal(entry.get("cumFillFee"))
        if value <= 0 or fee < 0:
            raise ExecutionError("INVALID_FILL_VALUE")
        price = value / filled
        self.update(
            r,
            filled_size=str(filled),
            entry_fill_price=str(price),
            entry_fee_usdc=str(fee),
        )
        a = await self.adapter.account()
        position = a["positions"].get(r["contract_id"], decimal(0))
        if position == 0:
            # Filled entry plus a flat account is closed only if an owned exit
            # has a matching terminal exchange fill. Otherwise external state.
            exits = []
            for kind in ("sl", "tp", "close"):
                if not r.get(kind + "_attempted"):
                    continue
                order = await self.adapter.order(r[kind + "_client_id"])
                if order is not None:
                    verify_order(order, r, kind)
                    if decimal(order.get("cumFillSize")) > 0:
                        exits.append(order)
            if sum((decimal(x["cumFillSize"]) for x in exits), decimal(0)) != filled:
                self.halt("UNEXPLAINED_FLAT_POSITION")
                return
            for kind in ("sl", "tp"):
                if r.get(kind + "_attempted"):
                    o = await self.adapter.order(r[kind + "_client_id"])
                    if o is not None and o.get("status") not in {"FILLED", "CANCELED"}:
                        # At most one cancel attempt, then query until terminal.
                        if not r.get(kind + "_cancel_attempted"):
                            self.update(r, **{kind + "_cancel_attempted": True})
                            await self.adapter.cancel(r[kind + "_client_id"])
                        o = await self.adapter.order(r[kind + "_client_id"])
                    if o is None or o.get("status") not in {"FILLED", "CANCELED"}:
                        self.halt("EXIT_CANCEL_UNCONFIRMED")
                        return
            exit_value = sum((decimal(x["cumFillValue"]) for x in exits), decimal(0))
            exit_fee = sum((decimal(x["cumFillFee"]) for x in exits), decimal(0))
            pnl = (
                (exit_value - value if r["side"] == "LONG" else value - exit_value)
                - fee
                - exit_fee
            )
            self.update(
                r,
                "CLOSED",
                trade_pnl_before_funding_usdc=str(pnl),
                outcome_observed_ms=self.clock(),
            )
            return
        expected = filled if r["side"] == "LONG" else -filled
        if position != expected:
            exited = decimal(0)
            for kind in ("sl", "tp", "close"):
                if r.get(kind + "_attempted"):
                    o = await self.adapter.order(r[kind + "_client_id"])
                    if o is not None:
                        verify_order(o, r, kind)
                        exited += decimal(o["cumFillSize"])
            if (
                exited > 0
                and abs(position) + exited == filled
                and position * expected > 0
                and not r.get("close_attempted")
            ):
                await self.emergency_close(r, quantity=abs(position))
                return
            await self.emergency_close(r)
            return
        if any(v for k, v in a["positions"].items() if k != r["contract_id"]):
            self.halt("POSITION_MISMATCH")
            return
        if r.get("close_attempted"):
            self.halt("EMERGENCY_CLOSE_UNRESOLVED")
            return
        long = r["side"] == "LONG"
        stop = decimal(r["stop_price"])
        target = decimal(r["target_price"])
        valid = stop < price < target if long else target < price < stop
        slip = decimal(self.config.slippage_bps) / 10000
        stop_exit = stop * (1 - slip if long else 1 + slip)
        risk = (
            filled
            * (
                abs(price - stop_exit)
                + stop_exit * decimal(self.config.fee_bps) / 10000
            )
            + fee
        )
        if (
            not valid
            or abs(target - price) / abs(price - stop) < 2
            or risk > decimal(r["risk_reserved_usdc"]) + decimal("0.00000001")
            or value > decimal(r["notional_limit_usdc"]) + decimal("0.00000001")
            or self.clock() >= r["protection_deadline_ms"] - 60000
        ):
            await self.emergency_close(r)
            return
        for kind in ("sl", "tp"):
            if not r.get(kind + "_attempted"):
                try:
                    await self.send(r, kind)
                except ExecutionError:
                    await self.emergency_close(r)
                    return
            try:
                order = await self.adapter.order(r[kind + "_client_id"])
            except ExecutionError:
                await self.emergency_close(r)
                return
            if order is None:
                self.halt("PROTECTION_ACK_UNRESOLVED")
                await self.emergency_close(r)
                return
            try:
                verify_order(order, r, kind)
            except ExecutionError:
                await self.emergency_close(r)
                return
            if order.get("status") != "UNTRIGGERED":
                # An exit may have just executed. Reconcile the account before
                # deciding protection was lost; reduce-only prevents reversal.
                a2 = await self.adapter.account()
                if a2["positions"].get(r["contract_id"], decimal(0)) == 0:
                    return
                await self.emergency_close(r)
                return
        self.update(r, "PROTECTED")

    async def preflight(self):
        # A failed fresh check must not leave a previous flat-account result
        # presented as current. Do not change the operator's armed state.
        with self.db_connect() as conn:
            s = state(conn)
            s["last_preflight_arm_blockers"] = None
            save_state(conn, s)
        if self.config.mode == "OFF" or self.config.errors():
            raise ExecutionError("CONFIGURATION_REQUIRED")
        metadata = await self.adapter.metadata()
        try:
            g = metadata["global"]
            if int(g.get("nativeChainId") or g["chainId"]) <= 0:
                raise ValueError()
            address = g["contractAddress"]
            if len(address) != 42 or not address.startswith("0x"):
                raise ValueError()
            int(address[2:], 16)
            contracts = metadata["contractList"]
            if not isinstance(contracts, list) or not contracts:
                raise ValueError()
            ids = [str(c["contractId"]) for c in contracts]
            if len(set(ids)) != len(ids) or not all(c.isdigit() for c in ids):
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ExecutionError("INCOMPLETE_SIGNING_METADATA") from None
        a = await self.adapter.account()
        active = await self.adapter.active_orders()
        if not 0 <= self.clock() - a["observed_ms"] < 5000:
            raise ExecutionError("STALE_ACCOUNT_OR_SCAN")
        self.metadata = metadata
        with self.db_connect() as conn:
            s = state(conn)
            s.update(
                last_preflight_ms=self.clock(), last_success_ms=self.clock(),
                last_preflight_arm_blockers=flat_account_blockers(a, active, orders(conn)),
            )
            save_state(conn, s)
        return a, active

    async def arm(self, previous_ready=()):
        if self.config.mode != "LIVE" or self.config.errors():
            raise ExecutionError("LIVE_CONFIGURATION_REQUIRED")
        with self.db_connect() as conn:
            pause(conn, "ARM_CHECK_IN_PROGRESS")
            epoch = state(conn).get("control_epoch", 0)
        a, active = await self.preflight()
        with self.db_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if flat_account_blockers(a, active, orders(conn)):
                raise ExecutionError("DEDICATED_FLAT_ACCOUNT_REQUIRED")
            s = state(conn)
            if s.get("control_epoch", 0) != epoch:
                raise ExecutionError("PAUSED_DURING_ARM_CHECK")
            day = (
                datetime.fromtimestamp(self.clock() / 1000, ZoneInfo("Asia/Tokyo"))
                .date()
                .isoformat()
            )
            if s["day"] == day and s["day_start_equity"] is not None:
                loss = self.config.daily_loss_limit(s["day_start_equity"])
                if (
                    loss is not None
                    and decimal(s["day_start_equity"]) - a["equity"] >= loss
                ):
                    raise ExecutionError("DAILY_EQUITY_LOSS_LIMIT")
            s.update(
                armed=True,
                armed_ms=self.clock(),
                reason=None,
                bound_fingerprint=self.config.fingerprint(),
                last_error=None,
            )
            for signal in previous_ready:
                identity = signal.get("setup_id")
                if (
                    identity
                    and not conn.execute(
                        "SELECT 1 FROM live_execution_orders WHERE setup_id=?",
                        (identity,),
                    ).fetchone()
                ):
                    save(conn, dict(setup_id=identity, status="SKIPPED"), self.clock())
            save_state(conn, s)

    async def cycle(self, rows=(), *, snapshot_ms=None):
        if self.config.mode == "OFF":
            return
        if self.config.errors():
            raise ExecutionError("CONFIGURATION_REQUIRED")
        try:
            with self.db_connect() as conn:
                s = state(conn)
                items = orders(conn)
            if s["bound_fingerprint"] not in (None, self.config.fingerprint()):
                raise ExecutionError("ACCOUNT_OR_POLICY_CHANGED")
            if self.config.mode == "LIVE":
                for r in items:
                    if r["status"] not in TERMINAL:
                        try:
                            await self.reconcile(r)
                        except ExecutionError:
                            # Known fill with unreadable protection/account:
                            # try a single reduce-only close, never a new entry.
                            if r.get("filled_size") and not r.get("close_attempted"):
                                await self.emergency_close(r)
                            raise
            a = await self.adapter.account()
            active = await self.adapter.active_orders()
            with self.db_connect() as conn:
                s = state(conn)
                s["last_success_ms"] = self.clock()
                s["last_error"] = None
                day = (
                    datetime.fromtimestamp(self.clock() / 1000, ZoneInfo("Asia/Tokyo"))
                    .date()
                    .isoformat()
                )
                if s["day"] != day:
                    s.update(day=day, day_start_equity=str(a["equity"]))
                if self.config.mode == "LIVE":
                    loss = self.config.daily_loss_limit(s["day_start_equity"])
                    if (
                        loss is not None
                        and decimal(s["day_start_equity"]) - a["equity"] >= loss
                    ):
                        s.update(armed=False, reason="DAILY_EQUITY_LOSS_LIMIT")
                save_state(conn, s)
                items = orders(conn)
            if (
                self.config.mode != "LIVE"
                or not s["armed"]
                or any(r["status"] not in TERMINAL for r in items)
            ):
                return
            if active or any(a["positions"].values()):
                self.halt("EXTERNAL_ACCOUNT_ACTIVITY")
                return
            self.metadata = await self.adapter.metadata()
            metas = {str(m["contractId"]): m for m in self.metadata["contractList"]}
            seen = {r["setup_id"] for r in items}
            for row in sorted(
                rows, key=lambda r: float(r.get("score") or 0), reverse=True
            ):
                identity = setup_identity(row)
                if identity in seen or row.get("stage") != "READY":
                    continue
                try:
                    quote = await self.adapter.quote(
                        str(row["contract_id"]), row["entry_reference"]
                    )
                    r = prepare(
                        row,
                        metas.get(str(row["contract_id"]), {}),
                        a,
                        quote,
                        self.config,
                        now_ms=self.clock(),
                        armed_ms=s["armed_ms"],
                        snapshot_ms=snapshot_ms,
                    )
                except (ExecutionError, KeyError, TypeError, ValueError):
                    # Do not rearm a rejected setup on a later favourable quote.
                    if identity:
                        with self.db_connect() as conn:
                            save(
                                conn,
                                dict(setup_id=identity, status="SKIPPED"),
                                self.clock(),
                            )
                    continue
                with self.db_connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute(
                        "SELECT 1 FROM live_execution_orders WHERE setup_id=?",
                        (identity,),
                    ).fetchone():
                        return
                    save(conn, r, self.clock())
                await self.send(r, "entry")
                try:
                    await self.reconcile(r)
                except ExecutionError:
                    if r.get("filled_size") and not r.get("close_attempted"):
                        await self.emergency_close(r)
                    raise
                break
        except Exception as exc:
            code = (
                str(exc) if isinstance(exc, ExecutionError) else "EXECUTION_CYCLE_ERROR"
            )
            with self.db_connect() as conn:
                s = state(conn)
                s.update(armed=False, reason="RECONCILIATION_REQUIRED", last_error=code)
                save_state(conn, s)
            # Caller logs only this fixed safe machine code.
            raise ExecutionError(code) from None
