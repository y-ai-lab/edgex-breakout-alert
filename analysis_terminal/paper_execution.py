"""A persistent simulation ledger. No exchange client or order transport exists here."""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

from analysis_terminal.setups import setup_identity

STEP = 900_000
JST = ZoneInfo("Asia/Tokyo")
ACTIVE = {"PENDING", "OPEN", "AMBIGUOUS"}
POLICY = dict(version=1, initial_cash_usdc=10000.0, risk_pct=1.0,
              max_positions=3, max_total_risk_pct=3.0, max_notional_multiple=1.0,
              daily_loss_pct=3.0, fee_bps=5.0, slippage_bps=2.0,
              freshness_ms=120000, pending_ttl_ms=2*STEP)


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def floor_size(size, step):
    return float((Decimal(str(max(0.0, size))) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step)))


def stop_cost_per_unit(entry, stop, side, policy):
    exit_stop = stop*(1-side*policy["slippage_bps"]/10000)
    return abs(entry-exit_stop)+(entry+exit_stop)*policy["fee_bps"]/10000


def outstanding_risk(order):
    # Entry fees have already reduced cash; do not reserve them a second time.
    return max(0.0,order["reserved_risk_usdc"]-order.get("entry_fee_usdc",0))


def _append_event(conn, order, now_ms, *, origin, previous=None):
    """Persist observed transitions; market time never replaces observation time."""
    market_ms = None
    if order["status"] == "OPEN":
        market_ms = order.get("filled_ms")
    elif order["status"] in {"TP", "SL", "AMBIGUOUS"}:
        candle_ms = order.get("exit_candle_ms")
        market_ms = candle_ms + STEP if candle_ms is not None else None
    event = dict(order_id=order["order_id"], setup_id=order["setup_id"],
                 ticker=order["ticker"], status=order["status"],
                 quality=order.get("quality"), reason=order.get("reason"),
                 previous_status=previous.get("status") if previous else None,
                 previous_quality=previous.get("quality") if previous else None,
                 observed_ms=now_ms, market_ms=market_ms, origin=origin,
                 order=dict(order))
    conn.execute("""INSERT INTO simulated_order_events
        (order_id,setup_id,status,observed_ms,origin,payload) VALUES(?,?,?,?,?,?)""",
        (order["order_id"], order["setup_id"], order["status"], now_ms, origin,
         json.dumps(event, separators=(",", ":"))))


def _write(conn, order, now_ms, *, origin="LIVE_CYCLE"):
    saved = conn.execute("SELECT payload FROM simulated_orders WHERE order_id=?",
                         (order["order_id"],)).fetchone()
    previous = json.loads(saved[0]) if saved else None
    conn.execute("""INSERT INTO simulated_orders(order_id,payload,created_ms,updated_ms)
        VALUES(?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET
        payload=excluded.payload, updated_ms=excluded.updated_ms""",
        (order["order_id"], json.dumps(order, separators=(",", ":")), order["created_ms"], now_ms))
    if previous is None or any(previous.get(k) != order.get(k) for k in ("status", "quality", "reason")):
        _append_event(conn, order, now_ms, origin=origin, previous=previous)


