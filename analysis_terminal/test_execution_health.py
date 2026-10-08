"""Synthetic ledger diagnostics; no orders, credentials or market samples."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from httpx import ASGITransport, AsyncClient
from analysis_terminal import execution_health as health, server
from analysis_terminal import test_live_execution as fixtures
from analysis_terminal import test_live_execution_coexist as coexist

NOW = fixtures.NOW

def connection(**changes):
    return dict(dict(mode='LIVE',status='RUNNING',real_orders_enabled=True,
        protective_management_enabled=True,last_success_ms=NOW,last_error=None,reason=None),**changes)

def protected(**changes):
    return dict(dict(status='PROTECTED',filled_size='1',sl_attempted=True,tp_attempted=True,
        protection_deadline_ms=NOW+120000),**changes)

class HealthTests(unittest.TestCase):
    def audit(self, records=(), **changes):
        return health.report(records,connection(**changes),now_ms=NOW)

    def test_scope_empty_and_baselines_do_not_mean_account_flat_or_filled(self):
        h=self.audit([dict(status='SKIPPED')]*5)
        self.assertEqual(h['state'],'NO_ACTIVE_BOT_RECORD')
        self.assertEqual(h['counts']['recorded_fills'],0)
        self.assertEqual(h['counts']['skipped'],5)
        self.assertFalse(h['exchange_positions_queried'])
        self.assertIn('手動',h['summary'])

    def test_protection_is_only_recorded_not_current_exchange_proof(self):
        h=self.audit([protected()])
        self.assertEqual(h['state'],'PROTECTION_RECORDED')
        self.assertFalse(h['per_order_freshness_verified'])
        self.assertEqual(h['counts']['recorded_fills'],1)
        self.assertEqual(self.audit([protected()],reason='MANUAL_PAUSE',real_orders_enabled=False)['entry_review'],'UNRESOLVED_LEDGER_REVIEW_REQUIRED')

    def test_ownership_persists_after_connection_error_cleared(self):
        for status in ('OWNERSHIP_CONFLICT','OWNERSHIP_UNVERIFIED'):
            h=self.audit([dict(status=status)],last_error=None)
            self.assertEqual(h['state'],'OWNERSHIP_UNCERTAIN')
            self.assertEqual(h['severity'],'CRITICAL')

    def test_close_attempt_precedes_old_protected_state(self):
        self.assertEqual(self.audit([protected(close_attempted=True)])['state'],'EXIT_UNCONFIRMED')
        self.assertEqual(self.audit(reason='EXIT_CANCEL_UNCONFIRMED')['state'],'EXIT_UNCONFIRMED')

    def test_send_and_ack_are_not_fill_proof(self):
        for status in health.PENDING:
            h=self.audit([dict(status=status,observed_ms=NOW)])
            self.assertEqual(h['state'],'ENTRY_PENDING')
            self.assertEqual(h['counts']['recorded_fills'],0)
        self.assertEqual(self.audit([dict(status='ACKED_ENTRY',observed_ms=NOW-10000)])['state'],'ENTRY_UNCONFIRMED')
        self.assertEqual(self.audit([dict(status='ACKED_ENTRY')])['state'],'ENTRY_UNCONFIRMED')

    def test_protection_expiry_and_cancellation_are_not_protected(self):
        for row in (protected(protection_deadline_ms=NOW+60000),protected(protection_deadline_ms=NOW-1),protected(sl_cancel_attempted=True)):
            self.assertEqual(self.audit([row])['state'],'PROTECTION_UNCONFIRMED')
        for status in health.PROTECTING:
            self.assertEqual(self.audit([dict(status=status)])['state'],'PROTECTION_UNCONFIRMED')

    def test_invalid_unknown_and_contradictory_records_are_uncertain(self):
        for row in (None,{},dict(status='future'),protected(sl_attempted='true'),protected(filled_size='NaN'),protected(filled_size='-1'),protected(filled_size='inf'),protected(filled_size=True),protected(protection_deadline_ms=True),dict(status='CLOSED',filled_size='1'),dict(status='CLOSED',filled_size='1',outcome_observed_ms=NOW+1),dict(status='NO_FILL',filled_size='1'),dict(status='SKIPPED',ownership_quarantined=True)):
            with self.subTest(row=row):
                self.assertEqual(self.audit([row])['state'],'LEDGER_DATA_UNCERTAIN')

    def test_multiple_active_and_untruncated_closed_counts(self):
        self.assertEqual(self.audit([protected(),protected()])['state'],'LEDGER_DATA_UNCERTAIN')
        h=self.audit([dict(status='CLOSED',filled_size='1',outcome_observed_ms=NOW)]*50)
        self.assertEqual(h['counts']['closed'],50)
        self.assertEqual(h['active_records'],0)

    def test_stale_and_clock_boundaries(self):
        self.assertEqual(self.audit([protected()],last_success_ms=NOW-29999)['state'],'PROTECTION_RECORDED')
        for value in (NOW-30000,NOW+1,None,True):
            self.assertEqual(self.audit([protected()],last_success_ms=value)['state'],'ACTIVE_OBSERVATION_STALE')

    def test_disabled_and_policy_blocked_management(self):
        for mode in ('OFF','READ_ONLY'):
            self.assertEqual(self.audit([protected()],mode=mode)['state'],'MANAGEMENT_DISABLED')
        self.assertEqual(self.audit([protected()],protective_management_enabled=False)['state'],'MANAGEMENT_DISABLED')
        self.assertEqual(self.audit([protected()],last_error='ACCOUNT_OR_POLICY_CHANGED')['state'],'MANAGEMENT_BLOCKED')

    def test_private_metadata_never_emitted_and_input_is_immutable(self):
        rows=[protected(setup_id='secret-setup',ticker='secret-ticker',entry_price='private-price')]
        c=connection(last_error='secret-error',reason='secret-reason')
        events=[dict(status='CLOSED',observed_ms=NOW,setup_id='secret-setup'),dict(status='secret-state',observed_ms=NOW+1)]
        before=copy.deepcopy((rows,c,events))
        h=health.report(rows,c,now_ms=NOW,events=events)
        self.assertEqual((rows,c,events),before)
        self.assertNotIn('secret',json.dumps(h))
        self.assertIsNone(h['recent_events'][1]['observed_ms'])
        self.assertNotIn('決着',h['recent_events'][1]['label'])

class EngineHealthTests(unittest.IsolatedAsyncioTestCase):
    setUp = coexist.CoexistTests.setUp
    records = coexist.CoexistTests.records
    state = coexist.CoexistTests.state
    armed = coexist.CoexistTests.armed
    enter = coexist.CoexistTests.enter

    async def test_recovered_sdk_does_not_hide_quarantined_ownership(self):
        await self.enter()
        self.exchange.external_orders=[coexist.manual('10000001')]
        with self.assertRaisesRegex(fixtures.ExecutionError,'CONTRACT_OWNERSHIP_CONFLICT'):
            await self.engine.cycle()
        await self.engine.cycle()
        with self.db() as db:
            c=fixtures.live.report(db,self.config,now_ms=self.now)
        self.assertIsNone(c['last_error'])
        self.assertEqual(c['status'],'PAUSED')
        rows=self.records();before=copy.deepcopy(rows)
        h=health.report(rows,c,now_ms=self.now)
        self.assertEqual(h['state'],'OWNERSHIP_UNCERTAIN')
        self.assertEqual(h['severity'],'CRITICAL')
        self.assertEqual(rows,before)
        self.assertEqual(self.exchange.creates,['entry','sl','tp'])
        self.assertEqual(len(self.exchange.cancels),2)
        self.assertEqual(len(self.exchange.external_orders),1)

class HealthAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_get_is_sanitized_and_does_not_change_any_db_row(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server,'DB_PATH',Path(directory)/'test.db'), patch.object(server,'_live_execution_config',fixtures.configuration()):
            server._init_db()
            with server._live_execution_db() as db:
                fixtures.live.save(db,dict(protected(),setup_id='private-setup'),NOW)
                db.commit()
                before=list(db.iterdump())
            async with AsyncClient(transport=ASGITransport(app=server.app),base_url='https://test') as client:
                for _ in range(2):
                    response=await client.get('/api/live-execution')
                    self.assertEqual(response.status_code,200)
                    h=response.json()['health']
                    self.assertTrue(h['read_only'])
                    self.assertNotIn('private-setup',response.text)
                    self.assertEqual(len(h['recent_events']),1)
                self.assertEqual((await client.post('/api/live-execution')).status_code,405)
            with server._live_execution_db() as db:
                self.assertEqual(list(db.iterdump()),before)
