"""Private viewing authority and mocked GET-only EdgeX data; no live orders."""
import asyncio
import copy
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
from httpx import ASGITransport,AsyncClient
from analysis_terminal import account_view as view,server,push_security
from analysis_terminal import test_push_security as push_fixtures
from analysis_terminal import test_live_execution as fixtures

HTTPException=view.HTTPException
NOW=1791417600000+100000

def raw_asset():
    return dict(account={'id':'123'},collateralAssetModelList=[dict(accountId='123',coinId='1000',totalEquity='100.00',availableAmount='80',initialMarginRequirement='20')],
        collateralList=[dict(accountId='123',coinId='1000',amount='95')],
        positionList=[dict(accountId='123',contractId='1',openSize='-.02')],
        positionAssetList=[dict(accountId='123',contractId='1',avgEntryPrice='60000',liquidatePrice='70000',unrealizePnl='5',initialMarginRequirement='20')])

def raw_page(kind='positions',**changes):
    r=dict(id='1',accountId='123',coinId='1000',createdTime=str(NOW-10),contractId='1',type='SELL_POSITION',
        censorStatus='L2_APPROVED',fillPrice='60000',fillOpenSize='0.02',fillCloseSize='0',realizePnl='5',fillOpenFee='0.6',fillCloseFee='0',deltaFundingFee='0.1',deltaAmount='-10',beforeAmount='110',privateField='must-not-return')
    r.update(changes)
    return dict(dataList=[r],nextPageOffsetData='')

