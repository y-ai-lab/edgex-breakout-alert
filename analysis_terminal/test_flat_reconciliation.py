"""Synthetic-only owner review: never live orders, cancellation or auto-arm."""
import asyncio
import copy
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from analysis_terminal import live_execution as live, execution_health as health
from analysis_terminal import flat_reconciliation as review
from analysis_terminal import test_execution_controls as controls
from analysis_terminal import test_live_execution as fixtures


class FlatReviewTests(unittest.IsolatedAsyncioTestCase):
    setUp = controls.ControlApiTests.setUp
    header = controls.ControlApiTests.header
    send = controls.ControlApiTests.send
    state = controls.ControlApiTests.state
    records = controls.ControlApiTests.records

    async def quarantined(self, flat=True):
        self.config.account_policy='COEXISTING_CONTRACTS'
        self.now=fixtures.NOW-60000
        await self.engine.arm();self.now+=60000
        await self.engine.cycle([fixtures.candidate()],snapshot_ms=self.now)
        r=self.records()[0];self.assertEqual(r['status'],'PROTECTED')
        await self.engine.quarantine(r)
        if flat:self.exchange.positions[r['contract_id']]=Decimal(0)
        self.creates_before=self.exchange.creates.copy();self.cancels_before=self.exchange.cancels.copy()
        return await self.header()

    async def run_review(self,h):
        r,_=await self.send('reconcile_flat',headers=h);self.assertEqual(r.status_code,200)
        await self.controller.process(self.engine)
        with self.db() as c:
            row=c.execute('SELECT status,code FROM execution_web_controls ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertEqual(self.exchange.creates,self.creates_before)
        self.assertEqual(self.exchange.cancels,self.cancels_before)
        self.assertFalse(self.state()['armed'])
        return tuple(row)

    async def test_explicit_flat_review_preserves_original_fills_and_risk_without_financial_result(self):
        h=await self.quarantined();before=self.records()[0]
        self.exchange.external_orders=[dict(contractId='99',clientOrderId='manual-other')]
        self.exchange.positions['99']=Decimal(2)
        self.assertEqual(await self.run_review(h),('DONE','FLAT_REVIEW_DONE'))
        after=self.records()[0]
        for k,v in before.items():
            if k!='status':self.assertEqual(after[k],v,k)
        self.assertTrue(after['ownership_quarantined']);self.assertFalse(after['financial_outcome_verified'])
        self.assertNotIn('net_pnl_usdc',after);self.assertEqual(after['status'],'EXTERNAL_FLAT_VERIFIED')
        audit=health.report([after],{'mode':'LIVE','status':'PAUSED','reason':'MANUAL_PAUSE','last_success_ms':self.now},now_ms=self.now)
        self.assertEqual(audit['active_records'],0);self.assertEqual(audit['counts']['closed'],0)
        self.assertEqual(audit['counts']['external_flat_verified'],1)
        with self.db() as c:
            status=self.controller.status(c,self.config,now_ms=self.now)
            self.assertTrue(status['can_request_on'])
            self.assertGreater(c.execute('SELECT COUNT(*) FROM live_execution_events').fetchone()[0],1)
        self.assertEqual(self.state()['reason'],'MANUAL_PAUSE')

    async def test_open_position_and_remaining_manual_orders_refuse_without_mutation(self):
        h=await self.quarantined(flat=False);before=copy.deepcopy(self.records())
        self.assertEqual(await self.run_review(h),('REFUSED','FLAT_REVIEW_POSITION_OR_ORDERS_REMAIN'))
        self.exchange.positions[before[0]['contract_id']]=Decimal(0)
        self.exchange.external_orders=[dict(contractId=before[0]['contract_id'],clientOrderId='manual-stop')]
        self.assertEqual(await self.run_review(h),('REFUSED','FLAT_REVIEW_POSITION_OR_ORDERS_REMAIN'))
        self.assertEqual(self.records(),before)
        self.assertEqual((await self.send('arm',headers=h))[0].status_code,409)

    async def test_missing_active_pages_stale_account_and_missing_order_refuse(self):
        h=await self.quarantined();before=copy.deepcopy(self.records())
        for method,returns in [('active_orders',None),('account',{'positions':{},'observed_ms':0}),('order',None)]:
            with patch.object(self.exchange,method,AsyncMock(return_value=returns)):
                result=await self.run_review(h)
                self.assertEqual(result[0],'REFUSED');self.assertEqual(self.records(),before)

    async def test_wrong_order_identity_changed_fill_and_nonterminal_order_refuse(self):
        h=await self.quarantined();before=copy.deepcopy(self.records());r=before[0]
        key=r['entry_client_id'];saved=copy.deepcopy(self.exchange.ledger[key])
        for field,value in [('id','9999'),('contractId','77'),('cumFillSize','0'),('status','OPEN')]:
            self.exchange.ledger[key]=dict(saved,**{field:value})
            self.assertEqual((await self.run_review(h))[0],'REFUSED');self.assertEqual(self.records(),before)
        self.exchange.ledger[key]=saved

    async def test_second_flat_check_catches_new_position(self):
        h=await self.quarantined();before=copy.deepcopy(self.records());original=self.exchange.account;calls=0
        async def account():
            nonlocal calls
            calls+=1
            if calls==2:self.exchange.positions[before[0]['contract_id']]=Decimal(1)
            return await original()
        with patch.object(self.exchange,'account',account):
            self.assertEqual(await self.run_review(h),('REFUSED','FLAT_REVIEW_POSITION_OR_ORDERS_REMAIN'))
        self.assertEqual(self.records(),before)

    async def test_off_expiry_revocation_and_ledger_changes_block_late_review(self):
        for change in ('off','expiry','revoke','policy','ledger'):
            with self.subTest(change=change):
                # Separate instance avoids accumulating controls and state changes.
                case=FlatReviewTests();case.setUp()
                try:
                    h=await case.quarantined();before=copy.deepcopy(case.records())
                    res,_=await case.send('reconcile_flat',headers=h);self.assertEqual(res.status_code,200)
                    entered=asyncio.Event();release=asyncio.Event();original=case.exchange.order
                    async def delayed(cid):entered.set();await release.wait();return await original(cid)
                    case.exchange.order=delayed
                    task=asyncio.create_task(case.controller.process(case.engine));await asyncio.wait_for(entered.wait(),2)
                    if change=='off':await case.send('pause',headers=h)
                    elif change=='expiry':case.monotonic+=120
                    elif change=='revoke':case.controller.sessions.items.clear()
                    elif change=='policy':case.config.risk_pct='2'
                    else:
                        with case.db() as c:
                            r=live.orders(c)[0];r['unrelated_audit_field']=True;live.save(c,r,case.now)
                    release.set();await task
                    self.assertEqual(case.records()[0]['status'],'OWNERSHIP_CONFLICT')
                    self.assertFalse(case.state()['armed']);self.assertEqual(case.exchange.creates,case.creates_before)
                    self.assertEqual(case.exchange.cancels,case.cancels_before)
                finally:case.doCleanups()

    async def test_read_token_and_nonquarantined_records_cannot_review(self):
        h=await self.quarantined();read=await controls.account_fixtures.APIAuthorityTests.header(self)
        self.assertEqual((await self.send('reconcile_flat',headers=read))[0].status_code,401)
        with self.db() as c:
            r=live.orders(c)[0];r['ownership_quarantined']=False;live.save(c,r,self.now)
        self.assertEqual((await self.send('reconcile_flat',headers=h))[0].status_code,409)
        self.assertFalse(review.eligible([dict(status='OWNERSHIP_CONFLICT',ownership_quarantined=True,isolated_contract=True,filled_size='NaN')]))

    async def test_review_request_is_idempotent_and_restart_never_resumes_it(self):
        h=await self.quarantined();before=copy.deepcopy(self.records())
        response,body=await self.send('reconcile_flat',headers=h)
        self.assertEqual(response.status_code,200)
        response,_=await self.send('reconcile_flat',headers=h,request_id=body['request_id'],epoch=body['expected_epoch'])
        self.assertEqual(response.status_code,200)
        with self.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM execution_web_controls').fetchone()[0],1)
            self.controller.recover(c,now_ms=self.now)
        await self.controller.process(self.engine)
        self.assertEqual(self.records(),before);self.assertFalse(self.state()['armed'])
        self.assertEqual(self.exchange.creates,self.creates_before);self.assertEqual(self.exchange.cancels,self.cancels_before)
