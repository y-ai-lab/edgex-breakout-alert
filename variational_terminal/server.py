"""Separate ASGI app; explicitly selects public routes and its own collectors.

The EdgeX lifespan/account/order routes are never mounted. Only the public
reference-data facade and pure analysis/UI/history helpers are reused in this
process. The existing EdgeX service, sources and data are not changed.
"""
from __future__ import annotations
import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from analysis_terminal import server as core
from variational_terminal.market import StatsClient, PROVENANCE, view
from variational_terminal.ui import render

VERSION = '1.0.1'
ORIGIN = os.getenv('VARIATIONAL_PUBLIC_ORIGIN', 'http://localhost:8000').rstrip('/')
DB_PATH = Path(os.getenv('VARIATIONAL_DB_PATH', '/data/variational_terminal.db'))
if DB_PATH.name == 'analysis_terminal.db':
    raise RuntimeError('EdgeX database must not be reused')
core.DB_PATH = DB_PATH
core.VAPID_KEY_PATH = DB_PATH.parent/'variational_vapid_private.pem'
core.VAPID_SUBJECT = ORIGIN
core.VAPID_PRIVATE_KEY = core.VAPID_PUBLIC_KEY = ''
core.push_security.PUBLIC_ORIGIN = ORIGIN
# Even an accidentally inherited execution configuration cannot be used here.
core._live_execution_config = replace(core._live_execution_config, mode='OFF')
REFERENCE_CLIENT = core.CLIENT
REFERENCE_FETCH = core.fetch_snapshots
REFERENCE_ANALYZE = core.analyze_contract
stats = StatsClient()


class PublicReferenceClient:
    def __init__(self):
        self.contracts = {}
        self.reference_by_id = {}
        self.lock = asyncio.Lock()
        self.refreshed_at = None

    async def get_contracts(self):
        async with self.lock:
            if self.contracts and self.refreshed_at is not None and time.monotonic()-self.refreshed_at < 60:
                return self.contracts
            markets, reference = await asyncio.gather(stats.refresh(), REFERENCE_CLIENT.get_contracts())
            by_name = {c.contract_name: c for c in reference.values()}
            contracts, mappings = {}, {}
            for ticker in markets:
                cid, name = 'variational:'+ticker, ticker+'USDC'
                # Literal symbol mapping only: no aliases or 1000x conversions.
                ref = by_name.get(name)
                contracts[cid] = core.scanner.Contract(cid, name, 'USDC', True, True,
                    None, None, None, None, None)
                if ref:
                    mappings[cid] = ref
            self.contracts, self.reference_by_id = contracts, mappings
            self.refreshed_at = time.monotonic()
            return contracts

    def _get_json_sync(self, path, params):
        if path != '/api/v2/public/quote/getKline':
            raise ValueError('PUBLIC_HISTORY_ONLY')
        cid = params.get('contractId')
        ref = self.reference_by_id.get(cid)
        if ref is None:
            raise ValueError('REFERENCE_UNAVAILABLE')
        raw = REFERENCE_CLIENT._get_json_sync(path, dict(params, contractId=ref.contract_id))
        if raw.get('code') == 'SUCCESS' and isinstance(raw.get('data'), dict):
            for item in raw['data'].get('dataList', []):
                if str(item.get('contractId')) != ref.contract_id:
                    raise ValueError('REFERENCE_IDENTITY_MISMATCH')
                item['contractId'] = cid
                item['contractName'] = self.contracts[cid].contract_name
        return raw


client = PublicReferenceClient()
core.CLIENT = client


