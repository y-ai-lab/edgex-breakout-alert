#!/usr/bin/env python3
from __future__ import annotations

import backtest_4h_exit as bt


def simulate_trade_breakeven(signal, entries, entry_index, end_ms):
    entry = signal.candle.close
    initial_stop = float(signal.stop_loss_override)
    stop = initial_stop
    target = float(signal.take_profit_override)
    rr = float(signal.rr)
    risk = abs(entry - initial_stop)
    one_r = entry + risk if signal.direction == "up" else entry - risk
    split = len(signal.split_targets) >= 2
    tp1 = signal.split_targets[0][1] if split else None
    tp1_done = False
    breakeven_active = False
    last_close = entry

    result = {
        "setup_key": signal.key,
        "contract_id": signal.contract.contract_id,
        "symbol": signal.contract.contract_name,
        "direction": "LONG" if signal.direction == "up" else "SHORT",
        "entry_time_ms": signal.candle.time_ms + bt.ENTRY_MS,
        "breakout_time_ms": signal.breakout_time_ms,
        "entry": entry,
        "stop": initial_stop,
        "target": target,
        "rr_planned": rr,
        "split": split,
        "roll_level": signal.breakout_level,
        "atr_15m": signal.atr_entry,
        "atr_4h": signal.atr_monitor,
        "exit_time_ms": None,
        "exit_reason": "OPEN",
        "r_result": None,
        "mark_r": 0.0,
        "breakeven_triggered": False,
    }

    for candle in entries[entry_index + 1:]:
        if candle.time_ms >= end_ms:
            break
        last_close = candle.close

        # Conservative intrabar rule: a 1R touch activates the BE stop only
        # from the NEXT 15m candle. This avoids assuming an unknown OHLC path.
        stop_hit = bt.hit(candle, stop, signal.direction, "stop")
        one_r_hit = bt.hit(candle, one_r, signal.direction, "target")

        if not split:
            target_hit = bt.hit(candle, target, signal.direction, "target")
            if stop_hit:
                r_out = 0.0 if breakeven_active else -1.0
                result.update(exit_time_ms=candle.time_ms + bt.ENTRY_MS,
                              exit_reason="BE" if breakeven_active else ("SL" if not target_hit else "SL_BOTH_HIT"),
                              r_result=r_out)
                return result
            if target_hit:
                result.update(exit_time_ms=candle.time_ms + bt.ENTRY_MS, exit_reason="TP", r_result=rr)
                return result
            if one_r_hit and not breakeven_active:
                breakeven_active = True; stop = entry; result["breakeven_triggered"] = True
            continue

        assert tp1 is not None
        if not tp1_done:
            tp1_hit = bt.hit(candle, tp1, signal.direction, "target")
            final_hit = bt.hit(candle, target, signal.direction, "target")
            if stop_hit:
                r_out = 0.0 if breakeven_active else -1.0
                result.update(exit_time_ms=candle.time_ms + bt.ENTRY_MS,
                              exit_reason="BE" if breakeven_active else ("SL" if not (tp1_hit or final_hit) else "SL_BOTH_HIT"),
                              r_result=r_out)
                return result
            if final_hit:
                result.update(exit_time_ms=candle.time_ms + bt.ENTRY_MS, exit_reason="TP1_TP2_SAME_CANDLE", r_result=1.0 + 0.5 * rr)
                return result
            if tp1_hit:
                tp1_done = True
            if one_r_hit and not breakeven_active:
                breakeven_active = True; stop = entry; result["breakeven_triggered"] = True
            continue

        final_hit = bt.hit(candle, target, signal.direction, "target")
        if stop_hit:
            # After TP1, half was realized at +1R; the remaining half exits at BE.
            result.update(exit_time_ms=candle.time_ms + bt.ENTRY_MS,
                          exit_reason="TP1_THEN_BE" if breakeven_active else "TP1_THEN_SL",
                          r_result=0.5 if breakeven_active else 0.5)
            return result
        if final_hit:
            result.update(exit_time_ms=candle.time_ms + bt.ENTRY_MS, exit_reason="TP1_THEN_TP2", r_result=1.0 + 0.5 * rr)
            return result
        if one_r_hit and not breakeven_active:
            breakeven_active = True; stop = entry; result["breakeven_triggered"] = True

    mark = bt.mark_r(signal.direction, entry, initial_stop, last_close)
    if split and tp1_done:
        mark = 1.0 + 0.5 * mark
    result["mark_r"] = mark
    return result


if __name__ == "__main__":
    bt.simulate_trade = simulate_trade_breakeven
    bt.main()
