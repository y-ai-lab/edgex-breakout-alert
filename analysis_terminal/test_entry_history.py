"""Archive deduplication, additive import, chronology and read-only UI/API checks."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal import entry_history as archive
from analysis_terminal.test_lifecycle import row, NOW, MONITOR, ENTRY
from analysis_terminal import test_storage as storage

server = storage.server


class EntryHistoryTests(unittest.TestCase):
    setUp = storage.StorageTests.setUp

    def capture(self, rows, now=NOW):
        with server._db_connect() as conn:
            archive.capture(conn, rows, now_ms=now, min_rr=2, monitor_ms=MONITOR, entry_ms=ENTRY)
            conn.commit()

    def review(self, **kwargs):
        with server._db_connect() as conn:
            return archive.review(conn, now_ms=NOW+100, days=0, **kwargs)

    def test_first_scan_frozen_near_ready_and_latest_are_independent_of_push(self):
        self.capture([row()]);self.capture([row(stage='READY',entry_reference=101)], NOW+1)
        self.capture([row(stage='RR_WAIT',entry_reference=102,rr=1)], NOW+2)
        self.capture([row(stage='READY',entry_reference=103)], NOW+3)
        item=self.review()['items'][0]
        self.assertEqual(item['first_near']['entry_reference'],100)
        self.assertEqual(item['first_ready']['entry_reference'],101)
        self.assertEqual(item['latest']['entry_reference'],103)
        self.assertEqual(self.review()['totals'],dict(setups=1,ready=1,near=1))
        self.assertEqual(server._load_push_events(),[])
        server._init_db();server._init_db()
        self.assertEqual(self.review()['items'][0],item)
        self.assertEqual(server._subscription_count(),1)

    def test_stale_partial_identityless_and_shadow_only_rows_are_not_candidates(self):
        items=[row(latest_15m_time_ms=0),row(stage='DATA_WAIT'),
               row(stage='RR_WAIT',shadow_v2_ready=True),row(rr=1.99),row(rr=float('nan')),
               dict(row(),setup_id=None),dict(row(),setup_id='wrong')]
        self.capture(items)
        self.assertEqual(self.review()['totals']['setups'],0)
        self.capture([row()]);self.capture([row(stage='READY',latest_4h_time_ms=0)],NOW+1)
        self.assertIsNone(self.review()['items'][0]['first_ready'])

    def test_same_ticker_new_setup_does_not_promote_old_setup(self):
        original=row();replacement=row(stage='READY',breakout_time_ms=original['breakout_time_ms']+MONITOR)
        self.capture([original]);self.capture([replacement],NOW+1)
        server._persist_setup_lifecycles([original],observed_ms=NOW)
        server._persist_setup_lifecycles([replacement],observed_ms=NOW+1)
        items={i['setup_id']:i for i in self.review()['items']}
        self.assertEqual(len(items),2)
        self.assertIsNone(items[original['setup_id']]['first_ready'])
        self.assertEqual(items[original['setup_id']]['lifecycle']['end_reason'],'SUPERSEDED')
        self.assertIsNotNone(items[replacement['setup_id']]['first_ready'])

    def test_out_of_order_update_cannot_replace_latest_or_duplicate_first(self):
        self.capture([row()],NOW+5);self.capture([row(entry_reference=95)],NOW)
        item=self.review()['items'][0]
        self.assertEqual(item['latest']['observed_ms'],NOW+5)
        self.assertEqual(item['first_near']['observed_ms'],NOW)
        self.assertEqual(item['latest']['entry_reference'],100)

    def test_cursor_filters_and_total_cohort_are_independent_of_page_limit(self):
        self.capture([row(ticker=t) for t in ['BTCUSDC','ETHUSDC','HYPEUSDC']])
        full=self.review();page=self.review(limit=1);found=[]
        while True:
            found.extend(i['setup_id'] for i in page['items'])
            self.assertEqual(page['totals'],full['totals'])
            if not page['next_cursor']:break
            page=self.review(limit=1,**page['next_cursor'])
        self.assertEqual(len(found),3);self.assertEqual(len(set(found)),3)
        self.assertEqual(self.review(ticker='btc')['totals']['setups'],1)
        self.assertEqual(self.review(ticker='%')['totals']['setups'],0)
        self.assertEqual(self.review(candidate_kind='READY')['totals']['setups'],0)
        self.assertEqual(self.review(candidate_kind='NEAR')['totals']['setups'],3)
        with server._db_connect() as conn:
            recent=archive.review(conn,now_ms=NOW+32*86_400_000,days=30)
        self.assertEqual(recent['totals']['setups'],0)
        self.assertEqual(self.review()['totals']['setups'],3)

    def test_import_available_events_and_first_current_signal_without_inference(self):
        item=row()
        with patch.object(server.time,'time',return_value=NOW/1000):
            server._log_candidate_event(item,kind='NEAR',label='near')
            server._log_candidate_event(dict(item,setup_id=None),kind='READY',label='legacy')
        signal=dict(key='first',setup_id=item['setup_id'],ticker=item['ticker'],side='LONG',
                    entry=101,stop=90,target=125,rr=2.2,created_ms=NOW+1,
                    result={'status':'SL','evaluation_version':2,'coverage_complete':True,'final_r':-1})
        server._insert_paper_signal(signal)
        server._insert_paper_signal(dict(signal,key='duplicate',created_ms=NOW+2,
                                        result={'status':'TP','evaluation_version':2,'coverage_complete':True,'final_r':2}))
        server._insert_paper_signal(dict(signal,key='legacy',setup_id=None))
        server._save_market_snapshot({'time_ms':self.now_ms,'ready':0})
        with server._db_connect() as conn:
            conn.execute("DELETE FROM app_state WHERE key='entry_history_import_v1'");conn.commit()
        server._init_db();server._init_db()
        review=self.review();self.assertEqual(review['totals'],dict(setups=1,ready=1,near=1))
        saved=review['items'][0]
        self.assertEqual(saved['first_ready']['direction'],'LONG')
        self.assertEqual(saved['first_ready']['entry_reference'],101)
        self.assertEqual(saved['reference_outcome']['status'],'SL')
        self.assertTrue(saved['reference_outcome']['verified'])
        self.assertTrue(saved['reference_outcome']['hypothetical'])
        self.assertEqual(len(server._load_paper_signals()),3)
        self.assertEqual(len(server._load_candidate_events()),2)
        self.assertEqual(len(server._load_market_history()),1)
        self.assertEqual(server._subscription_count(),1)
        # The independent archive survives legacy event pruning and restarts.
        with server._db_connect() as conn:
            conn.execute('DELETE FROM candidate_events');conn.commit()
        server._init_db();self.assertEqual(self.review()['items'],review['items'])

    def test_missing_or_unverified_outcome_is_explicit_and_never_ticker_joined(self):
        item=row(stage='READY');self.capture([item])
        replacement=row(breakout_time_ms=item['breakout_time_ms']+MONITOR)
        signal=dict(key='other',setup_id=replacement['setup_id'],ticker=item['ticker'],side='LONG',
                    created_ms=NOW+1,result={'status':'TP','final_r':2})
        server._insert_paper_signal(signal)
        self.assertIsNone(self.review()['items'][0]['reference_outcome'])
        server._insert_paper_signal(dict(signal,key='this',setup_id=item['setup_id']))
        self.assertFalse(self.review()['items'][0]['reference_outcome']['verified'])


class EntryHistoryApiTests(unittest.IsolatedAsyncioTestCase):
    setUp = storage.StorageTests.setUp

    async def test_validation_pagination_and_read_only_requests(self):
        with server._db_connect() as conn:
            archive.capture(conn,[row(),row(ticker='OTHERUSDC')],now_ms=self.now_ms,
                            min_rr=2,monitor_ms=MONITOR,entry_ms=ENTRY)
            # Use fixture time for candle freshness.
            archive.capture(conn,[row(),row(ticker='OTHERUSDC')],now_ms=NOW,
                            min_rr=2,monitor_ms=MONITOR,entry_ms=ENTRY)
            conn.commit()
            before=conn.execute('SELECT payload FROM entry_candidate_history ORDER BY setup_id').fetchall()
        with patch.object(server.time,'time',return_value=(NOW+100)/1000):
            async with AsyncClient(transport=ASGITransport(app=server.app),base_url='http://test') as client:
                page=(await client.get('/api/entry-history?limit=1')).json()
                self.assertEqual(page['totals']['setups'],2)
                self.assertTrue(page['historical']);self.assertFalse(page['current_entry_status'])
                cursor=page['next_cursor']
                second=(await client.get('/api/entry-history',params={'limit':1,**cursor})).json()
                self.assertNotEqual(page['items'][0]['setup_id'],second['items'][0]['setup_id'])
                for query in ['kind=SHADOW','limit=101','days=-1','before_ms=1','before_setup=x']:
                    self.assertEqual((await client.get('/api/entry-history?'+query)).status_code,422)
        with server._db_connect() as conn:
            after=conn.execute('SELECT payload FROM entry_candidate_history ORDER BY setup_id').fetchall()
        self.assertEqual([r[0] for r in before],[r[0] for r in after])
        self.assertEqual(server._load_push_events(),[])

    async def test_archive_storage_failure_does_not_block_collector_outcomes_or_notifications(self):
        async def sleep(delay):
            if delay==30:raise asyncio.CancelledError
        mocks={name:Mock() for name in ['_persist_scan_result','_persist_shadow_v2_signals',
               '_persist_setup_lifecycles','_simulation_cycle','_maybe_generate_daily_report']}
        mocks['_scan_market_rows']=AsyncMock(return_value=({},[]))
        for name in ['_process_priority_changes','_maybe_push_candidate_changes',
                     '_refresh_paper_signal_results','_refresh_shadow_v2_results',
                     '_refresh_candidate_event_results','_refresh_approach_event_results','_recover_simulation_history']:
            mocks[name]=AsyncMock()
        with patch.multiple(server,**mocks),patch.object(server.asyncio,'sleep',sleep),patch.object(archive,'capture',side_effect=RuntimeError('fixture')),patch('builtins.print'):
            with self.assertRaises(asyncio.CancelledError):await server._background_collector()
        for name in ['_maybe_push_candidate_changes','_refresh_paper_signal_results','_refresh_shadow_v2_results']:
            mocks[name].assert_awaited_once()
