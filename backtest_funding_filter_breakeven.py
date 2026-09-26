#!/usr/bin/env python3
from __future__ import annotations

import bisect
from typing import Any

import backtest_4h_exit as bt
import backtest_breakeven_1r as be

FUNDING_LOOKBACK_MS = 200 * bt.DAY_MS
FUNDING_FORWARD_MS = 2 * bt.DAY_MS
_funding_cache: dict[str, list[tuple[int, float]]] = {}
_base_compact = bt.compact_summary

COST_SCENARIOS = {
    'fee_only_0.076pct': 0.00076,
    'base_0.096pct': 0.00096,
    'conservative_0.176pct': 0.00176,
    'stress_0.300pct': 0.00300,
}
BASE_ROUNDTRIP_COST = COST_SCENARIOS['base_0.096pct']
MAX_BASE_COST_R = 0.20

# 4H trend-strength gate: normalize the existing EMA20/EMA50 separation by
# 4H ATR so it is comparable across BTC, alts, equities and commodities.
# 0.25 means the fast/slow EMA gap must be at least one quarter of a 4H ATR.
MIN_TREND_GAP_ATR = 0.25


def fetch_funding_history(contract_id: str, anchor_ms: int) -> list[tuple[int, float]]:
    if contract_id in _funding_cache:
        return _funding_cache[contract_id]
    begin = anchor_ms - FUNDING_LOOKBACK_MS
    end = anchor_ms + FUNDING_FORWARD_MS
    params = {
        'contractId': contract_id,
        'size': '640',
        'filterSettlementFundingRate': 'true',
        'filterBeginTimeInclusive': str(begin),
        'filterEndTimeExclusive': str(end),
    }
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = ''
    while True:
        page_params = dict(params)
        if offset:
            page_params['offsetData'] = offset
        payload = bt.fetch_json('/api/v2/public/funding/getFundingRatePage', page_params)
        data = payload.get('data') or {}
        page = data.get('dataList') or []
        rows.extend(x for x in page if isinstance(x, dict) and x.get('isSettlement') is True)
        nxt = str(data.get('nextPageOffsetData') or '')
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        offset = nxt
    out: dict[int, float] = {}
    for row in rows:
        try:
            out[int(row['fundingTimestamp'])] = float(row['fundingRate'])
        except (KeyError, TypeError, ValueError):
            pass
    history = sorted(out.items())
    _funding_cache[contract_id] = history
    return history


def previous_funding(history: list[tuple[int, float]], t_ms: int) -> float | None:
    if not history:
        return None
    times = [x[0] for x in history]
    i = bisect.bisect_right(times, t_ms) - 1
    return history[i][1] if i >= 0 else None


def funding_sum(history: list[tuple[int, float]], start_ms: int, end_ms: int) -> float:
    return sum(rate for ts, rate in history if start_ms < ts <= end_ms)


def cost_r_for_roundtrip(result: dict[str, Any], roundtrip_cost: float) -> float:
    entry = float(result['entry'])
    stop = float(result['stop'])
    risk_pct = abs(entry - stop) / entry if entry > 0 else 0.0
    return roundtrip_cost / risk_pct if risk_pct > 0 else 0.0