class ParserTests(unittest.TestCase):
    def test_asset_separates_cash_equity_available_all_positions_and_unknown_roi(self):
        raw=raw_asset();before=copy.deepcopy(raw)
        r=view.asset(raw,'123',{'1':'BTCUSDC'},observed_ms=NOW)
        self.assertEqual(r['balance']['equity_usdc'],'100.00')
        self.assertEqual(r['balance']['cash_usdc'],'95')
        self.assertEqual(r['balance']['available_usdc'],'80')
        self.assertEqual(r['positions'][0]['direction'],'SHORT')
        self.assertEqual(r['positions'][0]['quantity'],'0.02')
        self.assertFalse(r['arc_wallet_included']);self.assertIsNone(r['portfolio_roi_pct'])
        self.assertEqual(raw,before)
        self.assertNotIn('accountId',json.dumps(r));self.assertNotIn('contractId',json.dumps(r))

    def test_zero_negative_equity_visible_missing_optional_is_not_zero(self):
        for equity in ('0','-5'):
            raw=raw_asset();raw['collateralAssetModelList'][0]['totalEquity']=equity
            raw.pop('collateralList');raw.pop('positionAssetList')
            r=view.asset(raw,'123',{},observed_ms=NOW)
            self.assertEqual(r['balance']['equity_usdc'],equity)
            self.assertIsNone(r['balance']['cash_usdc']);self.assertIsNone(r['balance']['unrealized_pnl_usdc'])
            self.assertIsNone(r['positions'][0]['entry_price'])

    def test_missing_or_duplicate_positions_and_wrong_accounts_reject_not_flat(self):
        for kind in ('missing','duplicate','wrong_account','nan','missing_equity','invalid_detail'):
            raw=raw_asset()
            if kind=='missing':raw.pop('positionList')
            if kind=='duplicate':raw['positionList']*=2
            if kind=='wrong_account':raw['positionList'][0]['accountId']='999'
            if kind=='nan':raw['positionList'][0]['openSize']='NaN'
            if kind=='missing_equity':raw['collateralAssetModelList'][0].pop('totalEquity')
            if kind=='invalid_detail':raw['positionAssetList'][0]['unrealizePnl']='Infinity'
            with self.subTest(kind=kind),self.assertRaises(view.AccountDataError):view.asset(raw,'123',{},observed_ms=NOW)

    def test_empty_verified_position_list_is_flat_not_missing(self):
        raw=raw_asset();raw['positionList']=[]
        self.assertEqual(view.asset(raw,'123',{},observed_ms=NOW)['positions'],[])

    def test_history_no_net_or_roi_inferred_and_no_private_field(self):
        for kind in ('positions','collateral'):
            r=view.history_page(raw_page(kind),'123',{'1':'BTCUSDC'},kind,observed_ms=NOW,start_ms=NOW-view.DAY,end_ms=NOW)
            self.assertEqual(len(r['items']),1)
            self.assertNotIn('must-not-return',json.dumps(r['items']))
            self.assertNotIn('accountId',json.dumps(r['items']))
            self.assertNotIn('roi',json.dumps(r['items']).lower())
        r=view.history_page(raw_page(),'123',{},'positions',observed_ms=NOW,start_ms=NOW-view.DAY,end_ms=NOW)
        self.assertEqual(r['items'][0]['realized_pnl_usdc'],'5')
        self.assertEqual(r['items'][0]['open_fee_usdc'],'0.6')
        self.assertEqual(r['items'][0]['funding_delta_usdc'],'0.1')

    def test_history_missing_page_foreign_coin_duplicates_and_window_violation_reject(self):
        pages=[{},raw_page(accountId='999'),raw_page(coinId='9'),raw_page(createdTime=str(NOW)),raw_page(createdTime=str(NOW-view.DAY-1)),raw_page(fillPrice='NaN')]
        r=raw_page();r['dataList']*=2;pages.append(r)
        r=raw_page();r['nextPageOffsetData']=None;pages.append(r)
        for page in pages:
            with self.subTest(page=page),self.assertRaises(view.AccountDataError):view.history_page(page,'123',{},'positions',observed_ms=NOW,start_ms=NOW-view.DAY,end_ms=NOW)

    def test_optional_empty_numbers_are_missing_not_zero(self):
        for value in ('','   ',None):
            self.assertIsNone(view.number(value))
        for value in ('NaN','Infinity','not-a-number'):
            with self.assertRaises(view.AccountDataError):view.number(value)

    def test_funding_history_empty_optional_values_remain_unknown(self):
        page=raw_page(type='SETTLE_FUNDING_FEE',fillPrice='',fillOpenSize='',fillCloseSize='',realizePnl='',fillOpenFee='',fillCloseFee='',deltaFundingFee='0.25')
        result=view.history_page(page,'123',{},'positions',observed_ms=NOW,start_ms=NOW-view.DAY,end_ms=NOW)['items'][0]
        self.assertIsNone(result['price']);self.assertIsNone(result['realized_pnl_usdc'])
        self.assertIsNone(result['open_quantity']);self.assertIsNone(result['close_fee_usdc'])
        self.assertEqual(result['funding_delta_usdc'],'0.25')

    def test_required_empty_balances_quantities_and_cash_changes_stay_invalid(self):
        for value in ('','   ',None):
            with self.assertRaises(view.AccountDataError):view.number(value,required=True)
        raw=raw_asset();raw['positionList'][0]['openSize']=''
        with self.assertRaises(view.AccountDataError):view.asset(raw,'123',{},observed_ms=NOW)
        with self.assertRaises(view.AccountDataError):view.history_page(raw_page(deltaAmount=''),'123',{},'collateral',observed_ms=NOW,start_ms=NOW-view.DAY,end_ms=NOW)

    def test_stale_boundaries_fail_closed(self):
        store=view.Store();store.asset=view.asset(raw_asset(),'123',{},observed_ms=NOW)
        self.assertAlmostEqual(store.positions(now_ms=NOW+29999)['snapshot_age_seconds'],29.999)
        for now in (NOW-1,NOW+30000):
            with self.assertRaises(HTTPException):store.positions(now_ms=now)
        store.asset=None
        with self.assertRaises(HTTPException):store.positions(now_ms=NOW)

class APIAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        for obj,name,value in ((server,'DB_PATH',Path(self.directory.name)/'test.db'),(server,'VAPID_PRIVATE_KEY',''),(server,'_live_execution_config',fixtures.configuration()),(view,'STORE',view.Store()),(view,'SESSIONS',view.Sessions()),(push_security,'LIMITER',push_security.RateLimiter())):
            p=patch.object(obj,name,value);p.start();self.addCleanup(p.stop)
        server._init_db()
        self.sub=push_fixtures.subscription()
        server._save_push_subscription(server.PushSubscriptionRequest(subscription=self.sub))
        # Model a sole subscription that existed before the first upgrade.
        with server._db_connect() as conn:
            conn.execute('DELETE FROM account_view_owner')
            conn.execute('UPDATE push_subscriptions SET created_ms=?',(NOW-1,))
            view.initialize(conn,server._live_execution_config,now_ms=NOW)
        self.client=AsyncClient(transport=ASGITransport(app=server.app),base_url='https://test');self.addAsyncCleanup(self.client.aclose)
        now=int(time.time()*1000)
        view.STORE.asset=view.asset(raw_asset(),'123',{},observed_ms=now)
        view.STORE.window=(NOW-view.DAY,NOW)
        view.STORE.histories={k:view.history_page(raw_page(k),'123',{},k,observed_ms=NOW,start_ms=NOW-view.DAY,end_ms=NOW) for k in ('positions','collateral')}
        view.STORE.last_cycle_ms=now

    async def header(self):
        r=await self.client.post('/api/account/session',json={'subscription':self.sub})
        self.assertEqual(r.status_code,200)
        return {'Authorization':'Bearer '+r.json()['view_token']}

    async def test_unauthed_endpoint_management_token_and_forged_keys_never_read(self):
        for route in ('overview','positions'):
            r=await self.client.get('/api/account/'+route)
            self.assertEqual(r.status_code,401);self.assertNotIn('100.00',r.text)
            r=await self.client.get('/api/account/'+route,headers={'Authorization':'Bearer '+push_security.management_token(self.sub)})
            self.assertEqual(r.status_code,401)
        forged=push_fixtures.subscription(auth=b'0123456789abcdef')
        self.assertEqual((await self.client.post('/api/account/session',json={'subscription':forged})).status_code,403)

    async def test_sole_sealed_device_reads_values_but_no_order_controls(self):
        header=await self.header()
        with server._db_connect() as conn:before=list(conn.iterdump())
        for route in ('overview','positions'):
            r=await self.client.get('/api/account/'+route,headers=header)
            self.assertEqual(r.status_code,200);self.assertEqual(r.json()['balance']['equity_usdc'],'100.00')
            self.assertEqual(r.headers['cache-control'],'no-store')
            self.assertEqual((await self.client.post('/api/account/'+route,headers=header,json={})).status_code,405)
        with server._db_connect() as conn:self.assertEqual(list(conn.iterdump()),before)

    async def test_new_push_device_cannot_read_or_rebind_owner_after_restart(self):
        other=push_fixtures.subscription('https://web.push.apple.com/other')
        server._save_push_subscription(server.PushSubscriptionRequest(subscription=other))
        server._init_db()
        r=await self.client.post('/api/account/session',json={'subscription':other})
        self.assertEqual(r.status_code,403)
        self.assertEqual((await self.client.get('/api/account/overview',headers=await self.header())).status_code,200)

    async def test_deleting_owner_revokes_and_does_not_enroll_replacement(self):
        header=await self.header();server._delete_push_subscription(self.sub['endpoint'])
        self.assertEqual((await self.client.get('/api/account/overview',headers=header)).status_code,403)
        other=push_fixtures.subscription('https://web.push.apple.com/new-device')
        server._save_push_subscription(server.PushSubscriptionRequest(subscription=other));server._init_db()
        self.assertEqual((await self.client.post('/api/account/session',json={'subscription':other})).status_code,403)

    async def test_account_change_and_expiry_revoke(self):
        header=await self.header();session=view.SESSIONS.items[header['Authorization'][7:]]
        session['expires']=view.SESSIONS.clock()-1
        self.assertEqual((await self.client.get('/api/account/overview',headers=header)).status_code,401)
        header=await self.header();server._live_execution_config.account_id='999'
        self.assertEqual((await self.client.get('/api/account/overview',headers=header)).status_code,401)
        self.assertEqual((await self.client.post('/api/account/session',json={'subscription':self.sub})).status_code,403)

    async def test_logout_invalidate_only_view_session_not_execution(self):
        header=await self.header()
        before=(await self.client.get('/api/live-execution')).json()
        self.assertEqual((await self.client.post('/api/account/logout',headers=header,json={})).status_code,200)
        self.assertEqual((await self.client.get('/api/account/overview',headers=header)).status_code,401)
        after=(await self.client.get('/api/live-execution')).json()
        self.assertEqual(before['operator_control'],after['operator_control']);self.assertEqual(before['armed'],after['armed'])

    async def test_availability_is_public_without_values_or_identifiers(self):
        r=await self.client.get('/api/account/config');self.assertEqual(r.status_code,200)
        self.assertTrue(r.json()['owner_device_bound']);self.assertTrue(r.json()['read_only'])
        for text in ('100.00','balance','accountId',self.sub['endpoint'],'position_count'):
            self.assertNotIn(text,r.text)

    async def test_cross_site_oversized_bootstrap_rejected(self):
        r=await self.client.post('/api/account/session',json={'subscription':self.sub},headers={'Origin':'https://evil.test'})
        self.assertEqual(r.status_code,403)
        r=await self.client.post('/api/account/session',content=b'x'*16385)
        self.assertEqual(r.status_code,413)

    async def test_unavailable_sections_are_null_not_zero_records(self):
        view.STORE.histories={}
        r=await self.client.get('/api/account/overview',headers=await self.header())
        self.assertEqual(r.status_code,200)
        self.assertIsNone(r.json()['histories']['positions']['items']);self.assertIsNone(r.json()['histories']['positions']['complete'])
        view.STORE.asset=None
        r=await self.client.get('/api/account/positions',headers=await self.header())
        self.assertEqual(r.status_code,503);self.assertNotIn('positions',r.json())

    async def test_paginated_frozen_window_is_session_scoped_and_single_use(self):
        page=view.STORE.histories['positions'];page['offset']='private-offset'
        header=await self.header()
        r=await self.client.get('/api/account/overview',headers=header);cursor=r.json()['histories']['positions']['next_cursor']
        self.assertNotIn('private-offset',r.text);self.assertFalse(r.json()['histories']['positions']['complete'])
        next_page=view.history_page(raw_page(id='2'),'123',{},'positions',observed_ms=NOW,start_ms=NOW-view.DAY,end_ms=NOW)
        reader=Mock();reader.history=AsyncMock(return_value=next_page);view.STORE.reader=reader
        url='/api/account/history?kind=positions&cursor='+cursor
        other=await self.header();self.assertEqual((await self.client.get(url,headers=other)).status_code,400)
        r=await self.client.get(url,headers=header);self.assertEqual(r.status_code,200);self.assertTrue(r.json()['complete'])
        reader.history.assert_awaited_once_with('positions',NOW-view.DAY,NOW,'private-offset')
        self.assertEqual((await self.client.get(url,headers=header)).status_code,400)

    async def test_duplicate_pages_and_repeated_offsets_do_not_claim_complete(self):
        header=await self.header();page=view.STORE.histories['positions'];page['offset']='repeat'
        r=await self.client.get('/api/account/overview',headers=header);cursor=r.json()['histories']['positions']['next_cursor']
        reader=Mock();reader.history=AsyncMock(return_value=page);view.STORE.reader=reader
        r=await self.client.get('/api/account/history?kind=positions&cursor='+cursor,headers=header)
        self.assertEqual(r.status_code,409)

    async def test_revocation_during_await_prevents_returning_history(self):
        header=await self.header();view.STORE.histories['positions']['offset']='next'
        cursor=(await self.client.get('/api/account/overview',headers=header)).json()['histories']['positions']['next_cursor']
        async def revoked(*args):
            server._delete_push_subscription(self.sub['endpoint'])
            return view.history_page(raw_page(id='2'),'123',{},'positions',observed_ms=NOW,start_ms=NOW-view.DAY,end_ms=NOW)
        reader=Mock();reader.history=AsyncMock(side_effect=revoked);view.STORE.reader=reader
        r=await self.client.get('/api/account/history?kind=positions&cursor='+cursor,headers=header)
        self.assertEqual(r.status_code,403);self.assertNotIn('60000',r.text)

    async def test_empty_or_multiple_initial_devices_stays_sealed_no_public_enrollment(self):
        for count in (0,2):
            with server._db_connect() as conn:
                conn.execute('DELETE FROM account_view_owner');conn.execute('DELETE FROM push_subscriptions')
            for i in range(count):server._save_push_subscription(server.PushSubscriptionRequest(subscription=push_fixtures.subscription('https://web.push.apple.com/d'+str(i))))
            with server._db_connect() as conn:
                view.initialize(conn,server._live_execution_config,now_ms=int(time.time()*1000)+10)
                self.assertFalse(view.owner_exists(conn,server._live_execution_config))
            server._save_push_subscription(server.PushSubscriptionRequest(subscription=self.sub));server._init_db()
            self.assertEqual((await self.client.post('/api/account/session',json={'subscription':self.sub})).status_code,403)

