import asyncio
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

# This test suite runs in its own process to preserve the original core tests.
_temp = tempfile.TemporaryDirectory()
os.environ['VARIATIONAL_DB_PATH'] = str(Path(_temp.name)/'variational_terminal.db')
os.environ['VARIATIONAL_PUBLIC_ORIGIN'] = 'https://variational.example'
from variational_terminal import server as s
from variational_terminal.market import parse_stats, view, numeric, StatsClient
import httpx

NOW = 1792089000000

def listing(ticker='BTC'):
    return dict(ticker=ticker,name=ticker,mark_price='100.123456789012345',funding_rate='0',
                funding_interval_s=28800,volume_24h='123',quotes=dict(updated_at='2026-10-15T18:20:00Z',base=dict(bid='99',ask='101')))


def contract(cid='variational:BTC'):
    return s.core.scanner.Contract(cid,'BTCUSDC','USDC',True,True)


def candles(interval,n):
    step=s.core.scanner.INTERVAL_MS[interval]
    end=NOW//step*step
    return [s.core.scanner.Candle('variational:BTC','BTCUSDC',interval,end-(n-i)*step,
            100,102,98,101,1000,100000,10) for i in range(n)]


class StatsTests(unittest.TestCase):
    def test_precision_and_optional_fields(self):
        m=parse_stats({'listings':[listing(),dict(ticker='ETH')]},observed_ms=NOW)
        self.assertEqual(m['BTC']['mark_price'],'100.123456789012345')
        self.assertEqual(m['BTC']['funding_rate'],'0')
        self.assertEqual(m['BTC']['funding_rate_basis'],'UNVERIFIED_API_UNIT')
        self.assertIsNone(m['ETH']['mark_price'])
        self.assertIsNone(m['ETH']['mark_updated_ms'])
        self.assertIsNone(m['ETH']['indicative_bid'])

    def test_non_finite_and_bool_are_not_prices(self):
        for bad in ('NaN','inf',True,{},'1e10000'):
            self.assertIsNone(numeric(bad,positive=True))
        self.assertIsNone(numeric('-1',positive=True))
        self.assertIsNone(numeric('0',positive=True))
        self.assertEqual(numeric('-0.05'),'-0.05')

    def test_ui_does_not_invent_funding_rate_period_or_units(self):
        js=Path(s.__file__).with_name('venue.js').read_text()
        self.assertNotIn('Number(m.funding_rate)*100',js)
        self.assertIn('Funding API値',js)
        self.assertIn('単位は未確認',js)

    def test_invalid_stats_and_duplicate_symbols(self):
        for raw in ({},dict(listings=[]),dict(listings=[listing(),listing()]),dict(listings=[dict(ticker='<script>')])):
            with self.assertRaises(ValueError):parse_stats(raw,observed_ms=NOW)

    def test_quote_fetch_time_is_not_quote_time(self):
        m=parse_stats({'listings':[listing()]},observed_ms=NOW)['BTC']
        m['quote_updated_ms']=NOW-600000
        result=view(m,now_ms=NOW)
        self.assertEqual(result['stats_age_seconds'],0)
        self.assertEqual(result['quote_age_seconds'],600)
        self.assertEqual(result['quote_timestamp_status'],'STALE')
        self.assertFalse(result['quote_executable'])
        self.assertIsNone(result['mark_updated_ms'])

    def test_future_unknown_and_120s_boundary(self):
        m=parse_stats({'listings':[listing()]},observed_ms=NOW)['BTC']
        for ts,expected in [(None,'UNKNOWN'),(NOW+1,'FUTURE_TIMESTAMP'),(NOW-119999,'RECENT_INDICATIVE'),(NOW-120000,'STALE')]:
            m['quote_updated_ms']=ts
            self.assertEqual(view(m,now_ms=NOW)['quote_timestamp_status'],expected)


