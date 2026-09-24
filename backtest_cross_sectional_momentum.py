#!/usr/bin/env python3
from __future__ import annotations

import json, math, time, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app import Candle, Contract, EdgeXClient

BASE_URL='https://edgex-prod-v2.edgex.exchange'
DAY=86400000
INTERVAL='HOUR_4'
BAR=4*60*60*1000
PAGE=640
PERIOD_DAYS=180
LOOKBACK=42  # 7 days on 4H bars
SKIP=6      # skip latest 24h to reduce short-term reversal
HOLD=6      # rebalance daily
TOP_N=5
MIN_HISTORY=LOOKBACK+SKIP+2
TAKER_SIDE=0.00038
SLIP_SIDE=0.0005
ROUNDTRIP_COST=2*(TAKER_SIDE+SLIP_SIDE)


def fetch_json(path:str, params:dict[str,str], retries:int=6)->dict[str,Any]:
    url=f'{BASE_URL}{path}?{urllib.parse.urlencode(params)}'; delay=1.0
    for i in range(retries):
        try:
            req=urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'edgex-cs-momentum-backtest/1.0'})
            with urllib.request.urlopen(req,timeout=30) as r: p=json.load(r)
            if not isinstance(p,dict) or p.get('code')!='SUCCESS': raise RuntimeError(str(p))
            return p
        except Exception:
            if i+1>=retries: raise
            time.sleep(delay); delay=min(delay*2,12)
    raise RuntimeError('unreachable')


def fetch_klines(c:Contract, begin:int,end:int)->list[Candle]:
    out={}; cur=begin; chunk=100*DAY
    while cur<end:
        ce=min(cur+chunk,end)
        base={'contractId':c.contract_id,'klineType':INTERVAL,'priceType':'LAST_PRICE','size':str(PAGE),'filterBeginKlineTimeInclusive':str(cur),'filterEndKlineTimeExclusive':str(ce)}
        p=fetch_json('/api/v2/public/quote/getKline',base); d=p.get('data') or {}
        rows=d.get('dataList') or []; off=str(d.get('nextPageOffsetData') or ''); seen=set()
        while True:
            for row in rows:
                x=Candle.from_payload(row,fallback_interval=INTERVAL)
                if x and begin<=x.time_ms<end: out[x.time_ms]=x
            if not off or off in seen: break
            seen.add(off); p=fetch_json('/api/v2/public/quote/getKline',{**base,'offsetData':off}); d=p.get('data') or {}; rows=d.get('dataList') or []; off=str(d.get('nextPageOffsetData') or '')
        cur=ce
    return sorted(out.values(),key=lambda x:x.time_ms)


def max_dd(curve:list[float])->float:
    peak=1.0; dd=0.0
    for x in curve:
        peak=max(peak,x); dd=max(dd,(peak-x)/peak)
    return dd


def main():
    now=int(datetime.now(timezone.utc).timestamp()*1000); end=(now//BAR)*BAR; begin=end-PERIOD_DAYS*DAY; warm=begin-(LOOKBACK+SKIP+5)*BAR
    client=EdgeXClient(); contracts=client.fetch_contracts(); print('contracts',len(contracts),flush=True)
    data={}; failed=[]
    for n,c in enumerate(contracts,1):
        try:
            ks=fetch_klines(c,warm,end)
            if len(ks)>=MIN_HISTORY: data[c.contract_name]={x.time_ms:x for x in ks}
        except Exception as e: failed.append([c.contract_name,str(e)])
        if n%20==0: print('loaded',n,'usable',len(data),'failed',len(failed),flush=True)
    times=sorted({t for m in data.values() for t in m if begin<=t<end})
    rebals=times[::HOLD]
    equity=1.0; curve=[equity]; periods=[]; trades=[]
    for t in rebals:
        scored=[]
        for sym,m in data.items():
            hist=[x for tt,x in sorted(m.items()) if tt<=t]
            if len(hist)<MIN_HISTORY: continue
            cur=hist[-1].close; old=hist[-1-SKIP-LOOKBACK].close; skip=hist[-1-SKIP].close
            if old<=0 or skip<=0: continue
            mom=skip/old-1.0
            rets=[]
            for a,b in zip(hist[-LOOKBACK-1:-1],hist[-LOOKBACK:]):
                if a.close>0: rets.append(b.close/a.close-1)
            if len(rets)<20: continue
            mu=sum(rets)/len(rets); var=sum((r-mu)**2 for r in rets)/max(1,len(rets)-1); vol=math.sqrt(var)
            if vol<=0: continue
            scored.append((mom/vol,sym,cur))
        if len(scored)<2*TOP_N: continue
        scored.sort(); shorts=scored[:TOP_N]; longs=scored[-TOP_N:]
        next_t=t+HOLD*BAR
        pnl=[]
        for side,basket in [('SHORT',shorts),('LONG',longs)]:
            for score,sym,entry in basket:
                future=[x for tt,x in sorted(data[sym].items()) if t<tt<=next_t]
                if not future or entry<=0: continue
                exitp=future[-1].close
                gross=(exitp/entry-1) * (1 if side=='LONG' else -1)
                net=gross-ROUNDTRIP_COST
                pnl.append(net); trades.append({'time':t,'symbol':sym,'side':side,'score':score,'gross_return':gross,'net_return':net})
        if not pnl: continue
        r=sum(pnl)/len(pnl); equity*=1+r; curve.append(equity); periods.append((t,r))
    pos=[r for _,r in periods if r>0]; neg=[r for _,r in periods if r<0]
    gross=sum(pos); loss=-sum(neg); pf=gross/loss if loss>0 else None
    cutoff=end-30*DAY; last=[r for t,r in periods if t>=cutoff]
    result={'strategy':'cross-sectional 7d momentum, skip 24h, daily rebalance, top/bottom 5, vol-normalized','contracts_total':len(contracts),'contracts_usable':len(data),'contracts_failed':len(failed),'periods':len(periods),'position_legs':len(trades),'positive_period_pct':100*len(pos)/len(periods) if periods else 0,'net_return_pct':(equity-1)*100,'profit_factor_period_returns':pf,'max_drawdown_pct':max_dd(curve)*100,'last30_return_pct':(math.prod([1+r for r in last])-1)*100 if last else 0,'cost_assumption_roundtrip_pct':ROUNDTRIP_COST*100,'funding':'NOT_AVAILABLE_IN_THIS_OHLCV_ENDPOINT; excluded and flagged, not assumed zero','errors':failed[:20]}
    print('SUMMARY_CS_MOMENTUM '+json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__': main()
