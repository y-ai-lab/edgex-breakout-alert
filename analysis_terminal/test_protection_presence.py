"""Simulated missing active protection and read-only owner diagnostics."""
import copy
import json
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
from analysis_terminal import test_live_execution as f, test_account_view as account
from analysis_terminal import account_view as view, server


class PresenceTests(unittest.IsolatedAsyncioTestCase):
    setUp = f.ExecutionTests.setUp
    enter = f.ExecutionTests.enter
    armed = f.ExecutionTests.armed
    records = f.ExecutionTests.records
    state = f.ExecutionTests.state

    async def test_lookup_untriggered_but_missing_from_active_never_protected(self):
        original = self.exchange.active_orders
        async def missing():
            return [o for o in await original() if o['type'] != 'STOP_MARKET']
        self.exchange.active_orders = missing
        r = await self.enter()
        self.assertNotEqual(r['status'], 'PROTECTED')
        self.assertEqual(r['protection_check_error'], 'PROTECTION_NOT_ACTIVE')
        self.assertFalse(self.state()['armed'])
        self.assertEqual(self.exchange.creates, ['entry', 'sl', 'tp', 'close'])

    async def test_disappeared_tp_detected_after_previous_success_without_resend(self):
        r = await self.enter()
        original = self.exchange.active_orders
        async def missing():
            return [o for o in await original() if o['type'] != 'TAKE_PROFIT_MARKET']
        self.exchange.active_orders = missing
        self.now += 5000
        await self.engine.cycle()
        self.assertNotEqual(self.records()[0]['status'], 'PROTECTED')
        self.assertEqual(self.exchange.creates, ['entry', 'sl', 'tp', 'close'])
        await self.engine.cycle()
        self.assertEqual(self.exchange.creates.count('close'), 1)
        self.assertEqual(self.exchange.creates.count('tp'), 1)

    async def test_duplicate_and_mismatched_active_orders_rejected(self):
        r = await self.enter()
        active = await self.exchange.active_orders()
        for field, value in [('id','999'),('contractId','2'),('side','BUY'),
                             ('status','CANCELING'),('size','999'),('type','LIMIT'),
                             ('reduceOnly',False),('triggerPrice','1'),
                             ('triggerPriceType','INDEX_PRICE'),('expireTime','1')]:
            changed=copy.deepcopy(active);changed[0][field]=value
            with self.subTest(field=field), self.assertRaises(f.ExecutionError):
                f.live.verify_active_protection(changed,r)
        with self.assertRaises(f.ExecutionError):f.live.verify_active_protection(active+[active[0]],r)
        minimal=[{k:v for k,v in o.items() if k in {'id','clientOrderId','contractId','side','type','status','size'}} for o in active]
        f.live.verify_active_protection(minimal,r)

    async def test_ack_id_mismatch_rejected(self):
        r = await self.enter()
        o = await self.exchange.order(r['sl_client_id']);o['id']='999'
        with self.assertRaisesRegex(f.ExecutionError,'ORDER_ID_MISMATCH'):
            f.live.verify_order(o,r,'sl')

    async def test_quarantine_is_not_reverified_rearmed_or_closed(self):
        self.config.account_policy='COEXISTING_CONTRACTS'
        r = await self.enter()
        self.exchange.external_orders=[dict(accountId='123',contractId=r['contract_id'],clientOrderId='manual')]
        with self.assertRaises(f.ExecutionError):await self.engine.cycle()
        before=copy.deepcopy(self.records())
        await self.engine.cycle()
        self.assertEqual(self.records(),before)
        self.assertEqual(self.exchange.creates,['entry','sl','tp'])
        self.assertEqual(len(self.exchange.external_orders),1)
        self.assertFalse(self.state()['armed'])


