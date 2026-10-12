"""Synthetic watchlists: atomic revisions, legacy data preservation, no market/ordering."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from pydantic import ValidationError
from analysis_terminal import server

KEY = 'synthetic-watchlist-key-00000001'
OTHER = 'synthetic-watchlist-key-00000002'


class WatchlistSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'watch.sqlite'
        self.bind = patch.object(server, 'DB_PATH', self.path); self.bind.start()
        with server._db_connect() as conn:
            conn.execute('CREATE TABLE synced_watchlists(sync_key TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_ms INTEGER NOT NULL)')

    def tearDown(self):
        self.bind.stop(); self.temp.cleanup()

    def save(self, values, revision=0, key=KEY):
        return server._save_synced_watchlist(server.WatchlistSyncRequest(sync_key=key,tickers=values,expected_revision=revision))

    def test_missing_is_not_existing_empty(self):
        self.assertEqual(server._load_watchlist_snapshot(KEY),dict(exists=False,revision=0,tickers=[]))
        saved=self.save([])
        self.assertTrue(saved['exists']);self.assertGreater(saved['revision'],0)
        self.assertEqual(server._load_watchlist_snapshot(KEY),saved)

    def test_normalization_and_successive_revision(self):
        a=self.save([' btcusdc ','BTCUSDC','ETHUSDC'])
        self.assertEqual(a['tickers'],['BTCUSDC','ETHUSDC'])
        b=self.save(['NEARUSDC'],a['revision'])
        self.assertGreater(b['revision'],a['revision'])
        self.assertEqual(server._load_synced_watchlist(KEY),['NEARUSDC'])

    def test_stale_and_delayed_save_never_overwrites_new_list(self):
        a=self.save(['BTCUSDC']);b=self.save(['ETHUSDC'],a['revision'])
        with self.assertRaises(server.HTTPException) as e:self.save(['BTCUSDC'],a['revision'])
        self.assertEqual(e.exception.status_code,409)
        self.assertEqual(server._load_watchlist_snapshot(KEY),b)

    def test_same_millisecond_and_clock_reversal_still_advance(self):
        with patch.object(server.time,'time',return_value=100):a=self.save(['BTCUSDC']);b=self.save([],a['revision'])
        with patch.object(server.time,'time',return_value=99):c=self.save(['ETHUSDC'],b['revision'])
        self.assertEqual(b['revision'],a['revision']+1);self.assertEqual(c['revision'],b['revision']+1)

    def test_simultaneous_writers_have_one_winner(self):
        a=self.save(['BTCUSDC'])
        def run(value):
            try:return ('saved',self.save([value],a['revision']))
            except server.HTTPException as e:return ('conflict',e.status_code)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,['ETHUSDC','SOLUSDC']))
        self.assertEqual(sorted(x[0] for x in results),['conflict','saved'])
        winner=next(x[1] for x in results if x[0]=='saved')
        self.assertEqual(server._load_watchlist_snapshot(KEY),winner)

    def test_missing_revision_rejects_legacy_blind_write(self):
        before=self.save(['BTCUSDC'])
        with self.assertRaises(server.HTTPException) as e:server._save_synced_watchlist(server.WatchlistSyncRequest(sync_key=KEY,tickers=[]))
        self.assertEqual(e.exception.status_code,409);self.assertIn('再読み込み',e.exception.detail)
        self.assertEqual(server._load_watchlist_snapshot(KEY),before)

    def test_corrupt_database_payload_is_blocked_not_empty_or_replaced(self):
        for payload in ['NOT_JSON','{}','[42]','[""]']:
            with server._db_connect() as conn:conn.execute('INSERT OR REPLACE INTO synced_watchlists VALUES(?,?,?)',(KEY,payload,42))
            with self.assertRaises(server.HTTPException) as e:server._load_watchlist_snapshot(KEY)
            self.assertEqual(e.exception.status_code,503)
            with self.assertRaises(server.HTTPException):self.save([],42)
            with server._db_connect() as conn:self.assertEqual(conn.execute('SELECT payload FROM synced_watchlists WHERE sync_key=?',(KEY,)).fetchone()[0],payload)

    def test_invalid_request_never_truncates_or_removes_old_rows(self):
        before=self.save(['BTCUSDC'])
        for values in [[''],['x'*65]]:
            with self.assertRaises(server.HTTPException):self.save(values,before['revision'])
        for revision in [-1,2.5,9007199254740992]:
            with self.assertRaises(ValidationError):server.WatchlistSyncRequest(sync_key=KEY,expected_revision=revision)
        self.assertEqual(server._load_watchlist_snapshot(KEY),before)

    def test_independent_keys_and_existing_rows_retained(self):
        a=self.save(['BTCUSDC']);b=self.save(['ETHUSDC'],key=OTHER)
        self.save([],b['revision'],key=OTHER)
        self.assertEqual(server._load_watchlist_snapshot(KEY),a)

    def test_existing_schema_initialization_preserves_payload_revision_and_other_data(self):
        with server._db_connect() as conn:conn.execute('INSERT INTO synced_watchlists VALUES(?,?,?)',(KEY,'["BTCUSDC"]',42))
        with patch.object(server,'VAPID_PRIVATE_KEY',''):server._init_db();server._init_db()
        self.assertEqual(server._load_watchlist_snapshot(KEY),dict(exists=True,revision=42,tickers=['BTCUSDC']))
        with server._db_connect() as conn:self.assertEqual([x[1] for x in conn.execute('PRAGMA table_info(synced_watchlists)')],['sync_key','payload','updated_ms'])

    def test_get_header_legacy_query_and_no_store(self):
        self.save(['BTCUSDC'])
        # No TestClient lifespan: market collectors, notifications and execution never start.
        client=TestClient(server.app)
        a=client.get('/api/watchlist',headers={'X-Watch-Sync-Key':KEY})
        b=client.get('/api/watchlist',params={'sync_key':KEY})
        self.assertEqual(a.status_code,200);self.assertEqual(a.json(),b.json());self.assertIn('no-store',a.headers['cache-control'])
        self.assertEqual(client.get('/api/watchlist').status_code,400)
        self.assertEqual(client.get('/api/watchlist',headers={'X-Watch-Sync-Key':OTHER}).json()['exists'],False)

    def test_post_contract_and_conflict_no_sensitive_details(self):
        client=TestClient(server.app)
        a=client.post('/api/watchlist',json=dict(sync_key=KEY,tickers=['BTCUSDC'],expected_revision=0))
        self.assertEqual(a.status_code,200);self.assertTrue(a.json()['ok'])
        b=client.post('/api/watchlist',json=dict(sync_key=KEY,tickers=['ETHUSDC'],expected_revision=0))
        self.assertEqual(b.status_code,409);self.assertNotIn(KEY,b.text)
        self.assertEqual(server._load_synced_watchlist(KEY),['BTCUSDC'])