def simulate_trade(signal, entries, entry_index, end_ms):
    result = be.simulate_trade_breakeven(signal, entries, entry_index, end_ms)
    entry_ms = int(result['entry_time_ms'])
    history = fetch_funding_history(str(result['contract_id']), entry_ms)
    rate = previous_funding(history, entry_ms)
    direction = str(result['direction'])
    funding_aligned = rate is not None and ((direction == 'LONG' and rate <= 0.0) or (direction == 'SHORT' and rate >= 0.0))

    base_cost_r = cost_r_for_roundtrip(result, BASE_ROUNDTRIP_COST)
    cost_efficient = base_cost_r <= MAX_BASE_COST_R

    ema_fast = float(signal.ema_fast or 0.0)
    ema_slow = float(signal.ema_slow or 0.0)
    atr_4h = float(signal.atr_monitor or 0.0)
    trend_gap_atr = abs(ema_fast - ema_slow) / atr_4h if atr_4h > 0 else 0.0
    trend_strong = trend_gap_atr >= MIN_TREND_GAP_ATR

    eligible = funding_aligned and cost_efficient and trend_strong

    result['funding_entry_rate'] = rate
    result['funding_aligned'] = funding_aligned
    result['base_cost_r_pretrade'] = base_cost_r
    result['cost_efficient'] = cost_efficient
    result['trend_gap_atr'] = trend_gap_atr
    result['trend_strong'] = trend_strong
    result['strategy_eligible'] = eligible
    result['price_r_result'] = result.get('r_result')
    result['funding_r'] = 0.0
    result['cost_r_base'] = base_cost_r if eligible else 0.0

    if eligible and result.get('r_result') is not None and result.get('exit_time_ms') is not None:
        entry = float(result['entry']); stop = float(result['stop'])
        risk_pct = abs(entry - stop) / entry if entry > 0 else 0.0
        if risk_pct > 0:
            total_rate = funding_sum(history, entry_ms, int(result['exit_time_ms']))
            signed_return = (-total_rate) if direction == 'LONG' else total_rate
            funding_r = signed_return / risk_pct
            result['funding_r'] = funding_r
            result['r_result'] = float(result['r_result']) + funding_r - base_cost_r
    return result


def summary_for_cost(rows: list[dict[str, Any]], starting_equity: float, risk_fraction: float, roundtrip_cost: float):
    adjusted = []
    for x in rows:
        y = dict(x)
        if y.get('strategy_eligible') is True and y.get('price_r_result') is not None:
            y['r_result'] = float(y['price_r_result']) + float(y.get('funding_r') or 0.0) - cost_r_for_roundtrip(y, roundtrip_cost)
        adjusted.append(y)
    return _base_compact([x for x in adjusted if x.get('strategy_eligible') is True], starting_equity, risk_fraction)


def compact_funding_aligned(trades, starting_equity, risk_fraction):
    rows = list(trades)
    funding_ok = [x for x in rows if x.get('funding_aligned') is True]
    cost_ok = [x for x in funding_ok if x.get('cost_efficient') is True]
    eligible = [x for x in rows if x.get('strategy_eligible') is True]
    out = _base_compact(eligible, starting_equity, risk_fraction)
    out['signals_before_funding_filter'] = len(rows)
    out['signals_filtered_by_funding'] = len(rows) - len(funding_ok)
    out['signals_after_funding_filter'] = len(funding_ok)
    out['signals_filtered_by_cost_r'] = len(funding_ok) - len(cost_ok)
    out['signals_after_cost_r_filter'] = len(cost_ok)
    out['signals_filtered_by_trend_strength'] = len(cost_ok) - len(eligible)
    out['signals_after_trend_strength_filter'] = len(eligible)
    out['funding_data_missing'] = sum(1 for x in rows if x.get('funding_entry_rate') is None)
    out['funding_r_total'] = sum(float(x.get('funding_r') or 0.0) for x in eligible)
    out['cost_r_total_base'] = sum(float(x.get('cost_r_base') or 0.0) for x in eligible)
    out['roundtrip_cost_base_pct'] = BASE_ROUNDTRIP_COST * 100.0
    out['max_base_cost_r'] = MAX_BASE_COST_R
    out['min_trend_gap_atr'] = MIN_TREND_GAP_ATR
    out['cost_sensitivity'] = {
        name: summary_for_cost(rows, starting_equity, risk_fraction, cost)
        for name, cost in COST_SCENARIOS.items()
    }
    out['filter_rule'] = 'Funding aligned, base execution cost <= 0.20R, then abs(EMA20-EMA50) >= 0.25 x 4H ATR.'
    out['cost_model'] = 'Non-VIP taker 0.038% each side plus base slippage 0.010% each side; sensitivity includes fee-only, conservative, and stress round-trip costs.'
    return out


if __name__ == '__main__':
    bt.simulate_trade = simulate_trade
    bt.compact_summary = compact_funding_aligned
    bt.main()
