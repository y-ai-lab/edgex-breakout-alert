"""BTC-only descriptive waves and preregistered SHADOW watch hypotheses.

Nothing in this module authorizes a trade or modifies the production strategy.
"""
from __future__ import annotations

import math
import app as scanner

INTERVALS = ("HOUR_4", "HOUR_1", "MINUTE_15", "MINUTE_5")
STEP = 300_000
MODEL = "btc_confirmed_swing_watch_v1"
MIN_RR = 2.0
PIVOT_RADIUS = 2


def prepare(candles, contract, interval, now_ms):
    step = scanner.INTERVAL_MS[interval]
    found = {}
    for c in candles:
        if not isinstance(c.time_ms, int) or isinstance(c.time_ms, bool) or c.time_ms <= 0 or c.time_ms % step:
            raise ValueError("INVALID_TIME")
        if c.time_ms + step > now_ms:
            continue
        if c.contract_id != contract.contract_id or c.interval != interval:
            raise ValueError("INVALID_IDENTITY")
        if (not all(math.isfinite(x) and x > 0 for x in (c.open,c.high,c.low,c.close))
                or c.low > min(c.open,c.close) or c.high < max(c.open,c.close)):
            raise ValueError("INVALID_OHLC")
        if c.time_ms in found and found[c.time_ms] != c:
            raise ValueError("CONFLICTING_CANDLES")
        found[c.time_ms] = c
    items = sorted(found.values(), key=lambda c: c.time_ms)[-120:]
    if len(items) < 50:
        raise ValueError("INSUFFICIENT_HISTORY")
    if any(b.time_ms-a.time_ms != step for a,b in zip(items,items[1:])):
        raise ValueError("HISTORY_GAP")
    if items[-1].time_ms + step != now_ms//step*step:
        raise ValueError("LATEST_CANDLE_MISSING")
    return items


def pivots(items, interval):
    step = scanner.INTERVAL_MS[interval]
    result = []
    for i in range(PIVOT_RADIUS, len(items)-PIVOT_RADIUS):
        others = items[i-2:i] + items[i+1:i+3]
        high = all(items[i].high > c.high for c in others)
        low = all(items[i].low < c.low for c in others)
        # An outside candle cannot establish the intrabar order of two pivots.
        if high and low:
            continue
        if high or low:
            result.append(dict(kind="HIGH" if high else "LOW", time_ms=items[i].time_ms,
                               price=items[i].high if high else items[i].low,
                               confirmed_ms=items[i+2].time_ms+step))
    return result


def frame(items, interval):
    ps = pivots(items, interval)
    highs, lows = [p for p in ps if p["kind"]=="HIGH"], [p for p in ps if p["kind"]=="LOW"]
    ema20 = scanner._ema([c.close for c in items],20)
    ema50 = scanner._ema([c.close for c in items],50)
    atr = scanner._atr(items,14)
    last = items[-1]
    trend = "UP" if last.close > ema20 > ema50 else "DOWN" if last.close < ema20 < ema50 else "MIXED"
    structure = "UNCONFIRMED"
    if len(highs)>=2 and len(lows)>=2:
        structure = ("UP" if highs[-1]["price"]>highs[-2]["price"] and lows[-1]["price"]>lows[-2]["price"]
                     else "DOWN" if highs[-1]["price"]<highs[-2]["price"] and lows[-1]["price"]<lows[-2]["price"] else "MIXED")
    recent = items[-20:]
    floor, ceiling = min(c.low for c in recent), max(c.high for c in recent)
    legs = [dict(direction="UP" if b["price"]>a["price"] else "DOWN",
                 from_ms=a["time_ms"],to_ms=b["time_ms"],from_price=a["price"],to_price=b["price"],
                 change_pct=100*(b["price"]/a["price"]-1),known_ms=b["confirmed_ms"])
            for a,b in zip(ps,ps[1:]) if a["kind"]!=b["kind"]]
    current_leg = (dict(direction="UP" if last.close>ps[-1]["price"] else "DOWN" if last.close<ps[-1]["price"] else "FLAT",
                        from_ms=ps[-1]["time_ms"],from_price=ps[-1]["price"],to_price=last.close,
                        change_pct=100*(last.close/ps[-1]["price"]-1),confirmed=False) if ps else None)
    return dict(interval=interval, trend=trend, structure=structure, ema20=ema20, ema50=ema50, atr=atr,
                close=last.close, closed_ms=last.time_ms+scanner.INTERVAL_MS[interval],
                support=lows[-1] if lows else None, resistance=highs[-1] if highs else None,
                range_low=floor, range_high=ceiling,
                range_position_pct=100*(last.close-floor)/(ceiling-floor) if ceiling>floor else None,
                legs=legs[-12:],current_leg=current_leg,
                pivots=ps, series=[dict(time_ms=c.time_ms,open=c.open,high=c.high,low=c.low,close=c.close) for c in items])


