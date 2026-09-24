#!/usr/bin/env python3
import json, urllib.parse, urllib.request

BASE='https://edgex-prod-v2.edgex.exchange'
CANDIDATES=[
 ('/api/v2/public/funding/getFundingRate', {}),
 ('/api/v2/public/funding/getAllFundingRates', {}),
 ('/api/v2/public/funding/getFundingRatePage', {'size':'10'}),
 ('/api/v2/public/quote/getFundingRate', {}),
]

def get(path, params):
    url=BASE+path
    if params: url += '?' + urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'edgex-funding-probe/1.0'})
    try:
        with urllib.request.urlopen(req,timeout=20) as r:
            body=r.read().decode('utf-8','replace')
            return r.status, body[:4000]
    except Exception as e:
        if hasattr(e,'read'):
            try: return getattr(e,'code',0), e.read().decode('utf-8','replace')[:4000]
            except Exception: pass
        return 0, repr(e)

for path,params in CANDIDATES:
    status,body=get(path,params)
    print('FUNDING_PROBE',json.dumps({'path':path,'status':status,'body':body},ensure_ascii=False),flush=True)
