import asyncio
import copy
import json
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal import market_status as status, test_storage as storage
from analysis_terminal.test_lifecycle import row
from analysis_terminal.test_replay import c

server = storage.server
NOW = 2_000_000_000_000
CONTRACTS = {str(i): server.scanner.Contract(str(i), f'TEST{i}USDC', 'USDC', True, True) for i in range(1,4)}


class AvailabilityTests(unittest.TestCase):
    def test_retry_detects_each_required_interval_and_empty_stream(self):
        data = {('1','HOUR_4'):[1],('2','HOUR_4'):[1],('2','MINUTE_15'):[1],('3','MINUTE_15'):[]}
        self.assertEqual(status.missing_snapshot_contracts(list(CONTRACTS), data, ('HOUR_4','MINUTE_15')), ['1','3'])

    def test_checked_markets_are_distinct_from_indicator_ready_and_forming_bars(self):
        end = NOW//14400000*14400000
        data = {('2','HOUR_4'):[c(end-(49-i)*14400000,'HOUR_4') for i in range(49)] + [c(end,'HOUR_4')],
                ('2','MINUTE_15'):[c(NOW//900000*900000-(15-i)*900000) for i in range(15)]}
        before = copy.deepcopy(data)
        h = status.describe_scan(CONTRACTS, [dict(ticker='TEST1USDC',current_price=100)], data,
                                 server.SETTINGS,server.scanner.INTERVAL_MS,now_ms=NOW)
        self.assertEqual((h['checked'],h['analysis_ready'],h['data_wait_count']),(3,1,2))
        self.assertEqual((h['checked_pct'],h['analysis_coverage_pct']),(100,33.3))
        self.assertEqual((h['missing_snapshot_markets'],h['insufficient_history_markets']),(1,1))
        short,missing=h['data_wait']
        self.assertEqual(short['closed_candles']['HOUR_4'],49)
        self.assertEqual(short['reasons'],['INSUFFICIENT_HISTORY'])
        self.assertEqual(missing['missing_intervals'],['HOUR_4','MINUTE_15'])
        self.assertEqual(data,before)

    def test_empty_universe_is_not_reported_as_complete_data(self):
        h=status.describe_scan({},[],{},server.SETTINGS,server.scanner.INTERVAL_MS,now_ms=NOW)
        self.assertEqual((h['checked_pct'],h['analysis_coverage_pct']),(0,0))

    def test_partial_stream_retry_preserves_already_received_history(self):
        original=[c(NOW-14400000,'HOUR_4')];recovered=[c(NOW-900000)]
        fetch=AsyncMock(side_effect=[{('1','HOUR_4'):original},
            {('1','HOUR_4'):[c(NOW,'HOUR_4')],('1','MINUTE_15'):recovered}])
        with patch.object(server.CLIENT,'get_contracts',AsyncMock(return_value={'1':CONTRACTS['1']})), \
             patch.object(server,'fetch_snapshots',fetch),patch.object(server,'_snapshot_cache',None):
            data=asyncio.run(server.market_snapshots(force=True))
            self.assertEqual(fetch.await_count,2)
            self.assertEqual(fetch.await_args_list[1].args,(['1'],))
            self.assertEqual(data[('1','HOUR_4')],original)
            self.assertEqual(data[('1','MINUTE_15')],recovered)

    def test_failed_retry_does_not_restore_old_cache_or_invent_missing_interval(self):
        old={("1","MINUTE_15"):[c(NOW-900000)]};fresh={('1','HOUR_4'):[c(NOW-14400000,'HOUR_4')]}
        with patch.object(server.CLIENT,'get_contracts',AsyncMock(return_value={'1':CONTRACTS['1']})), \
             patch.object(server,'fetch_snapshots',AsyncMock(side_effect=[fresh,RuntimeError('synthetic failure')])), \
             patch.object(server,'_snapshot_cache',(1,old)):
            data=asyncio.run(server.market_snapshots(force=True))
            self.assertNotIn(('1','MINUTE_15'),data)
            self.assertEqual(data,fresh)


class DisplayAPITests(unittest.TestCase):
    setUp = storage.StorageTests.setUp

    def test_display_only_setups_do_not_change_ready_near_or_persistent_ledgers(self):
        ready=row(stage='READY',ticker='READYUSDC')
        near=row(ticker='NEARUSDC')
        blocked=row(ticker='BLOCKEDUSDC',rr=.5)
        rr_wait=row(ticker='RRUSDC',stage='RR_WAIT',rr=.8)
        rows=[ready,near,blocked,rr_wait]
        contracts={str(i):replace(CONTRACTS['1'],contract_id=str(i),contract_name=r['ticker']) for i,r in enumerate(rows)}
        before_rows=copy.deepcopy(rows);before_summary=server._market_summary(rows)
        with server._db_connect() as conn:before_db=list(conn.iterdump())
        async def get():
            with patch.object(server,'_scan_market_rows',AsyncMock(return_value=(contracts,rows))):
                async with AsyncClient(transport=ASGITransport(app=server.app),base_url='http://test') as client:
                    return (await client.get('/api/screener?stage=READY&q=nomatch')).json()
        j=asyncio.run(get())
        self.assertEqual(j['summary'],before_summary)
        self.assertEqual([x['ticker'] for x in j['ready_candidates']],['READYUSDC'])
        self.assertEqual([x['ticker'] for x in j['qualified_near_candidates']],['NEARUSDC'])
        self.assertEqual({x['ticker'] for x in j['blocked_setup_candidates']},{'BLOCKEDUSDC','RRUSDC'})
        self.assertEqual(j['results'],[])
        self.assertEqual(j['scan_health']['checked'],4)
        self.assertEqual(rows,before_rows)
        with server._db_connect() as conn:self.assertEqual(list(conn.iterdump()),before_db)

    def test_shipped_js_shows_missing_reasons_and_watch_only_without_entry_upgrade(self):
        subprocess.run(['node',str(Path(__file__).with_suffix('.js')),str(Path(__file__).with_name('index.html'))],check=True,capture_output=True,text=True)
