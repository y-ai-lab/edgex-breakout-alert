#!/usr/bin/env python3
import json, time, urllib.parse, urllib.request

BASE='https://edgex-prod-v2.edgex.exchange'
CONTRACTS=['10000001','10000002']
END=int(time.time()*1000)
BEGIN=END-30*24*60*60*1000
CANDIDATES=[
 ('/api/v2/public/funding/getLatestFundingRate', {'contractId':CONTRACTS}),
 ('/api/v2/public/funding/getFundingRatePage', {'contractId':CONTRACTS[0],'size':'100','filterSettlementFundingRate':'true','filterBeginTimeInclusive':str(BEGIN),'filterEndTimeExclusive':str(END)}),
]

def get(path, params):
    url=BASE+path
    if params:
        url += '?' + urllib.parse.urlencode(params, doseq=True)
    req=urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'edgex-funding-probe/2.0'})
    try:
        with urllib.request.urlopen(req,timeout=30) as r:
            body=r.read().decode('utf-8','replace')
            return r.status, body[:16000]
    except Exception as e:
        if hasattr(e,'read'):
            try: return getattr(e,'code',0), e.read().decode('utf-8','replace')[:16000]
            except Exception: pass
        return 0, repr(e)

for path,params in CANDIDATES:
    status,body=get(path,params)
    print('FUNDING_PROBE',json.dumps({'path':path,'params':params,'status':status,'body':body},ensure_ascii=False),flush=True)
