"""Isolated SQLite Shadow ledger for BTC hypotheses; no main signal writes."""
import json
import math
from statistics import mean

from analysis_terminal.btc_wave import STEP, compact
from analysis_terminal.outcomes import evaluate_paper_signal, verified_result
from analysis_terminal.outcome_history import consecutive_window
from analysis_terminal.net_costs import metrics as cost_metrics, projection as cost_projection


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS btc_wave_observations (
        bucket_ms INTEGER PRIMARY KEY, payload TEXT NOT NULL, observed_ms INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS btc_wave_signals (
        signal_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_ms INTEGER NOT NULL)""")


def records(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM btc_wave_signals ORDER BY created_ms,signal_key")]


def save(conn, report):
    now=report["observed_ms"]
    conn.execute("INSERT INTO btc_wave_observations VALUES (?,?,?) ON CONFLICT(bucket_ms) DO UPDATE SET payload=excluded.payload,observed_ms=excluded.observed_ms",
                 (now//STEP*STEP,json.dumps(compact(report),allow_nan=False),now))
    conn.execute("DELETE FROM btc_wave_observations WHERE bucket_ms<?",(now-7*86400000,))
    for plan in report["plans"]:
        signal=plan["signal"]
        if signal:
            conn.execute("INSERT OR IGNORE INTO btc_wave_signals VALUES (?,?,?)",
                         (signal["key"],json.dumps(signal,allow_nan=False),signal["created_ms"]))


def update(conn, signal, candles, contract, *, now_ms):
    for c in candles:
        if c.time_ms+STEP<=now_ms and (not all(math.isfinite(v) and v>0 for v in (c.open,c.high,c.low,c.close))
                                      or c.low>min(c.open,c.close) or c.high<max(c.open,c.close)):
            raise ValueError("INVALID_OUTCOME_OHLC")
    prefix,missing,_=consecutive_window(signal,candles,contract,"MINUTE_5",now_ms=now_ms)
    result=evaluate_paper_signal(signal,prefix,interval_ms=STEP,now_ms=now_ms)
    if result["status"] in {"TP","SL","AMBIGUOUS"}:
        missing=None
    result["quality"]="HISTORY_GAP" if missing is not None else "COMPLETE" if prefix else "WAITING_NEXT_CANDLE"
    result["next_missing_ms"]=missing
    result["net_r"]=None
    if verified_result(result) and result["status"] in {"TP","SL"}:
        exit_price=signal["target"] if result["status"]=="TP" else signal["stop"]
        # Fixed, explicitly hypothetical 5bps fee + 2bps slippage, each side.
        result["net_r"]=result["final_r"]-(signal["entry"]+exit_price)*.0007/abs(signal["entry"]-signal["stop"])
    conn.execute("UPDATE btc_wave_signals SET payload=? WHERE signal_key=?",
                 (json.dumps(dict(signal,result=result),allow_nan=False),signal["key"]))


def mark_error(conn, signal):
    result=dict(signal.get("result") or {},quality="DATA_ERROR")
    conn.execute("UPDATE btc_wave_signals SET payload=? WHERE signal_key=?",
                 (json.dumps(dict(signal,result=result),allow_nan=False),signal["key"]))


def metrics(items):
    resolved=[s for s in items if verified_result(s.get("result")) and s["result"]["status"] in {"TP","SL"}
              and s["result"].get("net_r") is not None]
    rs=[s["result"]["net_r"] for s in resolved]
    wins=sum(s["result"]["status"]=="TP" for s in resolved)
    profit,loss=sum(max(r,0) for r in rs),-sum(min(r,0) for r in rs)
    ordered=sorted(resolved,key=lambda s:(s["result"]["outcome_time_ms"],s["key"]))
    streak=maximum=0
    for s in ordered:
        streak=streak+1 if s["result"]["net_r"]<0 else 0;maximum=max(maximum,streak)
    return dict(signals=len(items),resolved=len(resolved),tp=wins,sl=len(resolved)-wins,
                open=sum((s.get("result") or {}).get("status","OPEN")=="OPEN" for s in items),
                ambiguous=sum((s.get("result") or {}).get("status")=="AMBIGUOUS" for s in items),
                history_gaps=sum((s.get("result") or {}).get("quality")=="HISTORY_GAP" for s in items),
                data_errors=sum((s.get("result") or {}).get("quality")=="DATA_ERROR" for s in items),
                win_rate=100*wins/len(resolved) if resolved else None,
                avg_gross_r=mean(s["result"]["final_r"] for s in resolved) if resolved else None,
                avg_net_r=mean(rs) if rs else None,expectancy_r=mean(rs) if rs else None,
                profit_factor=profit/loss if loss else "INF" if profit else None,
                avg_mfe_r=mean(s["result"]["mfe_r"] for s in resolved) if resolved else None,
                avg_mae_r=mean(s["result"]["mae_r"] for s in resolved) if resolved else None,
                max_consecutive_losses=maximum if resolved else None,
                sample_status="INSUFFICIENT SAMPLE" if len(resolved)<20 else "REVIEW_REQUIRED",
                cost_model="HYPOTHETICAL_TOUCH_5BPS_FEE_2BPS_SLIPPAGE_EACH_SIDE",
                real_execution_results=False)


def report(conn, *, now_ms, latest=None):
    items=records(conn)
    if latest is None:
        row=conn.execute("SELECT payload FROM btc_wave_observations ORDER BY bucket_ms DESC LIMIT 1").fetchone()
        latest=json.loads(row[0]) if row else None
    observations=conn.execute("SELECT count(*) FROM btc_wave_observations").fetchone()[0]
    age=(now_ms-latest["observed_ms"])/1000 if latest else None
    stale=age is None or age<0 or age>=120
    return dict(ticker="BTCUSDC",mode="SHADOW_ONLY",real_orders_enabled=False,automatic_promotion=False,
                eligible_for_live_promotion=False,changes_live_rules=False,now_ms=now_ms,
                snapshot_age_seconds=age,stale=stale,latest=latest,
                recorded_observations=observations,metrics=metrics(items),
                cost_adjusted=cost_metrics(items),
                groups={mode+"_"+side:metrics([s for s in items if s["mode"]==mode and s["side"]==side])
                        for mode in ("BREAKOUT","RANGE") for side in ("LONG","SHORT")},
                signals=[dict(s,cost_projection=cost_projection(s)) for s in reversed(items)][:50])
