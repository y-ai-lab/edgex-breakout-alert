"""Synthetic account sizing, privacy, freshness and read-only HTTP contract."""
import copy
from decimal import Decimal as D
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from analysis_terminal import account_risk as risk, account_view as view, server
from analysis_terminal import test_account_view as fixtures


def config():
    c = fixtures.fixtures.configuration('OFF')
    c.risk_pct='3';c.max_risk_usdc='ACCOUNT_RISK_PCT';c.max_notional_usdc='ACCOUNT_EQUITY'
    return c


def snapshot(equity='100', available='80'):
    return {'balance':{'equity_usdc':equity,'available_usdc':available},'observed_ms':123,'snapshot_age_seconds':0}


class MathTests(unittest.TestCase):
    def calculate(self, **values):
        return risk.plan(server.AccountRiskRequest(entry=100,stop=90,target=120,**values),snapshot(),config())

    def test_costs_floor_and_budget_long(self):
        r=self.calculate(step_size=.01)
        unit=D('100.02')-D('89.982')+(D('100.02')+D('89.982'))*D('.0005')
        self.assertEqual(D(r['risk_budget']),D(3));self.assertEqual(D(r['size']),D('.29'))
        self.assertEqual(D(r['max_loss']),D('.29')*unit)
        self.assertLess(D(r['max_loss']),D(3));self.assertLess(D(r['cost_adjusted_rr']),D(2))
        self.assertFalse(r['quote_verified']);self.assertFalse(r['funding_included']);self.assertFalse(r['loss_cap_guaranteed'])

    def test_short_adverse_prices_and_signed_negative_target_reward(self):
        r=risk.plan(server.AccountRiskRequest(entry=100,stop=110,target=80),snapshot(),config())
        unit=D('110.022')-D('99.98')+(D('110.022')+D('99.98'))*D('.0005')
        self.assertAlmostEqual(float(r['size']),float(D(3)/unit));self.assertLess(D(r['cost_adjusted_rr']),D(2))
        r=risk.plan(server.AccountRiskRequest(entry=100,stop=110,target=99.99),snapshot(),config())
        self.assertLess(D(r['target_profit']),0)

    def test_available_and_one_times_equity_cap_not_leverage_multiplier(self):
        q=server.AccountRiskRequest(entry=100,stop=99.999,leverage=50,step_size=.001)
        for available,limit in [('5',D(5)),('500',D(100))]:
            r=risk.plan(q,snapshot(available=available),config())
            self.assertTrue(r['margin_capped']);self.assertLessEqual(D(r['notional'])*D('1.0005'),limit)
            self.assertLessEqual(D(r['max_loss']),D(3))

    def test_budget_reference_is_cost_adjusted_and_floor_never_exceeds_budget(self):
        r=self.calculate(step_size=.01,leverage=10)
        self.assertEqual(D(r['budget_reference_size']),D('.29'))
        self.assertEqual(D(r['budget_reference_loss_usdc']),D(r['max_loss']))
        self.assertLessEqual(D(r['budget_reference_loss_usdc']),D(r['risk_budget']))
        self.assertTrue(r['budget_reference_within_limits'])
        self.assertEqual(D(r['budget_reference_margin_usdc']),D(r['budget_reference_notional_usdc'])/10)
        self.assertEqual(D(r['unused_risk_budget_usdc']),D(r['risk_budget'])-D(r['max_loss']))
        self.assertIn('ORDER_STEP_ROUNDING',r['sizing_constraints'])

    def test_full_budget_reference_shows_notional_policy_constraint_without_increasing_size(self):
        q=server.AccountRiskRequest(entry=100,stop=99,leverage=10,step_size=.01)
        r=risk.plan(q,snapshot(),config())
        self.assertGreater(D(r['budget_reference_size']),D(r['size']))
        self.assertLessEqual(D(r['notional'])*D('1.0005'),D(80))
        self.assertFalse(r['budget_reference_within_limits'])
        self.assertIn('NOTIONAL_POLICY_LIMIT',r['sizing_constraints'])
        self.assertLess(D(r['risk_budget_used_pct']),100)
        self.assertLessEqual(D(r['budget_reference_loss_usdc']),D(3))

    def test_constraints_distinguish_maximum_minimum_and_zero_available(self):
        r=self.calculate(max_order_size=.05)
        self.assertIn('MAX_ORDER_SIZE',r['sizing_constraints']);self.assertFalse(r['budget_reference_within_limits'])
        r=self.calculate(min_order_size=.5)
        self.assertIn('BELOW_MIN_ORDER_SIZE',r['sizing_constraints']);self.assertFalse(r['budget_reference_within_limits'])
        r=risk.plan(server.AccountRiskRequest(entry=100,stop=90),snapshot(available='0'),config())
        self.assertEqual(D(r['size']),0);self.assertGreater(D(r['budget_reference_size']),0)
        self.assertFalse(r['budget_reference_within_limits']);self.assertEqual(D(r['unused_risk_budget_usdc']),D(3))

    def test_lower_requested_risk_and_configured_caps_never_expand_policy(self):
        c=config();c.risk_pct='1';c.max_risk_usdc='.5';c.max_notional_usdc='4'
        r=risk.plan(server.AccountRiskRequest(entry=100,stop=90,risk_pct=2),snapshot(),c)
        self.assertEqual(D(r['risk_budget']),D('.5'));self.assertEqual(D(r['applied_risk_pct']),D('.5'))
        self.assertLessEqual(D(r['notional'])*D('1.0005'),D(4))
        self.assertEqual(D(self.calculate(risk_pct=.1)['risk_budget']),D('.1'))

    def test_zero_available_and_minimum_order_are_zero_reference_size(self):
        r=risk.plan(server.AccountRiskRequest(entry=100,stop=90),snapshot(available='0'),config())
        self.assertEqual(D(r['size']),0);self.assertEqual(D(r['max_loss']),0)
        r=self.calculate(min_order_size=.5)
        self.assertTrue(r['below_min_order']);self.assertEqual(D(r['size']),0)
        r=self.calculate(max_order_size=.05,step_size=.01)
        self.assertTrue(r['max_order_capped']);self.assertEqual(D(r['size']),D('.05'))

    def test_missing_invalid_or_nonpositive_equity_reject_not_fallback(self):
        for eq,av in [('0','10'),('-1','10'),(None,'10'),('NaN','10'),('100',None),('100','-1'),('100','Infinity')]:
            with self.subTest(eq=eq,av=av),self.assertRaises(server.HTTPException) as e:
                risk.plan(server.AccountRiskRequest(entry=100,stop=90),snapshot(eq,av),config())
            self.assertEqual(e.exception.status_code,503)
        for field,value in [('risk_pct','4'),('fee_bps','NaN'),('max_notional_usdc','-1'),('max_risk_usdc','private-secret')]:
            c=config();setattr(c,field,value)
            with self.assertRaises(server.HTTPException) as e:risk.plan(server.AccountRiskRequest(entry=100,stop=90),snapshot(),c)
            self.assertNotIn('private',str(e.exception.detail))

    def test_request_forbids_balance_override_nonfinite_and_risk_over_three(self):
        for extra in ({'equity':1000},{'available':1000},{'risk_pct':3.01},{'entry':float('nan')},{'stop':float('inf')},{'step_size':0}):
            with self.subTest(extra=extra),self.assertRaises(ValidationError):server.AccountRiskRequest(**dict({'entry':100,'stop':90},**extra))
        for stop,target in [(100,None),(90,90),(110,120)]:
            with self.assertRaises(server.HTTPException):risk.plan(server.AccountRiskRequest(entry=100,stop=stop,target=target),snapshot(),config())


class PrivateApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.APIAuthorityTests.setUp(self)
        server._live_execution_config.risk_pct='3';server._live_execution_config.max_risk_usdc='ACCOUNT_RISK_PCT';server._live_execution_config.max_notional_usdc='ACCOUNT_EQUITY'

    async def header(self):
        return await fixtures.APIAuthorityTests.header(self)

    async def test_requires_owner_and_same_origin_no_public_balance(self):
        body={'entry':100,'stop':90,'target':120}
        self.assertEqual((await self.client.post('/api/account/risk',json=body)).status_code,401)
        headers=await self.header()
        r=await self.client.post('/api/account/risk',json=body,headers={**headers,'Origin':'https://evil.test'})
        self.assertEqual(r.status_code,403)
        r=await self.client.post('/api/account/risk',json={**body,'equity':1000},headers=headers)
        self.assertEqual(r.status_code,422)
        server._delete_push_subscription(self.sub['endpoint'])
        self.assertEqual((await self.client.post('/api/account/risk',json=body,headers=headers)).status_code,403)

    async def test_uses_equity_not_cash_private_no_store_and_no_mutation(self):
        headers=await self.header()
        with server._db_connect() as c:before=list(c.iterdump())
        original=copy.deepcopy(view.STORE.asset)
        with patch.object(server,'_live_execution_db',side_effect=AssertionError('must not touch order state')):
            r=await self.client.post('/api/account/risk',json={'entry':100,'stop':90,'target':120,'step_size':.01},headers=headers)
        self.assertEqual(r.status_code,200);self.assertEqual(r.headers['cache-control'],'no-store')
        self.assertEqual(D(r.json()['equity_usdc']),100);self.assertEqual(D(r.json()['risk_budget']),3)
        self.assertEqual(D(r.json()['available_usdc']),80);self.assertEqual(view.STORE.asset,original)
        with server._db_connect() as c:self.assertEqual(list(c.iterdump()),before)
        self.assertNotIn('accountId',r.text)

    async def test_exact_freshness_boundary_missing_and_revocation(self):
        headers=await self.header();observed=view.STORE.asset['observed_ms']
        for delta,expected in [(29999,200),(30000,503),(-1,503)]:
            with patch.object(server.time,'time',return_value=(observed+delta)/1000):
                self.assertEqual((await self.client.post('/api/account/risk',json={'entry':100,'stop':90},headers=headers)).status_code,expected)
        view.STORE.asset=None
        self.assertEqual((await self.client.post('/api/account/risk',json={'entry':100,'stop':90},headers=headers)).status_code,503)
        view.SESSIONS.items[headers['Authorization'][7:]]['expires']=view.SESSIONS.clock()-1
        self.assertEqual((await self.client.post('/api/account/risk',json={'entry':100,'stop':90},headers=headers)).status_code,401)
