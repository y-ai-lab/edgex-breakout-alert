#!/usr/bin/env python3
import json, time, urllib.parse, urllib.request

BASE='https://edgex-prod-v2.edgex.exchange'
END=int(time.time()*1000)
BEGIN=END-30*24*60*60*1000

def get(path, params=None):
    url=BASE+path
    if params:
        url += '?' + urllib.parse.urlencode(params, doseq=True)
    req=urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'edgex-funding-probe/3.0'})
    try:
        with urllib.request.urlopen(req,timeout=30) as r:
            return r.status, r.read().decode('utf-8','replace')
    except Exception as e:
        if hasattr(e,'read'):
            try: return getattr(e,'code',0), e.read().decode('utf-8','replace')
            except Exception: pass
        return 0, repr(e)

status, body = get('/api/v2/public/meta/getMetaData')
meta = json.loads(body) if status == 200 else {}
contracts = []
for item in ((meta.get('data') or {}).get('contractList') or []):
    if not isinstance(item, dict): continue
    if str(item.get('enableTrade')).lower() not in ('true','1'): continue
    contracts.append((str(item.get('contractName') or ''), str(item.get('contractId') or '')))

print('CONTRACT_DISCOVERY', json.dumps({'status':status,'count':len(contracts),'sample':contracts[:20]},ensure_ascii=False), flush=True)

# Prefer BTC, then ETH, then first tradable contracts.
ordered = sorted(contracts, key=lambda x: (0 if 'BTC' in x[0].upper() else 1 if 'ETH' in x[0].upper() else 2, x[0]))
targets = ordered[:3]
ids = [cid for _,cid in targets if cid]
print('FUNDING_TARGETS', json.dumps(targets,ensure_ascii=False), flush=True)

if ids:
    status, body = get('/api/v2/public/funding/getLatestFundingRate', {'contractId':ids})
    print('FUNDING_LATEST', json.dumps({'status':status,'body':body[:16000]},ensure_ascii=False), flush=True)

for name,cid in targets:
    status, body = get('/api/v2/public/funding/getFundingRatePage', {
        'contractId':cid,
        'size':'640',
        'filterSettlementFundingRate':'true',
        'filterBeginTimeInclusive':str(BEGIN),
        'filterEndTimeExclusive':str(END),
    })
    print('FUNDING_HISTORY', json.dumps({'name':name,'contractId':cid,'status':status,'body':body[:32000]},ensure_ascii=False), flush=True)
