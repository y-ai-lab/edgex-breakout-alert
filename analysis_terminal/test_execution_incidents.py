"""Synthetic incident provenance; no production accounts, data or orders."""
from copy import deepcopy
import json
import unittest
from analysis_terminal import execution_incidents as incident, execution_health as health
from analysis_terminal import test_live_execution as fixtures

NOW = fixtures.NOW


class IncidentTests(unittest.TestCase):
    def test_unknown_or_private_exception_text_is_never_copied(self):
        for code in ('secret-token', 'SDK response with private body', 'NEW_ERROR'):
            value = incident.failure(code, NOW)
            self.assertEqual(value['category'], 'OTHER_EXECUTION_ERROR')
            self.assertNotIn(code, json.dumps(value))
            self.assertNotIn(code, json.dumps(incident.describe_failure(value, NOW)))

    def test_corrupt_category_and_timestamp_are_safe(self):
        for stamp in (True, -1, NOW+1, None, 'timestamp'):
            self.assertIsNone(incident.describe_failure(dict(category='secret', observed_ms=stamp), NOW))
        for category in ([], {}, 'secret'):
            value = incident.describe_failure(dict(category=category, observed_ms=NOW), NOW)
            self.assertNotIn('secret', json.dumps(value))

    def test_legacy_close_is_unknown_and_completion_is_not_request_time(self):
        row = dict(status='CLOSED', close_attempted=True, outcome_observed_ms=NOW)
        before = deepcopy(row)
        value = incident.recent_close([row], NOW)
        self.assertFalse(value['reason_recorded'])
        self.assertEqual(value['time_basis'], 'COMPLETION_RECORDED')
        self.assertIn('未確定', value['label'])
        self.assertEqual(row, before)

    def test_close_request_without_send_is_not_claimed_as_close(self):
        row = dict(status='OWNERSHIP_UNVERIFIED', emergency_close_requested_ms=NOW,
                   emergency_close_reason='PROTECTION_QUERY_FAILED')
        self.assertIsNone(incident.recent_close([row], NOW))
        row['close_attempted'] = True
        value = incident.recent_close([row], NOW)
        self.assertFalse(value['closed_recorded'])
        self.assertEqual(value['time_basis'], 'REQUEST_RECORDED')

    def test_latest_safe_reason_omits_private_fields_and_future_clock(self):
        rows = [dict(status='CLOSED', close_attempted=True, outcome_observed_ms=NOW-10,
                     ticker='private', setup_id='secret', close_client_id='secret'),
                dict(status='CLOSE_PENDING', close_attempted=True, emergency_close_requested_ms=NOW,
                     emergency_close_reason='PROTECTION_ACK_UNRESOLVED'),
                dict(status='CLOSED', close_attempted=True, emergency_close_requested_ms=NOW+1,
                     emergency_close_reason='secret')]
        before = deepcopy(rows)
        value = incident.recent_close(rows, NOW)
        self.assertTrue(value['reason_recorded'])
        self.assertFalse(value['closed_recorded'])
        self.assertNotIn('secret', json.dumps(value))
        self.assertNotIn('private', json.dumps(value))
        self.assertEqual(rows, before)


class EngineIncidentTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ExecutionTests.setUp
    records = fixtures.ExecutionTests.records
    state = fixtures.ExecutionTests.state
    armed = fixtures.ExecutionTests.armed
    enter = fixtures.ExecutionTests.enter

    async def test_successful_refresh_does_not_erase_failure_or_rearm(self):
        await self.armed()
        self.exchange.account_error = True
        with self.assertRaisesRegex(fixtures.ExecutionError, 'INCOMPLETE_ACCOUNT'):
            await self.engine.cycle()
        original = self.state()['last_reconciliation_failure']
        self.exchange.account_error = False
        self.now += 1000
        await self.engine.cycle()
        s = self.state()
        self.assertIsNone(s['last_error'])
        self.assertEqual(s['last_reconciliation_failure'], original)
        self.assertFalse(s['armed'])
        self.assertEqual(self.exchange.creates, [])
        with self.db() as conn:
            before = list(conn.iterdump())
            fixtures.live.initialize(conn, now_ms=self.now+1000000)
            self.assertEqual(list(conn.iterdump()), before)
            report = fixtures.live.report(conn, self.config, now_ms=self.now)
        self.assertEqual(report['status'], 'PAUSED')
        h = health.report(self.records(), report, now_ms=self.now)
        self.assertIn('口座', h['last_failure']['label'])
        self.assertEqual(h['last_failure']['observed_ms'], original['observed_ms'])

    async def test_recovery_and_lost_ack_preserve_first_close_cause(self):
        self.exchange.fail_kind = 'sl'
        self.exchange.unknown_kind = 'close'
        await self.armed()
        with self.assertRaises(fixtures.ExecutionError):
            await self.engine.cycle([fixtures.candidate()], snapshot_ms=self.now)
        r = self.records()[0]
        self.assertEqual(r['emergency_close_reason'], 'PROTECTION_SEND_FAILED')
        stamp = r['emergency_close_requested_ms']
        self.now += 1000
        await self.engine.cycle()
        current = self.records()[0]
        self.assertEqual(current['emergency_close_reason'], r['emergency_close_reason'])
        self.assertEqual(current['emergency_close_requested_ms'], stamp)
        self.assertEqual(self.exchange.creates.count('close'), 1)
        self.assertFalse(self.state()['armed'])

    async def test_specific_protection_and_fill_phases_are_recorded(self):
        for scenario, wanted in (('bad_fill', 'FILLED_ENTRY_OUTSIDE_LIMITS'),
                                 ('missing_sl', 'PROTECTION_ACK_UNRESOLVED'),
                                 ('bad_sl', 'PROTECTION_CONTENT_INVALID'),
                                 ('partial_exit', 'PARTIAL_EXIT_REMAINDER')):
            with self.subTest(scenario=scenario):
                self.setUp()
                if scenario=='bad_fill':
                    self.exchange.fill_price = fixtures.Decimal(105)
                if scenario=='bad_sl':
                    self.exchange.bad_protection = True
                r = await self.enter()
                if scenario=='missing_sl':
                    del self.exchange.ledger[r['sl_client_id']]
                    await self.engine.cycle()
                if scenario=='partial_exit':
                    self.exchange.fill_exit(r, 'sl', fraction=fixtures.Decimal('.5'))
                    await self.engine.cycle()
                final = self.records()[0]
                self.assertEqual(final['emergency_close_reason'], wanted)
                self.assertEqual(self.exchange.creates.count('close'), 1)
                self.assertTrue(self.exchange.ledger[final['close_client_id']]['reduceOnly'])

    async def test_normal_tp_does_not_create_an_emergency_reason(self):
        r = await self.enter()
        self.exchange.fill_exit(r, 'tp')
        await self.engine.cycle()
        final = self.records()[0]
        self.assertEqual(final['status'], 'CLOSED')
        self.assertNotIn('emergency_close_reason', final)
        self.assertEqual(self.exchange.creates, ['entry','sl','tp'])

    async def test_reason_is_committed_before_network_close(self):
        original = self.exchange.create
        async def checked_create(intent, kind, **kw):
            if kind=='close':
                persisted = self.records()[0]
                self.assertEqual(persisted['emergency_close_reason'], 'PROTECTION_SEND_FAILED')
                self.assertTrue(persisted['close_attempted'])
                self.assertEqual(persisted['emergency_close_requested_ms'], self.now)
            return await original(intent, kind, **kw)
        self.exchange.create = checked_create
        self.exchange.fail_kind = 'sl'
        await self.enter()

    async def test_read_only_and_off_create_no_incidents_or_orders(self):
        for mode in ('OFF','READ_ONLY'):
            self.config.mode = mode
            await self.engine.cycle([fixtures.candidate()], snapshot_ms=self.now)
            self.assertEqual(self.records(), [])
            self.assertNotIn('last_reconciliation_failure', self.state())
            self.assertEqual(self.exchange.creates, [])