async def fetch_snapshots(ids, intervals=None, timeout=25.0):
    await client.get_contracts()
    mapping = {cid: client.reference_by_id[cid] for cid in ids if cid in client.reference_by_id}
    if not mapping:
        return {}
    intervals = intervals or (core.SETTINGS.monitor_interval, core.SETTINGS.entry_interval)
    raw = await REFERENCE_FETCH([c.contract_id for c in mapping.values()], intervals=intervals, timeout=timeout)
    out = {}
    for cid, ref in mapping.items():
        for (ref_id, interval), candles in raw.items():
            if ref_id != ref.contract_id:
                continue
            if any(c.contract_id != ref_id or c.interval != interval for c in candles):
                raise ValueError('REFERENCE_STREAM_IDENTITY_MISMATCH')
            out[cid, interval] = [replace(c, contract_id=cid, contract_name=client.contracts[cid].contract_name)
                                 for c in candles]
    return out


snapshot_lock = asyncio.Lock()
async def market_snapshots(force=False):
    # Background refresh every minute; forced clicks cannot flood either venue.
    async with snapshot_lock:
        if core._snapshot_cache and time.time()-core._snapshot_cache[0] < (20 if force else 60):
            return core._snapshot_cache[1]
        await client.get_contracts()
        ids = list(client.reference_by_id)
        parts = await asyncio.gather(*(fetch_snapshots(ids[i:i+50]) for i in range(0,len(ids),50)),
                                     return_exceptions=True)
        data = {}
        for part in parts:
            if isinstance(part, dict):
                data.update(part)
        if not data:
            raise RuntimeError('REFERENCE_MARKET_DATA_UNAVAILABLE')
        core._snapshot_cache = (time.time(), data)
        return data


def valid_window(candles, contract, interval, now_ms, required):
    step = core.scanner.INTERVAL_MS[interval]
    found = {}
    for c in candles:
        if c.contract_id != contract.contract_id or c.interval != interval:
            return False
        if (not isinstance(c.time_ms, int) or isinstance(c.time_ms, bool) or c.time_ms % step
            or any(not math.isfinite(v) or v <= 0 for v in (c.open,c.high,c.low,c.close))
            or c.low > min(c.open,c.close) or c.high < max(c.open,c.close)
            or not math.isfinite(c.volume) or c.volume < 0):
            return False
        if c.time_ms+step > now_ms:
            continue
        if c.time_ms in found and found[c.time_ms] != c:
            return False
        found[c.time_ms] = c
    window = sorted(found)[-required:]
    return (len(window) >= required and window[-1]+step == now_ms//step*step
            and all(b-a == step for a,b in zip(window,window[1:])))


def analyze_contract(contract, monitor, entries, *, as_of_ms=None):
    now = int(time.time()*1000) if as_of_ms is None else as_of_ms
    s = core.SETTINGS
    needed = core.market_status.required_history(s)
    valid = (valid_window(monitor,contract,s.monitor_interval,now,needed[s.monitor_interval])
             and valid_window(entries,contract,s.entry_interval,now,needed[s.entry_interval]))
    row = REFERENCE_ANALYZE(contract,monitor if valid else [],entries if valid else [],as_of_ms=now)
    ticker = contract.contract_id.removeprefix('variational:')
    native = stats.markets.get(ticker)
    row.update(PROVENANCE)
    row['venue_ticker'] = ticker
    row['reference_ticker'] = contract.contract_name
    row['reference_status'] = ('AVAILABLE' if valid else 'NO_REFERENCE_MARKET'
                              if contract.contract_id not in client.reference_by_id else 'INVALID_OR_MISSING_CANDLES')
    row['venue_market'] = view(native,now_ms=now) if native else None
    return row


core.fetch_snapshots = fetch_snapshots
core.market_snapshots = market_snapshots
core.analyze_contract = analyze_contract