def watch_plan(mode, side, frames, bars, now_ms):
    fifteen, hour = frames["MINUTE_15"], frames["HOUR_1"]
    high, low, atr = fifteen["resistance"], fifteen["support"], fifteen["atr"]
    plan = dict(model=MODEL, mode=mode, side=side, state="WATCH", reasons=[], trigger=None,
                entry=None, stop=None, target=None, rr=None, signal=None,
                higher_trend=frames["HOUR_4"]["trend"], entry_basis="TRIGGER_LEVEL_ESTIMATE")
    if not high or not low or not atr or high["price"]<=low["price"]:
        plan.update(state="LEVEL_WAIT", reasons=["SWING_LEVELS_UNAVAILABLE"])
        return plan
    long = side=="LONG"
    anchor = (high if long else low) if mode=="BREAKOUT" else (low if long else high)
    trigger = anchor["price"]
    current, previous = bars[-1], bars[-2]
    known_before = max(high["confirmed_ms"],low["confirmed_ms"])<=current.time_ms
    colour = current.close>current.open if long else current.close<current.open
    if mode=="BREAKOUT":
        pattern = previous.close<=trigger<current.close if long else previous.close>=trigger>current.close
        context = hour["trend"] == ("UP" if long else "DOWN")
        stop = low["price"]-.25*atr if long else high["price"]+.25*atr
    else:
        touched = min(previous.low,current.low)<=trigger if long else max(previous.high,current.high)>=trigger
        pattern = touched and (current.close>trigger if long else current.close<trigger)
        context = hour["trend"]=="MIXED"
        stop = trigger-.25*atr if long else trigger+.25*atr
    confirmed = known_before and pattern and colour and context
    entry = current.close if confirmed else trigger
    if mode=="RANGE":
        target = high["price"] if long else low["price"]
    else:
        levels = [p["price"] for key in ("HOUR_1","HOUR_4") for p in frames[key]["pivots"]
                  if p["kind"] == ("HIGH" if long else "LOW") and (p["price"]>entry if long else p["price"]<entry)]
        target = (min(levels) if long else max(levels)) if levels else None
    valid = target is not None and (0<stop<entry<target if long else 0<target<entry<stop)
    rr = abs(target-entry)/abs(entry-stop) if valid else None
    reasons = []
    if not context: reasons.append("ONE_HOUR_CONTEXT_WAIT")
    if not known_before: reasons.append("LEVEL_JUST_CONFIRMED")
    if not (pattern and colour): reasons.append("FIVE_MINUTE_CONFIRMATION_WAIT")
    if target is None: reasons.append("STRUCTURAL_TARGET_UNAVAILABLE")
    elif not valid: reasons.append("INVALID_STRUCTURE")
    elif rr<MIN_RR: reasons.append("RR_BELOW_2")
    state = "TRIGGERED_SHADOW" if confirmed and valid and rr>=MIN_RR else "WATCH"
    if state=="TRIGGERED_SHADOW" and now_ms-(current.time_ms+STEP)>=120_000:
        state="LATE_SHADOW"
    plan.update(state=state,reasons=reasons,trigger=trigger,entry=entry,stop=stop,target=target,rr=rr,
                anchor=anchor,context_aligned=context,
                entry_basis="SIGNAL_CLOSE_REFERENCE" if confirmed else "TRIGGER_LEVEL_ESTIMATE")
    if state in {"TRIGGERED_SHADOW","LATE_SHADOW"}:
        key=f"btc-wave-v1:{mode}:{side}:{high['time_ms']}:{low['time_ms']}"
        plan["signal"] = dict(key=key,setup_id=key,ticker="BTCUSDC",model=MODEL,mode=mode,side=side,
                              entry=entry,stop=stop,target=target,rr=rr,signal_candle_ms=current.time_ms,
                              created_ms=current.time_ms+STEP+1,detected_ms=now_ms,
                              detection_lag_ms=now_ms-current.time_ms-STEP,anchor=anchor,
                              hypothetical_entry=True,real_orders_enabled=False)
    return plan


def analyze(contract, data, *, now_ms):
    if contract.contract_name != "BTCUSDC":
        raise ValueError("BTC_ONLY")
    frames, prepared, errors = {}, {}, {}
    for interval in INTERVALS:
        try:
            prepared[interval] = prepare(data.get(interval,[]),contract,interval,now_ms)
            frames[interval] = frame(prepared[interval],interval)
        except ValueError as exc:
            errors[interval]=str(exc)
    plans = [watch_plan(mode,side,frames,prepared["MINUTE_5"],now_ms)
             for mode in ("BREAKOUT","RANGE") for side in ("LONG","SHORT")] if not errors else []
    return dict(model=MODEL,ticker="BTCUSDC",observed_ms=now_ms,status="DATA_WAIT" if errors else "OBSERVING",
                mode="SHADOW_ONLY",real_orders_enabled=False,changes_live_rules=False,
                eligible_for_live_promotion=False,automatic_promotion=False,min_rr=MIN_RR,
                pivot_confirmation_bars=2,frames=frames,errors=errors,plans=plans,
                hypotheses=["BREAKOUT_LONG","BREAKOUT_SHORT","RANGE_LONG","RANGE_SHORT"],
                signal_count=sum(p["signal"] is not None for p in plans),
                fresh_trigger_count=sum(p["state"]=="TRIGGERED_SHADOW" for p in plans))


def compact(report):
    """Persist wave observations, not four full candle histories every five minutes."""
    return {**report,"frames":{k:{**{n:v for n,v in f.items() if n not in {"series","pivots"}},
                                 "pivots":f["pivots"][-12:]} for k,f in report["frames"].items()}}