class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_reviewed_get_paths_no_signer_or_wallet_key(self):
        fake=Mock();fake.async_client.make_authenticated_request=AsyncMock(return_value={'code':'SUCCESS','data':raw_asset()})
        r=view.Reader(fixtures.configuration(),client=fake)
        await r.get('asset')
        fake.async_client.make_authenticated_request.assert_awaited_once_with(method='GET',path=view.READ_PATHS['asset'],params={'accountId':'123'})
        with self.assertRaises(view.AccountDataError):await r.get('withdraw')
        fake.async_client.make_authenticated_request.assert_awaited_once()
        fake_module=Mock();fake_module.Client=Mock(return_value=fake)
        with patch.dict('sys.modules',{'edgex_sdk':fake_module}):view.Reader(fixtures.configuration())
        kwargs=fake_module.Client.call_args.kwargs
        self.assertEqual(kwargs['trading_private_key'],'');self.assertEqual(kwargs['wallet_private_key'],'')

    async def test_history_quality_reason_is_fixed_and_never_echoes_response(self):
        page=raw_page(accountId='private-mismatched-account')
        with self.assertRaises(view.AccountDataError) as exc:
            view.history_page(page,'123',{},'positions',observed_ms=NOW,start_ms=NOW-view.DAY,end_ms=NOW)
        self.assertEqual(exc.exception.code,'ACCOUNT_ROW_MISMATCH')
        self.assertNotIn('private',str(exc.exception))

    async def test_reader_blank_history_fields_no_longer_block_collection(self):
        fake=Mock();fake.async_client.make_authenticated_request=AsyncMock(return_value={'code':'SUCCESS','data':raw_page(fillPrice='',realizePnl='')})
        result=await view.Reader(fixtures.configuration(),client=fake).history('positions',NOW-view.DAY,NOW)
        self.assertIsNone(result['items'][0]['price']);self.assertIsNone(result['items'][0]['realized_pnl_usdc'])
        self.assertEqual(len(result['items']),1)

    async def test_sdk_exception_is_sanitized(self):
        fake=Mock();fake.async_client.make_authenticated_request=AsyncMock(side_effect=RuntimeError('private-secret-response'))
        with self.assertRaises(view.AccountDataError) as exc:await view.Reader(fixtures.configuration(),client=fake).get('asset')
        self.assertNotIn('private',str(exc.exception))

    async def test_failed_collection_clears_old_asset_not_zero_and_atomic_history_window(self):
        store=view.Store();reader=Mock();reader.metadata=AsyncMock();reader.config=fixtures.configuration();reader.names={}
        reader.get=AsyncMock(return_value=raw_asset())
        page=view.history_page(raw_page(),'123',{},'positions',observed_ms=NOW,start_ms=NOW-30*view.DAY,end_ms=NOW)
        reader.history=AsyncMock(return_value=page)
        await store.collect(reader,now_ms=NOW)
        self.assertIsNotNone(store.asset);self.assertEqual(store.window,(NOW-30*view.DAY,NOW))
        reader.get=AsyncMock(side_effect=RuntimeError('secret'))
        reader.history=AsyncMock(side_effect=RuntimeError('secret'))
        await store.collect(reader,now_ms=NOW+1)
        self.assertIsNone(store.asset);self.assertEqual(store.histories,{})
        self.assertEqual(store.config_report(owner_bound=True,now_ms=NOW+1)['collection_state'],'UNAVAILABLE')
        self.assertNotIn('secret',json.dumps(store.config_report(owner_bound=True,now_ms=NOW+1)))