def init_storage():
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    if not core.VAPID_KEY_PATH.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        with core.VAPID_KEY_PATH.open('xb') as f:
            f.write(pem)
        core.VAPID_KEY_PATH.chmod(0o600)
    key = serialization.load_pem_private_key(core.VAPID_KEY_PATH.read_bytes(),password=None)
    public = key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
    core.VAPID_PRIVATE_KEY = core.VAPID_KEY_PATH.read_text().strip()
    core.VAPID_PUBLIC_KEY = base64.urlsafe_b64encode(public).decode().rstrip('=')
    # Refuse an unrelated populated DB before applying any shared migrations.
    if DB_PATH.exists():
        import sqlite3
        with sqlite3.connect(DB_PATH) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'venue_identity' not in tables:
                raise RuntimeError('DATABASE_VENUE_MISMATCH')
            if 'venue_identity' in tables and conn.execute('SELECT name FROM venue_identity WHERE id=1').fetchone() != ('VARIATIONAL_REFERENCE_V1',):
                raise RuntimeError('DATABASE_VENUE_MISMATCH')
    core._init_db()
    with core._db_connect() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS venue_identity (id INTEGER PRIMARY KEY CHECK(id=1), name TEXT NOT NULL)')
        conn.execute("INSERT OR IGNORE INTO venue_identity VALUES (1,'VARIATIONAL_REFERENCE_V1')")
        if conn.execute('SELECT name FROM venue_identity WHERE id=1').fetchone()[0] != 'VARIATIONAL_REFERENCE_V1':
            raise RuntimeError('DATABASE_VENUE_MISMATCH')


# Native trading is not implemented, including control/arm/account/quote signing.
# Register only public analysis and subscription-scoped endpoints.
PUBLIC_PATHS = {
 '/api/contracts','/api/analyze','/api/chart','/api/compare','/api/screener','/api/btc-wave',
 '/api/readiness-history','/api/readiness-review','/api/entry-history','/api/setups',
 '/api/candidate-events','/api/priority-events','/api/approach-events','/api/server-history',
 '/api/server-paper-signals','/api/confirmation-diagnostics','/api/opportunity-analysis',
 '/api/approach-validation','/api/paper-execution','/api/paper-execution/events',
 '/api/watchlist','/api/risk','/api/daily-reports',
 '/api/push/config','/api/push/session','/api/push/subscribe','/api/push/unsubscribe',
 '/api/push/preferences','/api/push/test','/api/push/events',
}


SEND_PUSH = core._send_push_sync
def send_push(subscription, payload):
    body = dict(payload)
    body['title'] = 'Variational 参考条件通知' if body.get('notification_kind')=='READY' else 'Variational 分析'
    body['body'] = 'EdgeX参考足の分析です。Omniの価格・スプレッド・SL/TPを確認してください。\n'+str(body.get('body','')).replace('エントリー可能','参考条件成立')
    body['tag'] = str(body.get('tag','update')).replace('edgex','variational')
    return SEND_PUSH(subscription,body)
core._send_push_sync = send_push

