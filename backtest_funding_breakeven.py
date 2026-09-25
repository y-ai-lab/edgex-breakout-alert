#!/usr/bin/env python3
from __future__ import annotations

import json, urllib.parse, urllib.request
from typing import Any

import backtest_4h_exit as bt
import backtest_breakeven_1r as be

# Structural test, not threshold optimization.
# Compare baseline BE, funding cashflow, and a simple adverse-funding filter.
ADVERSE_FUNDING_THRESHOLD = 0.0005  # 5 bps per settlement; deliberately coarse


def funding_rows(contract_id: str, begin_ms: int, end_ms: int) -> list[dict[str, Any]]:
    params = {
        'contractId': contract_id,
        'size': '640',
        'filterSettlementFundingRate': 'true',
        'filterBeginTimeInclusive': str(begin_ms),
        'filterEndTimeExclusive': str(end_ms),
    }
    out=[]; seen=set(); offset=''
    while True:
        q=dict(params)
        if offset: q['offsetData']=offset
        p=bt.fetch_json('/api/v2/public/funding/getFundingRatePage', q)
        d=p.get('data') or {}
        for row in d.get('dataList') or []:
            if isinstance(row,dict): out.append(row)
        nxt=str(d.get('nextPageOffsetData') or '')
        if not nxt or nxt in seen: break
        seen.add(nxt); offset=nxt
    return out


def row_time(row:dict[str,Any])->int|None:
    for k in ('fundingTime','settlementTime','time','createdTime','fundingTimestamp'):
        try:
            if row.get(k) is not None: return int(row[k])
        except Exception: pass
    return None


def row_rate(row:dict[str,Any])->float|None:
    for k in ('fundingRate','settlementFundingRate','rate'):
        try:
            if row.get(k) is not None: return float(row[k])
        except Exception: pass
    return None


def funding_map(contract_id:str, begin_ms:int, end_ms:int):
    vals=[]
    for row in funding_rows(contract_id,begin_ms,end_ms):
        t=row_time(row); r=row_rate(row)
        if t is not None and r is not None: vals.append((t,r))
    vals.sort()
    return vals


def latest_rate_before(vals:list[tuple[int,float]], t:int)->float|None:
    ans=None
    for ts,r in vals:
        if ts>t: break
        ans=r
    return ans


def funding_cashflow_r(trade:dict[str,Any], vals:list[tuple[int,float]])->float:
    if trade.get('exit_time_ms') is None: return 0.0
    entry=float(trade['entry']); stop=float(trade['stop']); risk=abs(entry-stop)
    if entry<=0 or risk<=0: return 0.0
    direction=1.0 if trade['direction']=='LONG' else -1.0
    # Positive funding: longs pay shorts. Convert price-notional funding to R.
    total=0.0
    for ts,rate in vals:
        if trade['entry_time_ms'] < ts <= trade['exit_time_ms']:
            total += -direction * rate * entry / risk
    return total


def main():
    original_backtest_contract=bt.backtest_contract

    def wrapped(contract, settings, period_start_ms, period_end_ms, holdout_start_ms):
        full, pre, cov=original_backtest_contract(contract,settings,period_start_ms,period_end_ms,holdout_start_ms)
        try: vals=funding_map(contract.contract_id,period_start_ms-7*bt.DAY_MS,period_end_ms)
        except Exception as exc:
            cov['funding_error']=f'{type(exc).__name__}: {exc}'; vals=[]
        for rows in (full,pre):
            for tr in rows:
                rate=latest_rate_before(vals,int(tr['entry_time_ms']))
                tr['entry_funding_rate']=rate
                tr['funding_r']=funding_cashflow_r(tr,vals)
                if tr.get('r_result') is not None:
                    tr['r_result_raw']=tr['r_result']
                    tr['r_result']=float(tr['r_result'])+float(tr['funding_r'])
                adverse=(tr['direction']=='LONG' and rate is not None and rate>ADVERSE_FUNDING_THRESHOLD) or (tr['direction']=='SHORT' and rate is not None and rate < -ADVERSE_FUNDING_THRESHOLD)
                tr['adverse_funding']=adverse
        cov['funding_points']=len(vals)
        return full,pre,cov

    bt.simulate_trade=be.simulate_trade_breakeven
    bt.backtest_contract=wrapped
    bt.main()

if __name__=='__main__': main()