def conditional(**changes):
    return dict(dict(id='1',accountId='123',contractId='1',clientOrderId='private-client',
        type='STOP_MARKET',side='SELL',status='UNTRIGGERED',size='.01',
        triggerPrice='50000',reduceOnly=True,isPositionTpsl=False,privateField='secret'),**changes)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    def reader(self,pages):
        client=Mock();client.async_client.make_authenticated_request=AsyncMock(
            side_effect=[dict(code='SUCCESS',data=p) for p in pages])
        reader=view.Reader(f.configuration(),client=client);reader.names={'1':'BTCUSDC'}
        return reader,client

    async def test_complete_paginated_list_get_only_sanitized_and_unknown_flags(self):
        reader,client=self.reader([
            dict(dataList=[conditional()],nextPageOffsetData='cursor'),
            dict(dataList=[conditional(id='2',type='TAKE_PROFIT_MARKET',reduceOnly=None,isPositionTpsl=None)],nextPageOffsetData='')])
        r=await reader.conditional_orders()
        self.assertTrue(r['complete']);self.assertFalse(r['protection_guaranteed'])
        self.assertEqual([o['kind'] for o in r['items']],['SL','TP'])
        self.assertIsNone(r['items'][1]['reduce_only'])
        for secret in ('accountId','contractId','clientOrderId','private-client','secret','cursor'):
            self.assertNotIn(secret,json.dumps(r))
        calls=client.async_client.make_authenticated_request.await_args_list
        self.assertEqual(len(calls),2)
        for call in calls:self.assertEqual(call.kwargs['method'],'GET')
        self.assertEqual(calls[1].kwargs['params']['offsetData'],'cursor')

    async def test_empty_complete_is_distinct_from_failure(self):
        reader,_=self.reader([dict(dataList=[],nextPageOffsetData='')])
        self.assertEqual((await reader.conditional_orders())['items'],[])
        reader,_=self.reader([{}])
        with self.assertRaises(view.AccountDataError):await reader.conditional_orders()

    async def test_invalid_account_flag_decimal_and_side_fail_without_partial_result(self):
        for changes in [dict(accountId='999'),dict(reduceOnly='true'),dict(size='NaN'),
                        dict(triggerPrice='-1'),dict(contractId='0'),dict(status='CANCELED'),dict(side='UNKNOWN')]:
            reader,_=self.reader([dict(dataList=[conditional(**changes)],nextPageOffsetData='')])
            with self.subTest(changes=changes),self.assertRaises(view.AccountDataError):await reader.conditional_orders()

    async def test_duplicate_ids_loop_and_page_limit_never_report_complete(self):
        for pages in [
            [dict(dataList=[conditional(),conditional()],nextPageOffsetData='')],
            [dict(dataList=[],nextPageOffsetData='same')]*2,
            [dict(dataList=[],nextPageOffsetData=str(i+1)) for i in range(20)]]:
            reader,_=self.reader(pages)
            with self.assertRaises(view.AccountDataError):await reader.conditional_orders()


class AuthorityTests(unittest.IsolatedAsyncioTestCase):
    setUp = account.APIAuthorityTests.setUp
    header = account.APIAuthorityTests.header

    async def test_missing_auth_does_not_query_exchange_and_post_forbidden(self):
        reader=Mock();reader.conditional_orders=AsyncMock()
        view.STORE.reader=reader
        self.assertEqual((await self.client.get('/api/account/conditional-orders')).status_code,401)
        reader.conditional_orders.assert_not_called()
        self.assertEqual((await self.client.post('/api/account/conditional-orders',headers=await self.header())).status_code,405)

    async def test_owner_only_read_does_not_mutate_ledger_and_no_store(self):
        header=await self.header()
        reader=Mock();reader.conditional_orders=AsyncMock(return_value=dict(
            source='EDGEX_ACTIVE_CONDITIONAL_ORDERS',observed_ms=int(time.time()*1000),
            read_only=True,complete=True,items=[],protection_guaranteed=False))
        view.STORE.reader=reader
        with server._db_connect() as conn:before=list(conn.iterdump())
        r=await self.client.get('/api/account/conditional-orders',headers=header)
        self.assertEqual(r.status_code,200);self.assertEqual(r.headers['cache-control'],'no-store')
        with server._db_connect() as conn:self.assertEqual(list(conn.iterdump()),before)
        self.assertEqual(reader.method_calls,[unittest.mock.call.conditional_orders()])

    async def test_revocation_during_query_blocks_response(self):
        header=await self.header()
        async def revoke():
            server._delete_push_subscription(self.sub['endpoint'])
            return dict(observed_ms=int(time.time()*1000))
        reader=Mock();reader.conditional_orders=AsyncMock(side_effect=revoke);view.STORE.reader=reader
        self.assertEqual((await self.client.get('/api/account/conditional-orders',headers=header)).status_code,403)

    async def test_unavailable_and_stale_are_not_empty_success_or_raw_error(self):
        header=await self.header()
        reader=Mock();view.STORE.reader=reader
        reader.conditional_orders=AsyncMock(side_effect=RuntimeError('private-secret'))
        r=await self.client.get('/api/account/conditional-orders',headers=header)
        self.assertEqual(r.status_code,503);self.assertNotIn('private-secret',r.text)
        reader.conditional_orders=AsyncMock(return_value=dict(observed_ms=int(time.time()*1000)-30000))
        self.assertEqual((await self.client.get('/api/account/conditional-orders',headers=header)).status_code,503)