class ReferenceTests(unittest.TestCase):
    def test_valid_closed_windows(self):
        for interval,n in [('HOUR_4',50),('MINUTE_15',30)]:
            self.assertTrue(s.valid_window(candles(interval,n),contract(),interval,NOW,n))

    def test_gaps_stale_identity_conflicts_and_bad_prices(self):
        original=candles('MINUTE_15',30)
        from dataclasses import replace
        sets=[original[:-1],original[:10]+original[11:],
              original[:-1]+[replace(original[-1],contract_id='WRONG')],
              original+[replace(original[-1],close=100)],
              original[:-1]+[replace(original[-1],close=float('nan'))],
              original[:-1]+[replace(original[-1],low=110)],
              original[:-1]+[replace(original[-1],volume=-1)]]
        for data in sets:
            self.assertFalse(s.valid_window(data,contract(),'MINUTE_15',NOW,30))

    def test_unclosed_candle_never_fills_missing_closed_candle(self):
        from dataclasses import replace
        original=candles('MINUTE_15',30)
        current=replace(original[-1],time_ms=NOW//900000*900000)
        self.assertFalse(s.valid_window(original[:-1]+[current],contract(),'MINUTE_15',NOW,30))

    def test_invalid_window_cannot_be_ready(self):
        r=s.analyze_contract(contract(),[],[],as_of_ms=NOW)
        self.assertEqual(r['stage'],'DATA_WAIT')
        self.assertFalse(r['native_entry_ready'])
        self.assertFalse(r['real_orders_enabled'])
        self.assertEqual(r['candle_source'],'EDGEX_PUBLIC_LAST_PRICE')

    def test_no_private_method_or_history_escape(self):
        self.assertFalse(hasattr(s.client,'get_account_asset'))
        with self.assertRaises(ValueError):s.client._get_json_sync('/api/v2/private/order/create',{})
        with self.assertRaises(ValueError):s.client._get_json_sync('/api/v2/public/quote/getKline',dict(contractId='WRONG'))

    def test_stable_keys_and_database_identity(self):
        s.init_storage()
        pub=s.core.VAPID_PUBLIC_KEY
        with s.core._db_connect() as conn:
            conn.execute("INSERT OR REPLACE INTO venue_identity VALUES (1,'VARIATIONAL_REFERENCE_V1')")
        s.init_storage()
        self.assertEqual(pub,s.core.VAPID_PUBLIC_KEY)
        self.assertNotEqual(s.core.DB_PATH.name,'analysis_terminal.db')
        self.assertEqual(s.core._live_execution_config.mode,'OFF')

    def test_foreign_database_rejected_before_migration(self):
        original=s.DB_PATH
        foreign=Path(_temp.name)/'unrelated.db'
        with sqlite3.connect(foreign) as conn:conn.execute('CREATE TABLE original(value TEXT)')
        s.DB_PATH=foreign
        try:
            with self.assertRaises(RuntimeError):s.init_storage()
            with sqlite3.connect(foreign) as conn:
                self.assertEqual([r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")],['original'])
        finally:s.DB_PATH=original

    def test_push_is_clearly_reference(self):
        with patch.object(s,'SEND_PUSH',return_value=True) as send:
            s.send_push({},dict(title='EdgeX エントリー可能',body='エントリー可能',notification_kind='READY',tag='edgex-ready-BTC'))
        payload=send.call_args.args[1]
        self.assertIn('参考',payload['title'])
        self.assertIn('Omni',payload['body'])
        self.assertNotIn('エントリー可能',payload['body'])
        self.assertNotIn('edgex',payload['tag'])


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_symbol_mapping_without_alias_or_scale(self):
        client=s.PublicReferenceClient()
        with patch.object(s.stats,'refresh',AsyncMock(return_value={'BTC':{},'1000PEPE':{},'UNMAPPED':{}})),patch.object(s.REFERENCE_CLIENT,'get_contracts',AsyncMock(return_value={'1':s.core.scanner.Contract('1','BTCUSDC','USDC',True,True),'2':s.core.scanner.Contract('2','PEPEUSDC','USDC',True,True)})):
            result=await client.get_contracts()
        self.assertEqual(len(result),3)
        self.assertEqual(list(client.reference_by_id),['variational:BTC'])
        self.assertTrue(all(c.step_size is None and c.min_order_size is None for c in result.values()))

    async def test_reference_fetch_preserves_default_intervals_and_remaps_identity(self):
        original=s.core.scanner.Candle('1','BTCUSDC','MINUTE_15',NOW//900000*900000-900000,100,102,98,101,1000,100000,10)
        with patch.object(s.client,'get_contracts',AsyncMock(return_value={})),patch.object(s.client,'reference_by_id',{'variational:BTC':s.core.scanner.Contract('1','BTCUSDC','USDC',True,True)}),patch.object(s.client,'contracts',{'variational:BTC':contract()}),patch.object(s,'REFERENCE_FETCH',AsyncMock(return_value={('1','MINUTE_15'):[original]})) as fetch:
            result=await s.fetch_snapshots(['variational:BTC','variational:UNMAPPED'])
        self.assertEqual(fetch.call_args.kwargs['intervals'],('HOUR_4','MINUTE_15'))
        self.assertEqual(fetch.call_args.args[0],['1'])
        self.assertEqual(result[('variational:BTC','MINUTE_15')][0].contract_id,'variational:BTC')
        self.assertEqual(result[('variational:BTC','MINUTE_15')][0].close,original.close)

    async def test_mixed_reference_stream_cannot_be_relabelled_as_another_market(self):
        from dataclasses import replace
        original=s.core.scanner.Candle('1','BTCUSDC','MINUTE_15',NOW//900000*900000-900000,100,102,98,101,1000,100000,10)
        for bad in (replace(original,contract_id='2'),replace(original,interval='HOUR_4')):
            with patch.object(s.client,'get_contracts',AsyncMock(return_value={})),patch.object(s.client,'reference_by_id',{'variational:BTC':s.core.scanner.Contract('1','BTCUSDC','USDC',True,True)}),patch.object(s.client,'contracts',{'variational:BTC':contract()}),patch.object(s,'REFERENCE_FETCH',AsyncMock(return_value={('1','MINUTE_15'):[original,bad]})):
                with self.assertRaises(ValueError):await s.fetch_snapshots(['variational:BTC'])

    async def test_native_stats_shared_cache(self):
        response=Mock(status_code=200)
        response.json.return_value={'listings':[listing()]}
        client=StatsClient(get=Mock(return_value=response))
        await asyncio.gather(*[client.refresh() for _ in range(10)])
        self.assertEqual(client.get.call_count,1)
        self.assertFalse(client.get.call_args.kwargs['allow_redirects'])

    async def test_native_failure_does_not_refresh_previous_data_timestamp(self):
        client=StatsClient(get=Mock(side_effect=RuntimeError('unavailable')))
        client.observed_ms=1
        with self.assertRaises(RuntimeError):await client.refresh()
        self.assertEqual(client.observed_ms,1)

    async def test_routes_and_browser_security(self):
        s.init_storage()
        transport=httpx.ASGITransport(app=s.app)
        async with httpx.AsyncClient(transport=transport,base_url='https://variational.example') as c:
            for path in ['/','/btc','/health','/api/push/config','/api/readiness-history','/api/paper-execution','/api/btc-wave','/manifest.webmanifest','/sw.js']:
                response=await c.get(path)
                self.assertEqual(response.status_code,200,path)
            for path in ['/api/execution/control','/api/execution/session','/api/account/session','/api/account/risk','/api/order/create']:
                response=await c.post(path,json={})
                self.assertEqual(response.status_code,404,path)
            response=await c.post('/api/watchlist',json={},headers={'Origin':'https://evil.example'})
            self.assertEqual(response.status_code,403)
            response=await c.get('/')
            nonce=re.search("script-src 'nonce-([^']+)'",response.headers['content-security-policy']).group(1)
            nonces=re.findall('<script nonce="([^"]+)"',response.text)
            self.assertTrue(nonces)
            self.assertTrue(all(n==nonce for n in nonces))
            self.assertEqual(len(re.findall(r'<button[^>]+class="tab[^"]*"[^>]+data-tab=',response.text)),4)
            self.assertIn('参考足',response.text)
            self.assertNotIn('https://pro.edgex.exchange',response.text)
            self.assertIn('entryNowZone',response.text)
            self.assertIn('globalEntryBar',response.text)
            self.assertEqual((await c.get('/api/live-execution')).json()['real_orders_enabled'],False)

    async def test_javascript_syntax_and_disclosure_both_pages(self):
        for btc,response in [(False,await s.index()),(True,await s.btc_page())]:
            html=response.body.decode()
            self.assertIn('EdgeXの参考足',html)
            scripts=re.findall(r'<script[^>]*>([\s\S]*?)</script>',html)
            check='\n'.join('new Function('+json.dumps(script)+');' for script in scripts)
            subprocess.run(['node'],input=check,text=True,check=True,capture_output=True)


class PwaTests(unittest.IsolatedAsyncioTestCase):
    async def test_installable_manifest_icons_and_existing_identity(self):
        import struct
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=s.app),base_url='https://variational.example') as c:
            response=await c.get('/manifest.webmanifest')
            self.assertEqual(response.status_code,200)
            self.assertIn('application/manifest+json',response.headers['content-type'])
            m=response.json()
            self.assertEqual(m['id'],'/?tab=dashboard')
            self.assertEqual(m['id'],m['start_url'])
            self.assertEqual(m['scope'],'/')
            self.assertEqual(m['display'],'standalone')
            for icon in m['icons']:
                r=await c.get(icon['src']);self.assertEqual(r.status_code,200)
                if icon['type']=='image/png':
                    self.assertTrue(r.content.startswith(b'\x89PNG\r\n\x1a\n'))
                    width,height=struct.unpack('>II',r.content[16:24])
                    self.assertEqual(icon['sizes'],f'{width}x{height}')
            apple=await c.get('/icons/apple-touch-icon.png')
            self.assertEqual(struct.unpack('>II',apple.content[16:24]),(180,180))
            self.assertEqual((await c.get('/icons/secret.pem')).status_code,404)

    async def test_install_controls_on_main_and_btc_pages(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=s.app),base_url='https://variational.example') as c:
            for path in ['/','/btc']:
                r=await c.get(path)
                self.assertEqual(r.status_code,200)
                self.assertEqual(r.text.count('id="pwaInstallButton"'),1)
                self.assertIn('rel="apple-touch-icon"',r.text)
                self.assertIn('pwaInstallGuide',r.text)
                self.assertIn('ホーム画面',r.text)
                self.assertIn('v1.0.1',r.text)
                nonce=re.search("script-src 'nonce-([^']+)'",r.headers['content-security-policy']).group(1)
                self.assertTrue(all(n==nonce for n in re.findall('<script nonce="([^"]+)"',r.text)))

    async def test_offline_page_contains_no_prices_or_ready_actions(self):
        r=await s.offline_page()
        html=r.body.decode()
        self.assertIn('オフライン',html)
        self.assertIn('最新の市場データとエントリー可否を確認できません',html)
        self.assertNotIn('entryNowZone',html)
        self.assertNotIn('READY',html)
        self.assertNotIn('<script',html)

    async def test_install_state_and_worker_cache_logic(self):
        for script in ['test_install_ui.js','test_service_worker.js']:
            result=subprocess.run(['node',str(Path(s.__file__).with_name(script))],text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)

    async def test_service_worker_script_syntax(self):
        r=await s.service_worker()
        subprocess.run(['node'],input='new Function('+json.dumps(r.body.decode())+');',text=True,check=True,capture_output=True)
        self.assertEqual(r.headers['service-worker-allowed'],'/')
        self.assertEqual(r.headers['cache-control'],'no-cache')


if __name__=='__main__':unittest.main()
