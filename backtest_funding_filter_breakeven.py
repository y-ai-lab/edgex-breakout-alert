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
    if eligible and result.get('r_result') is not None and result.get('exit_time_ms') is not None:
        entry = float(result['entry']); stop = float(result['stop'])
        risk_pct = abs(entry - stop) / entry if entry > 0 else 0.0
        if risk_pct > 0:
            total_rate = funding_sum(history, entry_ms, int(result['exit_time_ms']))
            signed_return = (-total_rate) if direction == 'LONG' else total_rate
            # Full-size exposure through exit: deliberately conservative/simple first-pass model.
            funding_r = signed_return / risk_pct
            result['funding_r'] = funding_r
            result['r_result'] = float(result['r_result']) + funding_r
    return result


def compact_funding_aligned(trades, starting_equity, risk_fraction):
    rows = list(trades)
    eligible = [x for x in rows if x.get('funding_aligned') is True]
    out = _base_compact(eligible, starting_equity, risk_fraction)
    out['signals_before_funding_filter'] = len(rows)
    out['signals_filtered_out'] = len(rows) - len(eligible)
    out['funding_data_missing'] = sum(1 for x in rows if x.get('funding_entry_rate') is None)
    out['funding_r_total'] = sum(float(x.get('funding_r') or 0.0) for x in eligible)
    out['filter_rule'] = 'LONG only when prior settled funding <= 0; SHORT only when prior settled funding >= 0'
    return out


if __name__ == '__main__':
    bt.simulate_trade = simulate_trade
    bt.compact_summary = compact_funding_aligned
    bt.main()
