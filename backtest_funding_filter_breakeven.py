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

# Cost assumptions are expressed as round-trip notional cost.
# Non-VIP taker fee: 0.038% per side => 0.076% round trip.
# Base slippage stress: 0.010% per side => +0.020% round trip.
# We also report fee-only, conservative, and stress scenarios without optimizing thresholds.
COST_SCENARIOS = {
    'fee_only_0.076pct': 0.00076,
    'base_0.096pct': 0.00096,
    'conservative_0.176pct': 0.00176,
    'stress_0.300pct': 0.00300,
}
BASE_ROUNDTRIP_COST = COST_SCENARIOS['base_0.096pct']


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
    eligible = rate is not None and ((direction == 'LONG' and rate <= 0.0) or (direction == 'SHORT' and rate >= 0.0))
    result['funding_entry_rate'] = rate
    result['funding_aligned'] = eligible
    result['price_r_result'] = result.get('r_result')
    result['funding_r'] = 0.0
    result['cost_r_base'] = 0.0
    if eligible and result.get('r_result') is not None and result.get('exit_time_ms') is not None:
        entry = float(result['entry']); stop = float(result['stop'])
        risk_pct = abs(entry - stop) / entry if entry > 0 else 0.0
        if risk_pct > 0:
            total_rate = funding_sum(history, entry_ms, int(result['exit_time_ms']))
            signed_return = (-total_rate) if direction == 'LONG' else total_rate
            funding_r = signed_return / risk_pct
            cost_r = cost_r_for_roundtrip(result, BASE_ROUNDTRIP_COST)
            result['funding_r'] = funding_r
            result['cost_r_base'] = cost_r
            result['r_result'] = float(result['r_result']) + funding_r - cost_r
    return result


def summary_for_cost(rows: list[dict[str, Any]], starting_equity: float, risk_fraction: float, roundtrip_cost: float):
    adjusted = []
    for x in rows:
        y = dict(x)
        if y.get('funding_aligned') is True and y.get('price_r_result') is not None:
            y['r_result'] = float(y['price_r_result']) + float(y.get('funding_r') or 0.0) - cost_r_for_roundtrip(y, roundtrip_cost)
        adjusted.append(y)
    return _base_compact([x for x in adjusted if x.get('funding_aligned') is True], starting_equity, risk_fraction)


def compact_funding_aligned(trades, starting_equity, risk_fraction):
    rows = list(trades)
    eligible = [x for x in rows if x.get('funding_aligned') is True]
    out = _base_compact(eligible, starting_equity, risk_fraction)
    out['signals_before_funding_filter'] = len(rows)
    out['signals_filtered_out'] = len(rows) - len(eligible)
    out['funding_data_missing'] = sum(1 for x in rows if x.get('funding_entry_rate') is None)
    out['funding_r_total'] = sum(float(x.get('funding_r') or 0.0) for x in eligible)
    out['cost_r_total_base'] = sum(float(x.get('cost_r_base') or 0.0) for x in eligible)
    out['roundtrip_cost_base_pct'] = BASE_ROUNDTRIP_COST * 100.0
    out['cost_sensitivity'] = {
        name: summary_for_cost(rows, starting_equity, risk_fraction, cost)
        for name, cost in COST_SCENARIOS.items()
    }
    out['filter_rule'] = 'LONG only when prior settled funding <= 0; SHORT only when prior settled funding >= 0'
    out['cost_model'] = 'Non-VIP taker 0.038% each side plus base slippage 0.010% each side; sensitivity includes fee-only, conservative, and stress round-trip costs.'
    return out


if __name__ == '__main__':
    bt.simulate_trade = simulate_trade
    bt.compact_summary = compact_funding_aligned
    bt.main()
