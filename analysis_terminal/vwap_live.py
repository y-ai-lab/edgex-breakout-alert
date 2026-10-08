"""Preregistered, isolated VWAP observation ledger; no alerts or order adapter."""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from analysis_terminal import pending_entry_replay as control
from analysis_terminal import vwap_reclaim_replay as study
from analysis_terminal.replay import complete_window, rule_fingerprint, strategy_parameters
from analysis_terminal.setups import setup_identity

STEP, DAY, WEEK = 900000, 86400000, 604800000
MODELS = ("current_next_open", study.MODEL)
PROTOCOL = Path(__file__).with_name("vwap_live_protocol.json")


def encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def meta(conn):
    return json.loads(conn.execute("SELECT payload FROM vwap_live_meta WHERE id=1").fetchone()[0])


def save_meta(conn, value):
    conn.execute("UPDATE vwap_live_meta SET payload=? WHERE id=1", (encode(value),))


def initialize(conn, *, now_ms):
    conn.execute("CREATE TABLE IF NOT EXISTS vwap_live_meta(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS vwap_live_markets(ticker TEXT PRIMARY KEY,close_ms INTEGER NOT NULL,continuous_from_ms INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS vwap_live_unknown(ticker TEXT NOT NULL,session_ms INTEGER NOT NULL,PRIMARY KEY(ticker,session_ms))")
    conn.execute("CREATE TABLE IF NOT EXISTS vwap_live_seen(signal_key TEXT PRIMARY KEY,reason TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS vwap_live_signals(signal_key TEXT PRIMARY KEY,payload TEXT NOT NULL,created_ms INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS vwap_live_states(signal_key TEXT NOT NULL,close_ms INTEGER NOT NULL,observed_ms INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(signal_key,close_ms))")
    conn.execute("CREATE TABLE IF NOT EXISTS vwap_live_cycles(bucket_ms INTEGER PRIMARY KEY,observed_ms INTEGER NOT NULL,payload TEXT NOT NULL)")
    value = dict(activated_ms=now_ms, capture_start_ms=(now_ms//DAY+1)*DAY,
                 protocol_sha256=hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
                 engine_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                 rule_fingerprint=None, last_success_ms=None, last_error=None)
    conn.execute("INSERT OR IGNORE INTO vwap_live_meta VALUES(1,?)", (encode(value),))


def records(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM vwap_live_signals ORDER BY created_ms,signal_key")]


def save_record(conn, r):
    conn.execute("UPDATE vwap_live_signals SET payload=? WHERE signal_key=?", (encode(r), r["key"]))


def unknown(conn, ticker, stamp):
    for origin in {(stamp-STEP)//DAY*DAY, stamp//DAY*DAY}:
        conn.execute("INSERT OR IGNORE INTO vwap_live_unknown VALUES(?,?)", (ticker, origin))


def closed_window(contract, candles, interval, stamp):
    step = STEP if interval == "MINUTE_15" else 16*STEP
    closed = [c for c in candles if c.time_ms+step <= stamp]
    if any(c.contract_id != contract.contract_id or c.contract_name != contract.contract_name
           or c.interval != interval or c.time_ms % step
           or not all(math.isfinite(v) and v > 0 for v in (c.open,c.high,c.low,c.close))
           or not c.low <= min(c.open,c.close) <= max(c.open,c.close) <= c.high for c in closed):
        return None
    if any(a.time_ms >= b.time_ms for a,b in zip(closed,closed[1:])):
        return None
    return complete_window(closed,len(closed),180,step,stamp+1)


def evaluate_bar(record, candle, state, *, cursor):
    """One immutable future bar; transient validator clock never escapes."""
    r = dict(record)
    if (cursor % STEP or cursor < r["execution_start_ms"]
            or cursor != r["next_candle_ms"]
            or r["created_ms"] != r["signal_candle_ms"]+STEP+1):
        raise ValueError("Invalid prospective execution cursor")
    if r["status"] == "PENDING" and cursor >= r["expires_ms"]:
        r.update(status="EXPIRED", outcome_ms=r["expires_ms"])
        return r
    if r["model"] == study.MODEL:
        original = {k:r[k] for k in ("created_ms", "signal_candle_ms", "expires_ms")}
        # Original evaluator validates a full-study four-bar clock. Evaluate
        # only this cursor bar after enforcing the actual capture/expiry gates.
        adapted = dict(r, created_ms=cursor+1, signal_candle_ms=cursor-STEP,
                       expires_ms=cursor+4*STEP)
        r = study.evaluate(adapted, [candle] if candle else [], lambda _:state,
                           end_ms=cursor+STEP)
        r.update(original)
    else:
        if r["status"] == "PENDING":
            r = control.evaluate(dict(r, created_ms=cursor+1), [candle] if candle else [], {}, end_ms=cursor+STEP)
            r["created_ms"] = record["created_ms"]
        else:
            r = control.evaluate(r, [candle] if candle else [], {}, start_ms=cursor, end_ms=cursor+STEP)
    if r["status"] == "PENDING" and cursor+STEP >= r["expires_ms"]:
        r.update(status="EXPIRED", outcome_ms=r["expires_ms"])
    return r


def advance(conn, snapshots, contracts, *, end_ms):
    by_name = {c.contract_name:c for c in contracts.values()}
    for original in records(conn):
        if original["status"] not in {"PENDING", "OPEN"}:
            continue
        contract = by_name.get(original["ticker"])
        cs = snapshots.get((contract.contract_id, "MINUTE_15"), []) if contract else []
        closed = [c for c in cs if c.time_ms+STEP <= end_ms]
        counts = Counter(c.time_ms for c in closed)
        series = {c.time_ms:c for c in closed if counts[c.time_ms] == 1}
        r, cursor = dict(original), original["next_candle_ms"]
        while cursor+STEP <= end_ms and r["status"] in {"PENDING", "OPEN"}:
            c = series.get(cursor)
            if c is not None and (c.contract_id != contract.contract_id or c.contract_name != r["ticker"]
                    or c.interval != "MINUTE_15" or c.time_ms % STEP
                    or not all(math.isfinite(v) and v > 0 for v in (c.open,c.high,c.low,c.close))
                    or not c.low <= min(c.open,c.close) <= max(c.open,c.close) <= c.high):
                c = None
            observed = conn.execute("SELECT observed_ms,payload FROM vwap_live_states WHERE signal_key=? AND close_ms=?", (r["key"],cursor-STEP)).fetchone()
            state = json.loads(observed[1]) if observed and observed[0] < cursor else None
            r = evaluate_bar(r, c, state, cursor=cursor)
            cursor += STEP
            r["next_candle_ms"] = cursor
        save_record(conn, r)


def cycle(conn, *, contracts, snapshots, analyze, settings, now_ms):
    m, p = meta(conn), json.loads(PROTOCOL.read_text())
    if (m["protocol_sha256"] != hashlib.sha256(PROTOCOL.read_bytes()).hexdigest()
            or m["engine_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            or p["source_strategy_protocol_sha256"] != hashlib.sha256(study.PROTOCOL.read_bytes()).hexdigest()
            or p["production_parameters"] != strategy_parameters(settings)):
        raise ValueError("Live VWAP frozen policy changed")
    for name, digest in p["frozen_dependencies_sha256"].items():
        if hashlib.sha256(PROTOCOL.with_name(name).read_bytes()).hexdigest() != digest:
            raise ValueError("Frozen VWAP dependency changed")
    for name,digest in json.loads(study.PROTOCOL.read_text())["frozen_dependencies_sha256"].items():
        if hashlib.sha256(PROTOCOL.with_name(name).read_bytes()).hexdigest() != digest:
            raise ValueError("Original VWAP frozen dependency changed")
    fingerprint = rule_fingerprint(analyze, settings)
    if m["rule_fingerprint"] not in (None, fingerprint):
        raise ValueError("Do not mix live VWAP analyzer cohorts")
    m["rule_fingerprint"] = fingerprint
    stamp = now_ms//STEP*STEP
    advance(conn, snapshots, contracts, end_ms=stamp)
    if conn.execute("SELECT 1 FROM vwap_live_cycles WHERE bucket_ms=?", (stamp,)).fetchone():
        return  # No repeated bucket is new evidence, or repairs its first state.
    quality, frames = Counter(), {}
    for cid, contract in contracts.items():
        ticker = contract.contract_name
        previous = conn.execute("SELECT close_ms,continuous_from_ms FROM vwap_live_markets WHERE ticker=?", (ticker,)).fetchone()
        w4 = closed_window(contract, snapshots.get((cid,"HOUR_4"),[]), "HOUR_4", stamp)
        w15 = closed_window(contract, snapshots.get((cid,"MINUTE_15"),[]), "MINUTE_15", stamp)
        valid_volume = w15 is not None and all(isinstance(c.volume,(int,float)) and not isinstance(c.volume,bool)
                       and math.isfinite(c.volume) and c.volume >= 0 for c in w15)
        fresh = 0 <= now_ms-stamp < p["rules"]["max_capture_delay_ms"]
        contiguous = previous is not None and previous[0]+STEP == stamp
        if not fresh or not contiguous or w4 is None or w15 is None or not valid_volume:
            unknown(conn, ticker, stamp)
        if w4 is None or w15 is None or not valid_volume:
            quality["INCOMPLETE_INDICATOR_WINDOW"] += 1
            continue
        quality["VALID_INDICATOR_WINDOWS"] += 1
        baseline = not fresh or not contiguous
        continuous_from = stamp if baseline else previous[1]
        conn.execute("INSERT INTO vwap_live_markets VALUES(?,?,?) ON CONFLICT(ticker) DO UPDATE SET close_ms=excluded.close_ms,continuous_from_ms=excluded.continuous_from_ms", (ticker,stamp,continuous_from))
        frames[ticker] = (w4, w15, fresh)
        row = analyze(contract,w4,w15,as_of_ms=stamp+1)
        identity = setup_identity(row)
        current = control.candidate(row,"current_next_open",candle_ms=w15[-1].time_ms)
        if identity and (baseline or stamp < m["capture_start_ms"]):
            conn.execute("INSERT OR IGNORE INTO vwap_live_seen VALUES(?,?)", ("current_next_open:"+identity,"BASELINED_SETUP"))
        candidate, episode, reason = study.candidate(contract,w4,w15)
        if episode:
            key = study.MODEL+f":vwap-reclaim-v1:{ticker}:{episode[0]}:{episode[1]}"
            first = conn.execute("INSERT OR IGNORE INTO vwap_live_seen VALUES(?,?)", (key,reason or "FIRST_RECLAIM")).rowcount
            if not first:
                quality["ALREADY_CONSUMED_SESSION"] += 1
                candidate = None
            elif conn.execute("SELECT 1 FROM vwap_live_unknown WHERE ticker=? AND session_ms=?", (ticker,episode[1])).fetchone():
                quality["UNOBSERVED_SESSION_FIRST_RECLAIM"] += 1
                candidate = None
            elif candidate is None:
                quality[reason] += 1
        elif reason:
            quality[reason] += 1
        if current:
            first = conn.execute("INSERT OR IGNORE INTO vwap_live_seen VALUES(?,?)", (current["key"],"FIRST_QUALIFICATION")).rowcount
            if (not first or baseline or row["breakout_time_ms"]+16*STEP < m["capture_start_ms"]
                    or row["breakout_time_ms"]+16*STEP <= continuous_from):
                current = None
        for r in (current,candidate):
            if r is None or baseline or stamp < m["capture_start_ms"]:
                continue
            r.update(observed_ms=now_ms,execution_start_ms=stamp+STEP,next_candle_ms=stamp+STEP,
                     cohort_start_ms=m["capture_start_ms"]+(stamp-m["capture_start_ms"])//WEEK*WEEK,
                     dataset="LIVE_CAPTURE_HYPOTHETICAL",step_size=contract.step_size,
                     min_order_size=contract.min_order_size,max_order_size=contract.max_order_size)
            conn.execute("INSERT OR IGNORE INTO vwap_live_signals VALUES(?,?,?)", (r["key"],encode(r),r["created_ms"]))
            quality["CAPTURED_"+r["model"]] += 1
    for r in records(conn):
        frame = frames.get(r["ticker"])
        if r["status"] != "PENDING" or frame is None or not frame[2]:
            continue
        w4, w15, _ = frame
        state = study.pending_state(r,w4,w15) if r["model"] == study.MODEL else {}
        conn.execute("INSERT OR IGNORE INTO vwap_live_states VALUES(?,?,?,?)", (r["key"],stamp,now_ms,encode(state)))
    conn.execute("DELETE FROM vwap_live_states WHERE close_ms<?", (stamp-12*STEP,))
    conn.execute("INSERT OR IGNORE INTO vwap_live_cycles VALUES(?,?,?)", (stamp,now_ms,encode(dict(quality=quality,requested_markets=len(contracts),valid_markets=quality["VALID_INDICATOR_WINDOWS"],capture_delay_ms=now_ms-stamp))))
    m.update(last_success_ms=now_ms,last_error=None,last_bucket_ms=stamp,last_cycle=dict(quality))
    save_meta(conn,m)


def record_error(conn, error_type):
    m = meta(conn)
    m["last_error"] = error_type
    save_meta(conn,m)


def review(conn, *, now_ms, limit=50):
    m, rows = meta(conn), records(conn)
    cohorts = []
    for start in range(m["capture_start_ms"],now_ms//STEP*STEP+1,WEEK):
        rs = [r for r in rows if r["cohort_start_ms"] == start]
        metrics = {model:control.metrics([r for r in rs if r["model"] == model]) for model in MODELS}
        portfolios = {model:control.portfolio([dict(r,created_ms=r["observed_ms"]+1) for r in rs if r["model"] == model]) for model in MODELS}
        filled = {model:{r["setup_id"] for r in rs if r["model"] == model and r["filled_ms"] is not None} for model in MODELS}
        # Model-specific identities differ; compare immutable signal events.
        events = {model:{(r["ticker"],r["side"],r["signal_candle_ms"]) for r in rs if r["model"] == model and r["filled_ms"] is not None} for model in MODELS}
        a,b = events[MODELS[0]],events[MODELS[1]]
        cycles = conn.execute("SELECT bucket_ms,payload FROM vwap_live_cycles WHERE bucket_ms>=? AND bucket_ms<?", (start,min(now_ms//STEP*STEP+1,start+WEEK))).fetchall()
        latest = min(now_ms//STEP*STEP,start+WEEK-STEP)
        overdue = {stamp for stamp in range(start,latest+1,STEP) if stamp+120000 <= now_ms} - {c[0] for c in cycles}
        expected = (latest-start)//STEP+1
        quality = Counter()
        evidence = [json.loads(p) for _,p in cycles]
        for item in evidence:quality.update(item["quality"])
        requested = sum(item["requested_markets"] for item in evidence)
        valid = sum(item["valid_markets"] for item in evidence)
        cohorts.append(dict(start_ms=start,end_ms=start+WEEK,period_complete=now_ms>=start+WEEK,
                            metrics=metrics,portfolios=portfolios,
                            stress_metrics={model:study.stressed_metrics([r for r in rs if r["model"]==model]) for model in MODELS},
                            comparison=dict(shared_filled_events=len(a&b),proposal_only_filled_events=len(b-a),current_filled_missing_in_proposal=len(a-b),
                                            net_filled_count_difference=len(filled[study.MODEL])-len(filled[MODELS[0]]),
                                            capped_filled_count_difference=portfolios[study.MODEL]["filled"]-portfolios[MODELS[0]]["filled"],capped_shared_setup_count=None),
                            data_quality_status="OBSERVATION_HISTORY_INCOMPLETE" if overdue else
                                                "BLOCKED_UNCERTAIN_OUTCOMES" if any(metrics[model]["statuses"].get(status,0)
                                                for model in MODELS for status in ("AMBIGUOUS","DATA_GAP")) else "OBSERVATIONS_RECORDED",
                            coverage=dict(expected_buckets=expected,recorded_buckets=len(cycles),overdue_missing_buckets=len(overdue),
                                          pending_current_buckets=expected-len(cycles)-len(overdue),
                                          requested_market_observations=requested,valid_market_observations=valid,
                                          indicator_coverage_pct=100*valid/requested if requested else None,
                                          late_recorded_buckets=sum(item["capture_delay_ms"]>=120000 for item in evidence),quality=dict(quality))))
    continuous = {model:control.portfolio([dict(r,created_ms=r["observed_ms"]+1) for r in rows if r["model"]==model]) for model in MODELS}
    age = None if m["last_success_ms"] is None else now_ms-m["last_success_ms"]
    status = ("PAUSED_ERROR" if m["last_error"] else "WAITING_FOR_CAPTURE_START" if now_ms<m["capture_start_ms"]
              else "WAITING_FOR_FIRST_OBSERVATION" if age is None or m.get("last_bucket_ms",0)<m["capture_start_ms"]
              else "STALE_OBSERVATION" if age<0 or age>2*STEP else "COLLECTING")
    return dict(protocol="vwap_reclaim_live_capture_v1",dataset="LIVE_CAPTURE_HYPOTHETICAL",mode="SHADOW_ONLY",
                real_orders_enabled=False,automatic_promotion=False,eligible_for_live_promotion=False,
                notifications_enabled=False,current_entry_status=False,status=status,meta=m,
                cost_model=json.loads(PROTOCOL.read_text())["cost_model"],
                total_records=len(rows),cohorts=cohorts,latest=list(reversed(rows))[:limit],
                cohort_portfolio_scope="INDEPENDENT_PERIOD_SIMULATION",continuous_portfolios=continuous,
                continuous_capped_filled_count_difference=continuous[study.MODEL]["filled"]-continuous[MODELS[0]]["filled"],
                sample_status="INSUFFICIENT SAMPLE" if not cohorts or any(c["metrics"][study.MODEL]["resolved"]<50 for c in cohorts) else "REVIEW_REQUIRED",
                limitations=json.loads(PROTOCOL.read_text())["limitations"])