def orders(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM simulated_orders ORDER BY created_ms,order_id")]


def account(conn):
    return json.loads(conn.execute("SELECT payload FROM simulated_account WHERE id=1").fetchone()[0])


def _save_account(conn, state):
    conn.execute("UPDATE simulated_account SET payload=? WHERE id=1", (json.dumps(state, separators=(",", ":")),))


def initialize(conn, *, now_ms, previous_signals, min_rr):
    """Additive migration; baseline old signals once, never create past fills."""
    conn.execute("""CREATE TABLE IF NOT EXISTS simulated_orders(
        order_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
        created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL)""")
    conn.execute("CREATE TABLE IF NOT EXISTS simulated_account(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS simulated_order_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL,
        setup_id TEXT NOT NULL, status TEXT NOT NULL, observed_ms INTEGER NOT NULL,
        origin TEXT NOT NULL, payload TEXT NOT NULL,
        FOREIGN KEY(order_id) REFERENCES simulated_orders(order_id))""")
    conn.execute("""CREATE INDEX IF NOT EXISTS simulated_order_event_order
        ON simulated_order_events(order_id,id)""")
    state = dict(mode="PAPER_ONLY", policy=dict(POLICY, min_rr=float(min_rr)),
                 activated_ms=now_ms, paused=False, pause_reason=None, pause_ms=None,
                 last_cycle_ms=None, last_error=None, event_tracking_started_ms=now_ms)
    new = conn.execute("INSERT OR IGNORE INTO simulated_account VALUES(1,?)", (json.dumps(state),)).rowcount
    if new:
        for s in previous_signals:
            identity = setup_identity(dict(s, direction=s.get("side")))
            if identity is not None and s.get("setup_id") == identity:
                _write(conn, dict(order_id="current:"+identity, setup_id=identity, ticker=s["ticker"],
                                  created_ms=now_ms, status="BASELINED", reason="PRE_EXISTING_SIGNAL"),
                       now_ms, origin="ACTIVATION_BASELINE")
    else:
        state = account(conn)
        if "event_tracking_started_ms" not in state:
            state["event_tracking_started_ms"] = now_ms
            _save_account(conn, state)
        # An old ledger gives only its current state, not an observed transition history.
        # Import it once, at migration time, without inventing past PENDING/OPEN events.
        for o in orders(conn):
            if conn.execute("SELECT 1 FROM simulated_order_events WHERE order_id=? LIMIT 1",
                            (o["order_id"],)).fetchone() is None:
                _append_event(conn, o, now_ms, origin="MIGRATION_SNAPSHOT")


def cash_view(state, items, now_ms):
    initial = state["policy"]["initial_cash_usdc"]
    midnight = int(datetime.fromtimestamp(now_ms/1000, JST).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()*1000)
    flows = []
    for o in items:
        if o.get("filled_ms") is not None:
            flows.append((o["filled_ms"], -o["entry_fee_usdc"]))
        if o.get("status") in {"TP", "SL"}:
            flows.append((o["exit_candle_ms"]+STEP, o["gross_pnl_usdc"]-o["exit_fee_usdc"]))
    cash = initial + sum(v for _,v in flows)
    day_start = initial + sum(v for t,v in flows if t < midnight)
    return dict(known_cash_usdc=cash, daily_start_cash_usdc=day_start,
                daily_cash_change_usdc=sum(v for t,v in flows if t >= midnight),
                fees_paid_usdc=sum(o.get("entry_fee_usdc",0)+o.get("exit_fee_usdc",0) for o in items),
                gross_realized_pnl_usdc=sum(o.get("gross_pnl_usdc",0) for o in items))


def _pause(state, reason, now_ms):
    if not state["paused"]:
        state.update(paused=True, pause_reason=reason, pause_ms=now_ms)


def _valid_candle(c, contract_id):
    return (c.contract_id == contract_id and c.interval == "MINUTE_15"
            and isinstance(c.time_ms,int) and c.time_ms % STEP == 0
            and all(number(v) is not None and v > 0 for v in (c.open,c.high,c.low,c.close))
            and c.low <= min(c.open,c.close) <= max(c.open,c.close) <= c.high)


def _fill(order, candle, policy, now_ms):
    side = 1 if order["side"] == "LONG" else -1
    price = candle.open * (1 + side*policy["slippage_bps"]/10000)
    distance = abs(price-order["stop"])
    valid = order["stop"] < price < order["target"] if side == 1 else order["target"] < price < order["stop"]
    rr = abs(order["target"]-price)/distance if valid and distance else None
    if rr is None or rr < policy["min_rr"]:
        order.update(status="REJECTED",reason="LEVELS_OR_RR_INVALID_AT_FILL",fill_rr=rr)
        return
    # Quantity may shrink against precommitted limits; later profits cannot increase it.
    cost_risk = stop_cost_per_unit(price,order["stop"],side,policy)
    qty = floor_size(min(order["quantity"],order["reserved_risk_usdc"]/cost_risk,
                         order["reserved_notional_usdc"]/price),order["step_size"])
    if qty < order["min_order_size"] or qty <= 0:
        order.update(status="REJECTED",reason="SIZE_TOO_SMALL_AT_FILL")
        return
    order.update(status="OPEN",reason=None,quantity=qty,fill_price=price,fill_rr=rr,
                 filled_ms=candle.time_ms,fill_observed_ms=now_ms,
                 entry_fee_usdc=qty*price*policy["fee_bps"]/10000,
                 reserved_risk_usdc=qty*cost_risk,price_risk_usdc=qty*distance,reserved_notional_usdc=qty*price,
                 next_candle_ms=candle.time_ms,mfe_r=0.0,mae_r=0.0,quality="TRACKING")


def _evaluate(order, candle, policy, now_ms):
    side = 1 if order["side"] == "LONG" else -1
    entry, stop, target = order["fill_price"],order["stop"],order["target"]
    risk = abs(entry-stop)
    favorable = candle.high-entry if side == 1 else entry-candle.low
    adverse = entry-candle.low if side == 1 else candle.high-entry
    order["mfe_r"] = max(order["mfe_r"],favorable/risk,0)
    order["mae_r"] = max(order["mae_r"],adverse/risk,0)
    order.update(next_candle_ms=candle.time_ms+STEP,last_closed_candle_ms=candle.time_ms,
                 last_price=candle.close,quality="TRACKING")
    gap_stop = candle.open <= stop if side == 1 else candle.open >= stop
    gap_target = candle.open >= target if side == 1 else candle.open <= target
    stop_hit = candle.low <= stop if side == 1 else candle.high >= stop
    target_hit = candle.high >= target if side == 1 else candle.low <= target
    if stop_hit and target_hit:
        order.update(status="AMBIGUOUS",reason="TP_SL_SAME_CANDLE",exit_candle_ms=candle.time_ms,
                     outcome_observed_ms=now_ms,quality="AMBIGUOUS")
        return
    if not (gap_stop or gap_target or stop_hit or target_hit):
        return
    status = "SL" if gap_stop or (stop_hit and not gap_target) else "TP"
    trigger = candle.open if gap_stop or gap_target else stop if status == "SL" else target
    exit_price = trigger*(1-side*policy["slippage_bps"]/10000)
    gross = side*(exit_price-entry)*order["quantity"]
    exit_fee = order["quantity"]*exit_price*policy["fee_bps"]/10000
    net = gross-order["entry_fee_usdc"]-exit_fee
    order.update(status=status,exit_candle_ms=candle.time_ms,outcome_observed_ms=now_ms,
                 exit_price=exit_price,gross_pnl_usdc=gross,exit_fee_usdc=exit_fee,
                 net_pnl_usdc=net,net_r=net/order["reserved_risk_usdc"],reason=None)


def _advance(conn, order, candles, state, now_ms):
    if order["status"] not in {"PENDING","OPEN"}:
        return
    policy = state["policy"]
    if order["status"] == "PENDING":
        if state["paused"] and state["pause_ms"] <= order["execute_ms"]:
            order.update(status="CANCELLED",reason="ACCOUNT_PAUSED")
            return
        if now_ms > order["expires_ms"]:
            order.update(status="EXPIRED",reason="FILL_OBSERVATION_TIMEOUT")
            return
    # Closed revisions must agree; the forming candle contributes only its known open.
    by_time = {}
    expected = order["execute_ms"] if order["status"] == "PENDING" else order["next_candle_ms"]
    for c in sorted(candles,key=lambda c:c.time_ms):
        if c.time_ms > now_ms or c.time_ms < expected:
            continue
        if not _valid_candle(c,order["contract_id"]):
            order.update(quality="DATA_ERROR",reason="INVALID_CANDLE")
            return
        old = by_time.get(c.time_ms)
        if old is not None and c.time_ms+STEP <= now_ms and (old.open,old.high,old.low,old.close)!=(c.open,c.high,c.low,c.close):
            order.update(quality="DATA_ERROR",reason="CONFLICTING_CANDLES")
            return
        by_time[c.time_ms] = c
    if order["status"] == "PENDING":
        c = by_time.get(order["execute_ms"])
        if c is None:
            order["quality"] = "WAITING_OPEN" if now_ms < order["execute_ms"] else "MISSING_FILL_CANDLE"
            return
        _fill(order,c,policy,now_ms)
        # A delayed observation can fill and settle in the same cycle. Preserve OPEN
        # before evaluating any closed foot; both writes share the caller's transaction.
        _write(conn,order,now_ms)
    if order["status"] != "OPEN":
        return
    while order["next_candle_ms"]+STEP <= now_ms:
        c = by_time.get(order["next_candle_ms"])
        if c is None:
            order.update(quality="HISTORY_GAP",reason="MISSING_CLOSED_CANDLE")
            return
        _evaluate(order,c,policy,now_ms)
        if order["status"] != "OPEN":
            return
    order.update(quality="TRACKING",reason=None)


def _intent(row, state, items, now_ms, snapshot_age_ms):
    identity = setup_identity(row)
    if identity is None or row.get("setup_id") != identity:
        return None
    policy = state["policy"]
    order = dict(order_id="current:"+identity,setup_id=identity,ticker=row["ticker"],
                 side=row.get("direction"),contract_id=str(row.get("contract_id") or ""),
                 created_ms=now_ms,status="REJECTED",reason=None,quality="NOT_FILLED",
                 source_candle_ms=row.get("latest_15m_time_ms"),source_entry=number(row.get("entry_reference")),
                 stop=number(row.get("stop_loss")),target=number(row.get("take_profit")),
                 step_size=number(row.get("step_size")),min_order_size=number(row.get("min_order_size")))
    if state["paused"]:
        order["reason"] = "ACCOUNT_PAUSED"
        return order
    if any(o.get("quality") in {"HISTORY_GAP","DATA_ERROR","MISSING_FILL_CANDLE"} for o in items if o["status"] in ACTIVE):
        order["reason"] = "ACTIVE_POSITION_DATA_INCOMPLETE"
        return order
    source = number(order["source_candle_ms"])
    if (source is None or source % STEP or not 0 < now_ms-source-STEP < policy["freshness_ms"]
            or snapshot_age_ms is None or not 0 <= snapshot_age_ms < policy["freshness_ms"]):
        order["reason"] = "STALE_OR_UNKNOWN_DATA"
        return order
    if not order["contract_id"] or not order["step_size"] or order["step_size"]<=0 or not order["min_order_size"] or order["min_order_size"]<=0:
        order["reason"] = "INVALID_CONTRACT_SIZE_RULES"
        return order
    entry,stop,target = order["source_entry"],order["stop"],order["target"]
    valid = all(x is not None and x > 0 for x in (entry,stop,target))
    valid = valid and (stop < entry < target if order["side"] == "LONG" else target < entry < stop if order["side"] == "SHORT" else False)
    if not valid or abs(target-entry)/abs(entry-stop) < policy["min_rr"]:
        order["reason"] = "INVALID_READY_LEVELS"
        return order
    active = [o for o in items if o["status"] in ACTIVE]
    if any(o["ticker"] == order["ticker"] for o in active):
        order["reason"] = "TICKER_ALREADY_OPEN"
        return order
    if len(active) >= policy["max_positions"]:
        order["reason"] = "MAX_POSITIONS"
        return order
    cash = cash_view(state,items,now_ms)["known_cash_usdc"]
    risk_budget = max(0,cash)*policy["risk_pct"]/100
    remaining_risk = max(0,cash)*policy["max_total_risk_pct"]/100-sum(outstanding_risk(o) for o in active)
    remaining_notional = max(0,cash)*policy["max_notional_multiple"]-sum(o["reserved_notional_usdc"] for o in active)
    if risk_budget <= 0 or remaining_risk+1e-8 < risk_budget or remaining_notional <= 0:
        order["reason"] = "RISK_OR_NOTIONAL_LIMIT"
        return order
    side = 1 if order["side"] == "LONG" else -1
    reference = entry*(1+side*policy["slippage_bps"]/10000)
    if not (stop < reference < target if side == 1 else target < reference < stop):
        order["reason"] = "INVALID_REFERENCE_FILL"
        return order
    distance = stop_cost_per_unit(reference,stop,side,policy)
    maximum = number(row.get("max_order_size"))
    if maximum is not None and maximum <= 0:
        order["reason"] = "INVALID_CONTRACT_SIZE_RULES"
        return order
    qty = floor_size(min(risk_budget/distance,remaining_notional/(reference*(1+2*policy["fee_bps"]/10000)),maximum if maximum is not None else math.inf),order["step_size"])
    if qty <= 0 or qty < order["min_order_size"]:
        order["reason"] = "SIZE_TOO_SMALL"
        return order
    order.update(status="PENDING",reason=None,quality="WAITING_OPEN",quantity=qty,
                 reserved_risk_usdc=qty*distance,reserved_notional_usdc=qty*reference,
                 execute_ms=(now_ms//STEP+1)*STEP,expires_ms=now_ms+policy["pending_ttl_ms"])
    return order


def cycle(conn, *, rows, candles_by_ticker, now_ms, snapshot_age_ms):
    """Caller holds BEGIN IMMEDIATE; account and orders commit atomically."""
    state, items = account(conn), orders(conn)
    for o in items:
        _advance(conn,o,candles_by_ticker.get(o["ticker"],[]),state,now_ms)
        if o["status"] == "AMBIGUOUS":
            _pause(state,"AMBIGUOUS_ACCOUNT",now_ms)
        _write(conn,o,now_ms)
    cash = cash_view(state,items,now_ms)
    if cash["daily_cash_change_usdc"] <= -max(0,cash["daily_start_cash_usdc"])*state["policy"]["daily_loss_pct"]/100:
        _pause(state,"DAILY_LOSS_LIMIT",now_ms)
    if cash["known_cash_usdc"] <= 0:
        _pause(state,"CAPITAL_EXHAUSTED",now_ms)
    for o in items:
        if o["status"] == "PENDING" and state["paused"] and state["pause_ms"] <= o["execute_ms"]:
            o.update(status="CANCELLED",reason="ACCOUNT_PAUSED")
            _write(conn,o,now_ms)
    seen = {o["order_id"] for o in items}
    for row in sorted(rows,key=lambda r:(-float(r.get("score") or 0),str(r.get("ticker") or ""))):
        if row.get("stage") != "READY":
            continue
        identity = setup_identity(row)
        if identity is None or "current:"+identity in seen:
            continue
        order = _intent(row,state,items,now_ms,snapshot_age_ms)
        if order is not None:
            _write(conn,order,now_ms)
            items.append(order)
            seen.add(order["order_id"])
    state.update(last_cycle_ms=now_ms)
    _save_account(conn,state)


def record_error(conn, *, now_ms, error_type):
    state = account(conn)
    _pause(state,"ENGINE_ERROR",now_ms)
    state["last_error"] = dict(time_ms=now_ms,type=error_type)
    _save_account(conn,state)


def set_pause(conn, *, paused, now_ms):
    state, items = account(conn), orders(conn)
    cash = cash_view(state,items,now_ms)
    if not paused:
        if any(o["status"] == "AMBIGUOUS" for o in items):
            raise ValueError("Ambiguous account cannot resume")
        if cash["known_cash_usdc"] <= 0 or cash["daily_cash_change_usdc"] <= -max(0,cash["daily_start_cash_usdc"])*state["policy"]["daily_loss_pct"]/100:
            raise ValueError("Loss limit is still active")
    state.update(paused=paused,pause_reason="OPERATOR_STOP" if paused else None,pause_ms=now_ms if paused else None)
    if not paused:
        state["last_error"] = None
    for o in items:
        if paused and o["status"] == "PENDING" and o["execute_ms"] >= now_ms:
            o.update(status="CANCELLED",reason="OPERATOR_STOP")
            _write(conn,o,now_ms,origin="OPERATOR_CONTROL")
    _save_account(conn,state)


def event_report(conn, *, after_id=0, order_id=None, limit=50):
    """Read-only ascending cursor; state snapshots do not count as live transitions."""
    scope = " WHERE order_id=?" if order_id is not None else ""
    args = (order_id,) if order_id is not None else ()
    total = conn.execute("SELECT COUNT(*) FROM simulated_order_events"+scope,args).fetchone()[0]
    origins = dict(conn.execute("SELECT origin,COUNT(*) FROM simulated_order_events"+scope+" GROUP BY origin",args))
    where = " WHERE id>?" + (" AND order_id=?" if order_id is not None else "")
    page = conn.execute("SELECT id,payload FROM simulated_order_events"+where+" ORDER BY id LIMIT ?",
                        (after_id,*args,max(1,min(limit,200))+1)).fetchall()
    has_more = len(page) > max(1,min(limit,200))
    page = page[:max(1,min(limit,200))]
    return dict(mode="PAPER_ONLY", real_orders_enabled=False, eligible_for_live_promotion=False,
                automatic_promotion=False, source="FORWARD_PAPER_EXECUTION_EVENTS",
                event_tracking_started_ms=account(conn)["event_tracking_started_ms"],
                total_count=total, origin_counts=origins, has_more=has_more,
                next_after_id=page[-1][0] if page else after_id,
                events=[dict(json.loads(row[1]), id=row[0]) for row in page])


def report(conn, *, now_ms, limit=50):
    state, items = account(conn), orders(conn)
    cash = cash_view(state,items,now_ms)
    active = [o for o in items if o["status"] in ACTIVE]
    resolved = [o for o in items if o["status"] in {"TP","SL"}]
    unknown = any(o["status"] == "AMBIGUOUS" for o in items)
    age = (now_ms-state["last_cycle_ms"])/1000 if state["last_cycle_ms"] is not None else None
    stale = age is None or age < 0 or age >= 2*STEP/1000
    active_counts = {status: sum(o["status"] == status for o in active)
                     for status in ("PENDING", "OPEN", "AMBIGUOUS")}
    issues = Counter(o["quality"] for o in active
                     if o.get("quality") in {"HISTORY_GAP", "DATA_ERROR", "MISSING_FILL_CANDLE"})
    tracking_state = ("AMBIGUOUS" if unknown else "PAUSED" if state["paused"]
                      else "NOT_STARTED" if age is None else "COLLECTOR_STALE" if stale
                      else "DATA_INCOMPLETE" if issues else "TRACKING" if active_counts["OPEN"]
                      else "WAITING_FILL" if active_counts["PENDING"] else "WAITING_READY")
    return dict(mode="PAPER_ONLY",real_orders_enabled=False,eligible_for_live_promotion=False,
                automatic_promotion=False,source="CURRENT_READY_ONLY",fill_model="NEXT_15M_OPEN",
                costs_model="ASSUMED_FEES_SLIPPAGE_NO_FUNDING",time_ms=now_ms,
                account=dict(**state,**cash,equity_known=not unknown,
                         cash_usdc=None if unknown else cash["known_cash_usdc"],
                         cycle_age_seconds=age,collector_stale=stale,
                         active_positions=len(active),reserved_risk_usdc=sum(outstanding_risk(o) for o in active),
                         reserved_notional_usdc=sum(o["reserved_notional_usdc"] for o in active)),
                tracking=dict(state=tracking_state, active_status_counts=active_counts,
                              data_issue_count=sum(issues.values()), data_issue_counts=dict(issues)),
                metrics=dict(records=len(items),status_counts=dict(Counter(o["status"] for o in items)),
                             resolved=len(resolved),net_pnl_usdc=sum(o["net_pnl_usdc"] for o in resolved),
                             sample_status="INSUFFICIENT SAMPLE" if len(resolved)<20 else "SUFFICIENT SAMPLE"),
                latest=list(reversed(items))[:max(1,min(limit,200))])