collector_state = dict(last_success_ms=None,last_error=None,scanned=0)
async def collect_once():
    contracts, rows = await core._scan_market_rows(force=False)
    core._persist_scan_result(contracts,rows)
    core._persist_setup_lifecycles(rows)
    with core._db_connect() as conn:
        core.entry_history.capture(conn,rows,now_ms=int(time.time()*1000),min_rr=core.SETTINGS.min_rr,
            monitor_ms=core.scanner.INTERVAL_MS[core.SETTINGS.monitor_interval],
            entry_ms=core.scanner.INTERVAL_MS[core.SETTINGS.entry_interval])
        conn.commit()
    core._simulation_cycle(contracts,rows)
    await core._process_priority_changes(rows,int(time.time()*1000)//900000*900000)
    await core._maybe_push_candidate_changes(rows)
    await core._refresh_paper_signal_results(contracts)
    await core._refresh_candidate_event_results(contracts)
    await core._refresh_approach_event_results(contracts)
    await core._recover_simulation_history(contracts)
    collector_state.update(last_success_ms=int(time.time()*1000),last_error=None,scanned=len(rows))


async def collector():
    while True:
        try:
            await collect_once()
        except Exception as exc:
            collector_state['last_error'] = type(exc).__name__
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app):
    init_storage()
    tasks = [asyncio.create_task(collector()),asyncio.create_task(core._background_btc_wave())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


app = FastAPI(title='Variational Analysis Terminal',version=VERSION,lifespan=lifespan)
app.add_middleware(core.push_security.BrowserSecurityMiddleware)


@app.middleware('http')
async def provenance_headers(request, call_next):
    response = await call_next(request)
    response.headers['X-Analysis-Scope'] = 'EXTERNAL_REFERENCE_ONLY'
    response.headers['X-Candle-Source'] = 'EDGEX_PUBLIC_LAST_PRICE'
    response.headers['X-Native-Trading'] = 'UNSUPPORTED'
    return response

for route in core.app.routes:
    if getattr(route,'path',None) in PUBLIC_PATHS:
        app.router.routes.append(route)


@app.get('/health')
async def health():
    return dict(ok=True,service='variational-analysis-terminal',version=VERSION,time_ms=int(time.time()*1000),
        provenance=PROVENANCE,collector=dict(collector_state),
        storage=dict(db_exists=DB_PATH.exists(),market_snapshots_48h=len(core._load_market_history())),
        push=dict(enabled=core._push_enabled(),subscribers=core._subscription_count(),notification_policy='REFERENCE_READY_ONLY'))


@app.get('/api/variational/markets')
async def markets_api():
    await client.get_contracts()
    now = int(time.time()*1000)
    items = [dict(view(m,now_ms=now),reference_available='variational:'+t in client.reference_by_id)
             for t,m in stats.markets.items()]
    return dict(provenance=PROVENANCE,total=len(items),reference_mapped=len(client.reference_by_id),
                collector=dict(collector_state),markets=items)


@app.get('/api/variational/market')
async def market_api(ticker: str = Query(min_length=1,max_length=64)):
    await client.get_contracts()
    bare = ticker.upper().removesuffix('USDC')
    if bare not in stats.markets:
        raise HTTPException(404,'Variational market not found')
    return dict(provenance=PROVENANCE,market=view(stats.markets[bare],now_ms=int(time.time()*1000)))


@app.get('/api/live-execution')
async def disabled_execution():
    return dict(status='OFF',armed=False,real_orders_enabled=False,automatic_promotion=False,
                source='CURRENT_READY_ONLY',active_orders=0,status_counts={},
                pause_reason='UNSUPPORTED_VENUE',provenance=PROVENANCE)


@app.get('/api/account/config')
async def account_config():
    return dict(enabled=False,status='UNSUPPORTED',reason='Variational private API is not configured')


@app.get('/')
async def index():
    response = await core.index()
    return core.push_security.html_response(render(response.body.decode(),btc=False))


@app.get('/btc')
async def btc_page():
    response = await core.btc_wave_page()
    return core.push_security.html_response(render(response.body.decode(),btc=True))


@app.get('/manifest.webmanifest')
async def manifest():
    content = json.loads(Path(__file__).with_name('manifest.webmanifest').read_text())
    return JSONResponse(content,media_type='application/manifest+json',headers={'Cache-Control':'no-cache'})


@app.get('/sw.js')
async def service_worker():
    text = Path(__file__).with_name('sw.js').read_text()
    from fastapi.responses import Response
    return Response(text,media_type='application/javascript',headers={'Cache-Control':'no-cache','Service-Worker-Allowed':'/'})


@app.get('/app-icon.svg')
async def icon():
    return FileResponse(Path(__file__).with_name('app-icon.svg'),media_type='image/svg+xml')


@app.get('/icons/{name}')
async def pwa_icon(name: str):
    if name not in {'icon-192.png','icon-512.png','apple-touch-icon.png'}:
        raise HTTPException(404,'Icon not found')
    return FileResponse(Path(__file__).with_name('icons')/name,media_type='image/png',
                        headers={'Cache-Control':'public, max-age=3600'})


@app.get('/offline.html')
async def offline_page():
    return core.push_security.html_response(Path(__file__).with_name('offline.html').read_text())


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app,host='0.0.0.0',port=int(os.getenv('PORT','8000')))
