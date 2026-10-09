"""Explicit browser controls against a synthetic exchange; never live toggles."""
import asyncio
import json
import time
import unittest
import uuid
from unittest.mock import patch
from analysis_terminal import execution_controls as web, account_view as view, live_execution as live, server
from analysis_terminal import test_account_view as account_fixtures
from analysis_terminal import test_live_execution as fixtures


class ControlApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        account_fixtures.APIAuthorityTests.setUp(self)
        self.monotonic=100
        self.controller=web.Controller(clock=lambda:self.monotonic)
        p=patch.object(web,'CONTROLLER',self.controller);p.start();self.addCleanup(p.stop)
        self.controller.worker_available=True
        self.now=int(time.time()*1000)
        self.config=server._live_execution_config
        self.db=server._live_execution_db
        self.exchange=fixtures.Exchange(lambda:self.now)
        self.engine=live.Engine(self.db,self.config,self.exchange,clock=lambda:self.now)

    async def header(self):
        r=await self.client.post('/api/execution/session',json={'subscription':self.sub})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['scope'],'NEW_ENTRY_CONTROL');self.assertFalse(r.json()['read_only'])
        self.assertEqual(r.json()['expires_in_seconds'],120)
        return {'Authorization':'Bearer '+r.json()['control_token']}

    async def send(self, action='arm',headers=None,request_id=None,epoch=None):
        with self.db() as c:s=live.state(c)
        body={'action':action,'request_id':request_id or str(uuid.uuid4()),'expected_epoch':s.get('control_epoch',0) if epoch is None else epoch}
        r=await self.client.post('/api/execution/control',json=body,headers=headers or {})
        self.now=max(self.now,int(time.time()*1000))
        return r,body

    def state(self):
        with self.db() as c:return live.state(c)

    def records(self):
        with self.db() as c:return live.orders(c)

    async def test_no_authority_from_public_view_push_or_query_tokens(self):
        read=await account_fixtures.APIAuthorityTests.header(self)
        for headers in ({},read,{'Authorization':'Bearer '+server.push_security.management_token(self.sub)}):
            r,_=await self.send(headers=headers);self.assertEqual(r.status_code,401)
            self.assertEqual((await self.client.get('/api/execution/status',headers=headers)).status_code,401)
        h=await self.header()
        self.assertEqual((await self.client.get('/api/account/positions',headers=h)).status_code,401)
        token=h['Authorization'][7:]
        self.assertEqual((await self.client.get('/api/execution/status?token='+token)).status_code,401)
        self.assertFalse(self.state()['armed']);self.assertEqual(self.exchange.creates,[])

    async def test_bootstrap_and_status_do_not_change_state_or_show_money(self):
        with self.db() as c:before=list(c.iterdump())
        h=await self.header();r=await self.client.get('/api/execution/status',headers=h)
        self.assertEqual(r.status_code,200);self.assertEqual(r.headers['cache-control'],'no-store')
        for field in ('account_id','equity','balance','fingerprint','device_hash','signer_key'):
            self.assertNotIn(field,r.text)
        with self.db() as c:self.assertEqual(list(c.iterdump()),before)
        self.assertTrue(r.json()['can_request_on']);self.assertFalse(r.json()['armed'])
        self.assertEqual(r.json()['on_blockers'],[])

    async def test_ownership_block_is_visible_read_only_and_cannot_be_overridden(self):
        h=await self.header()
        with self.db() as c:
            live.save(c,dict(setup_id='private-setup',status='OWNERSHIP_CONFLICT',
                             ownership_quarantined=True,entry_client_id='private-order'),self.now)
            before=list(c.iterdump())
        r=await self.client.get('/api/execution/status',headers=h)
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['on_blockers'],['OWNERSHIP_UNCERTAIN'])
        self.assertEqual(r.json()['active_orders'],1)
        self.assertFalse(r.json()['can_request_on'])
        self.assertNotIn('private-',r.text)
        self.assertEqual((await self.send(headers=h))[0].status_code,409)
        with self.db() as c:self.assertEqual(list(c.iterdump()),before)
        self.assertFalse(self.state()['armed']);self.assertEqual(self.exchange.creates,[])
        self.assertEqual(self.exchange.cancels,[])

    async def test_unresolved_record_and_configuration_reasons_are_allowlisted(self):
        h=await self.header()
        with self.db() as c:
            live.save(c,dict(setup_id='private-position',status='PROTECTED'),self.now)
        self.controller.worker_available=False
        self.config.mode='READ_ONLY'
        r=await self.client.get('/api/execution/status',headers=h)
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['on_blockers'],['WORKER_UNAVAILABLE','LIVE_MODE_REQUIRED','UNRESOLVED_BOT_RECORDS'])
        self.assertEqual((await self.send(headers=h))[0].status_code,409)
        with self.db() as c:
            before=list(c.iterdump())
            state=self.controller.status(c,self.config,now_ms=self.now)
            self.assertEqual(list(c.iterdump()),before)
        self.assertEqual(state['on_blockers'],['WORKER_UNAVAILABLE','LIVE_MODE_REQUIRED','UNRESOLVED_BOT_RECORDS'])
        self.assertFalse(state['can_request_on'])
        self.assertNotIn('private-',json.dumps(state))

    async def test_configuration_and_already_armed_reasons_do_not_change_runtime(self):
        await self.engine.arm()
        with self.db() as c:
            before=list(c.iterdump());s=self.controller.status(c,self.config,now_ms=self.now)
            self.assertEqual(s['on_blockers'],['ALREADY_ARMED'])
            self.assertTrue(s['armed']);self.assertFalse(s['can_request_on'])
            self.assertEqual(list(c.iterdump()),before)
        with patch.object(self.config,'errors',return_value=['PRIVATE_CONFIGURATION_DETAIL']):
            with self.db() as c:s=self.controller.status(c,self.config,now_ms=self.now)
        self.assertEqual(s['on_blockers'],['CONFIGURATION_REQUIRED','ALREADY_ARMED'])
        self.assertNotIn('PRIVATE_CONFIGURATION_DETAIL',json.dumps(s))
        self.assertEqual(self.exchange.creates,[])

    async def test_forged_and_new_devices_cannot_enable_controls(self):
        other=account_fixtures.push_fixtures.subscription('https://web.push.apple.com/new')
        server._save_push_subscription(server.PushSubscriptionRequest(subscription=other));server._init_db()
        for supplied in (other,account_fixtures.push_fixtures.subscription(auth=b'0123456789abcdef')):
            self.assertEqual((await self.client.post('/api/execution/session',json={'subscription':supplied})).status_code,403)
        self.assertFalse(self.state()['armed'])

    async def test_cross_site_big_body_and_unknown_actions_block(self):
        h=await self.header()
        body={'action':'arm','request_id':str(uuid.uuid4()),'expected_epoch':0}
        self.assertEqual((await self.client.post('/api/execution/control',json=body,headers={**h,'Origin':'https://evil.test'})).status_code,403)
        self.assertEqual((await self.client.post('/api/execution/session',content=b'x'*16385)).status_code,413)
        for changes in ({'action':'close'},{'action':'withdraw'},{'request_id':'x'*36},{'expected_epoch':True},{'expected_epoch':2**63},{'equity':1000}):
            self.assertEqual((await self.client.post('/api/execution/control',json={**body,**changes},headers=h)).status_code,422)
        self.assertEqual(self.exchange.creates,[])

    async def test_queued_on_preflight_once_only_no_old_signal_or_order_sent(self):
        h=await self.header();r,body=await self.send(headers=h)
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()['latest_request']['status'],'QUEUED')
        self.assertFalse(self.state()['armed']);self.assertEqual(self.exchange.creates,[])
        await self.controller.process(self.engine)
        self.assertTrue(self.state()['armed']);armed_ms=self.state()['armed_ms']
        with self.db() as c:
            self.assertEqual(c.execute('SELECT status FROM execution_web_controls').fetchone()[0],'DONE')
            self.assertNotIn(h['Authorization'][7:],json.dumps(list(c.iterdump())))
        r,_=await self.send(headers=h,request_id=body['request_id'],epoch=body['expected_epoch'])
        self.assertEqual(r.status_code,200)
        await self.controller.process(self.engine)
        self.assertEqual(self.state()['armed_ms'],armed_ms);self.assertEqual(self.exchange.creates,[])
        self.assertEqual((await self.send(headers=h))[0].status_code,409,'ON while ON must not reset baseline')

    async def test_off_commits_without_worker_and_never_closes_or_cancels_positions(self):
        await self.engine.arm();h=await self.header();self.controller.worker_available=False
        r,body=await self.send('pause',headers=h,epoch=999)
        self.assertEqual(r.status_code,200);self.assertFalse(self.state()['armed'])
        self.assertTrue(r.json()['protective_management_enabled'])
        self.assertEqual(self.exchange.creates,[]);self.assertEqual(self.exchange.cancels,[])
        self.assertEqual(self.config.mode,'LIVE')
        self.assertEqual((await self.send(headers=h))[0].status_code,409)

    async def test_off_cancels_queued_on_before_worker(self):
        h=await self.header();await self.send(headers=h)
        await self.send('pause',headers=h)
        await self.controller.process(self.engine)
        self.assertFalse(self.state()['armed']);self.assertEqual(self.exchange.creates,[])
        with self.db() as c:self.assertEqual(c.execute('SELECT code FROM execution_web_controls ORDER BY rowid LIMIT 1').fetchone()[0],'SUPERSEDED_BY_OFF')

    async def test_off_interrupts_on_preflight_and_late_result_cannot_rearm(self):
        h=await self.header();await self.send(headers=h)
        started=asyncio.Event();release=asyncio.Event();original=self.exchange.metadata
        async def delayed():started.set();await release.wait();return await original()
        self.exchange.metadata=delayed
        task=asyncio.create_task(self.controller.process(self.engine));await asyncio.wait_for(started.wait(),2)
        r,_=await self.send('pause',headers=h,epoch=0);self.assertEqual(r.status_code,200)
        release.set();await task
        self.assertFalse(self.state()['armed']);self.assertEqual(self.exchange.creates,[])

    async def test_revocation_and_expiry_at_final_preflight_gate(self):
        for kind in ('logout','expiry','delete_device','policy_change','mode_change'):
            with self.subTest(kind=kind):
                # Direct synthetic authorization avoids endpoint bootstrap rate limit.
                with self.db() as c:
                    if kind=='delete_device':server._save_push_subscription(server.PushSubscriptionRequest(subscription=self.sub))
                    val=self.controller.create(c,self.config,self.sub)
                h={'Authorization':'Bearer '+val['control_token']}
                r,_=await self.send(headers=h);self.assertEqual(r.status_code,200)
                started=asyncio.Event();release=asyncio.Event();original=fixtures.Exchange.metadata
                async def delayed():started.set();await release.wait();return await original(self.exchange)
                self.exchange.metadata=delayed
                task=asyncio.create_task(self.controller.process(self.engine));await asyncio.wait_for(started.wait(),2)
                if kind=='logout':self.assertEqual((await self.client.post('/api/execution/logout',headers=h,json={})).status_code,200)
                elif kind=='expiry':self.monotonic+=120
                elif kind=='delete_device':server._delete_push_subscription(self.sub['endpoint'])
                elif kind=='policy_change':self.config.risk_pct='3'
                else:self.config.mode='READ_ONLY'
                release.set();await task
                self.assertFalse(self.state()['armed']);self.assertEqual(self.exchange.creates,[])
                if kind=='delete_device':server._save_push_subscription(server.PushSubscriptionRequest(subscription=self.sub))

    async def test_replay_mismatch_and_cli_id_collision_cannot_mutate(self):
        h=await self.header();r,body=await self.send(headers=h)
        r,_=await self.send('pause',headers=h,request_id=body['request_id'],epoch=body['expected_epoch'])
        self.assertEqual(r.status_code,409)
        token=str(uuid.uuid4())
        with self.db() as c:c.execute("INSERT INTO live_execution_controls VALUES(?,'pause','DONE',?, ?,NULL)",(token,self.now,self.now))
        r,_=await self.send('pause',headers=h,request_id=token)
        self.assertEqual(r.status_code,409)

    async def test_stale_epoch_expired_queue_and_missing_auth_never_start(self):
        h=await self.header();self.assertEqual((await self.send(headers=h,epoch=999))[0].status_code,409)
        for kind in ('epoch','stale','auth'):
            r,_=await self.send(headers=h);self.assertEqual(r.status_code,200)
            if kind=='epoch':
                with self.db() as c:live.pause(c,'MANUAL_PAUSE')
            elif kind=='stale':self.now+=30000
            else:self.controller.pending_auth.clear()
            await self.controller.process(self.engine)
            self.assertFalse(self.state()['armed']);self.assertEqual(self.exchange.creates,[])

    async def test_restart_refuses_queue_and_interrupted_on_without_replay(self):
        h=await self.header();await self.send(headers=h)
        with self.db() as c:
            before=live.orders(c);other=web.Controller();web.initialize(c);other.recover(c,now_ms=self.now)
            self.assertEqual(before,live.orders(c));self.assertEqual(c.execute('SELECT status FROM execution_web_controls').fetchone()[0],'REFUSED')
        await other.process(self.engine);self.assertFalse(self.state()['armed'])
        await self.send(headers=h)
        with self.db() as c:
            c.execute("UPDATE execution_web_controls SET status='PROCESSING' WHERE status='QUEUED'")
            c.execute("INSERT INTO live_execution_controls SELECT request_id,action,'PROCESSING',created_ms,NULL,NULL FROM execution_web_controls WHERE status='PROCESSING'")
            s=live.state(c);s['armed']=True;live.save_state(c,s)
            other.recover(c,now_ms=self.now)
            self.assertEqual(tuple(c.execute("SELECT status,code FROM live_execution_controls ORDER BY rowid DESC LIMIT 1").fetchone()),('REFUSED','CONTROL_INTERRUPTED'))
        self.assertFalse(self.state()['armed']);self.assertEqual(self.exchange.creates,[])

    async def test_paused_protected_position_is_managed_and_on_refused_until_resolved(self):
        self.now=fixtures.NOW-60000
        await self.engine.arm();self.now+=60000
        row=fixtures.candidate(latest_15m_time_ms=self.now-30000-live.STEP)
        await self.engine.cycle([row],snapshot_ms=self.now)
        r=self.records()[0];self.assertEqual(r['status'],'PROTECTED')
        h=await self.header();res,_=await self.send('pause',headers=h);self.assertEqual(res.status_code,200)
        self.assertEqual((await self.send(headers=h))[0].status_code,409)
        self.exchange.fill_exit(r,'sl');await self.engine.cycle([row],snapshot_ms=self.now)
        self.assertEqual(self.records()[0]['status'],'CLOSED');self.assertFalse(self.state()['armed'])
        self.assertEqual(self.exchange.creates,['entry','sl','tp'])

    async def test_off_during_entry_send_keeps_fill_protection_and_never_retries(self):
        self.now=fixtures.NOW-60000
        await self.engine.arm();self.now=fixtures.NOW
        row=fixtures.candidate()
        started=asyncio.Event();release=asyncio.Event();original=self.exchange.create
        async def delayed(r,kind,**kwargs):
            if kind=='entry':started.set();await release.wait()
            return await original(r,kind,**kwargs)
        self.exchange.create=delayed
        task=asyncio.create_task(self.engine.cycle([row],snapshot_ms=self.now))
        await asyncio.wait_for(started.wait(),2)
        h=await self.header();res,_=await self.send('pause',headers=h)
        self.assertEqual(res.status_code,200);self.assertFalse(self.state()['armed'])
        # Freeze the synthetic clock back to the submitted IOC window.
        self.now=fixtures.NOW;release.set();await task
        self.assertEqual(self.records()[0]['status'],'PROTECTED')
        self.assertEqual(self.exchange.creates,['entry','sl','tp'])
        await self.engine.cycle([row],snapshot_ms=self.now)
        self.assertEqual(self.exchange.creates,['entry','sl','tp'])
        self.assertFalse(self.state()['armed'])

    async def test_epoch_is_checked_in_engine_before_pausing_not_just_queue(self):
        with self.db() as c:live.pause(c,'MANUAL_PAUSE')
        with self.assertRaisesRegex(live.ExecutionError,'STALE_CONTROL_STATE'):
            await self.engine.arm(expected_epoch=0)
        self.assertFalse(self.state()['armed']);self.assertEqual(self.state()['control_epoch'],1)
        self.assertEqual(self.exchange.creates,[])
