"""Synthetic ledger pagination: full coverage, immutable clocks and no writes."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from analysis_terminal import captured_ledger as pages
from analysis_terminal import server,pending_live,pending_evidence,vwap_live,vwap_evidence
from analysis_terminal.test_pending_entry_replay import record,START as OLD_START

NOW = 1792022400000


def data(count=721):
    records=[dict(key=f'key-{i:04d}',created_ms=100+i//2,value=i) for i in range(count)]
    view=dict(total_records=count,latest=[],meta=dict(capture_start_ms=10,last_success_ms=90),
              metrics=dict(resolved=3),continuous_portfolios=dict(roi=None))
    return view,records


def first(view,records,source='pending',limit=500,now=NOW):
    return pages.page(pages.read_request(source=source,limit=limit,now_ms=now),view,records)


def next_page(previous,view,records,source='pending',limit=500,now=NOW+1):
    request=pages.read_request(source=source,limit=limit,now_ms=now,
                               cursor=previous['ledger_page']['next_cursor'])
    return pages.page(request,view,records)


class PaginationTests(unittest.TestCase):
    def test_more_than_500_records_are_returned_once_with_all_metrics_unchanged(self):
        view,rows=data();before=deepcopy((view,rows));p=first(view,list(reversed(rows)))
        q=next_page(p,view,rows)
        self.assertEqual(p['latest']+q['latest'],rows)
        self.assertEqual(q['ledger_page']['snapshot_sha256'],p['ledger_page']['snapshot_sha256'])
        self.assertEqual(q['ledger_page']['snapshot_ms'],NOW)
        self.assertEqual(q['ledger_page']['offset'],500)
        self.assertTrue(q['ledger_page']['complete']);self.assertIsNone(q['ledger_page']['next_cursor'])
        for result in (p,q):
            self.assertEqual({k:v for k,v in result.items() if k not in ('latest','ledger_page')},
                             {k:v for k,v in view.items() if k!='latest'})
            self.assertFalse(result['ledger_page']['new_independent_sample'])
        self.assertEqual((view,rows),before)

    def test_many_small_pages_and_equal_timestamps_have_a_stable_key_order(self):
        view,rows=data(31);p=first(view,rows,limit=3);all_rows=[]
        while True:
            all_rows.extend(p['latest'])
            if p['ledger_page']['complete']:break
            p=next_page(p,view,rows,limit=3)
        self.assertEqual(all_rows,rows)
        self.assertEqual(len({r['key'] for r in all_rows}),31)

    def test_empty_and_exact_page_boundary_do_not_invent_a_next_cursor(self):
        for count in (0,500):
            v,r=data(count);p=first(v,r)
            self.assertEqual(len(p['latest']),count);self.assertTrue(p['ledger_page']['complete'])
            self.assertEqual(p['ledger_page']['returned_records'],count)

    def test_outcome_entry_clock_identity_addition_deletion_and_metadata_changes_abort(self):
        v,r=data();p=first(v,r)
        for change in ('outcome','price','clock','key','add','remove','meta','metrics','proof'):
            view,rows=deepcopy((v,r))
            if change=='outcome':rows[-1]['status']='TP'
            elif change=='price':rows[-1]['trigger']=5
            elif change=='clock':rows[0]['created_ms']+=10
            elif change=='key':rows[0]['key']='different'
            elif change=='add':rows.append(dict(key='new',created_ms=9999));view['total_records']+=1
            elif change=='remove':rows.pop();view['total_records']-=1
            elif change=='meta':view['meta']['capture_start_ms']+=1
            elif change=='metrics':view['metrics']['resolved']+=1
            else:view['observation_evidence']=dict(status='MISSING')
            with self.subTest(change=change),self.assertRaises(pages.LedgerError) as e:
                next_page(p,view,rows)
            self.assertEqual((e.exception.status,e.exception.code),(409,'LEDGER_CHANGED_RESTART_REQUIRED'))

    def test_retry_same_cursor_is_idempotent_when_the_ledger_has_not_changed(self):
        v,r=data();p=first(v,r)
        self.assertEqual(next_page(p,v,r,now=NOW+1),next_page(p,v,r,now=NOW+2))

    def test_expiry_boundary_future_clock_and_process_restart_require_restart(self):
        v,r=data();p=first(v,r)
        self.assertTrue(next_page(p,v,r,now=NOW+pages.TTL_MS-1)['ledger_page']['complete'])
        for now in (NOW-1,NOW+pages.TTL_MS):
            with self.assertRaises(pages.LedgerError) as e:next_page(p,v,r,now=now)
            self.assertEqual(e.exception.status,409)
        with patch.object(pages,'_SIGNING_KEY',b'synthetic-other-process'):
            with self.assertRaises(pages.LedgerError) as e:next_page(p,v,r)
            self.assertEqual(e.exception.status,409)

    def test_source_and_page_size_cannot_be_swapped_between_requests(self):
        v,r=data();p=first(v,r)
        for source,limit in (('vwap',500),('pending',100)):
            with self.assertRaises(pages.LedgerError) as e:next_page(p,v,r,source,limit)
            self.assertEqual(e.exception.status,400)

    def test_tampered_cursor_has_no_unsigned_time_offset_or_digest_override(self):
        v,r=data();p=first(v,r);cursor=p['ledger_page']['next_cursor']
        replacement=('0' if cursor[-1]!='0' else '1')
        with self.assertRaises(pages.LedgerError) as e:
            pages.read_request(source='pending',limit=500,now_ms=NOW,cursor=cursor[:-1]+replacement)
        self.assertEqual(e.exception.status,409)

    def test_malformed_and_signed_invalid_cursors_fail_closed_without_echo(self):
        good=dict(v=1,s='pending',n=500,t=NOW,i=500,d='a'*64)
        invalid=['', 'private-marker', 'a.b.c', 'x.'+'0'*64, 'A'*1025]
        for override in ({'i':True},{'i':0},{'t':None},{'d':'bad'},{'v':True},{'n':None},{'unknown':1}):
            invalid.append(pages._token(good|override))
        for cursor in invalid:
            with self.subTest(cursor=cursor[:10]),self.assertRaises(pages.LedgerError) as e:
                pages.read_request(source='pending',limit=500,now_ms=NOW,cursor=cursor)
            self.assertNotIn('private-marker',str(e.exception))

    def test_bad_row_identity_count_time_duplicate_or_nonfinite_data_is_blocked(self):
        v,r=data(2)
        for view,rows in [(v,r+r[:1]),(v,[r[0],r[0]]),(v,[r[0],r[1]|dict(key='')]),
                          (v,[r[0],r[1]|dict(created_ms=True)]),(v,[r[0],r[1]|dict(value=float('nan'))]),
                          (v|dict(latest=r),r),(v,None)]:
            with self.assertRaises(pages.LedgerError) as e:first(view,rows)
            self.assertEqual(e.exception.status,503)

    def test_digest_changes_for_ordered_content_but_not_database_row_order(self):
        v,r=data(3)
        self.assertEqual(first(v,r),first(v,list(reversed(r))))
        self.assertNotEqual(first(v,r)['ledger_page']['snapshot_sha256'],
                            first(v,r[:2]+[r[2]|dict(value=4)])['ledger_page']['snapshot_sha256'])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'ledger.sqlite'
        self.bind=patch.object(server,'DB_PATH',self.path);self.bind.start()
        self.start=NOW-5*pending_live.STEP;self.start=self.start//pending_live.DAY*pending_live.DAY
        with server._db_connect() as conn:
            pending_live.initialize(conn,now_ms=self.start-1000)
            pending_evidence.initialize(conn,now_ms=self.start-1000)
            vwap_live.initialize(conn,now_ms=self.start-1000)
            vwap_evidence.initialize(conn,now_ms=self.start-1000)
        self.clock=patch.object(server.time,'time',return_value=(self.start+5*pending_live.STEP)/1000)
        self.clock.start();self.client=TestClient(server.app)  # No lifespan/collector/order activity.

    def tearDown(self):
        self.clock.stop();self.bind.stop();self.temp.cleanup()

    def seed(self,source,count=721):
        for i in range(count):
            r=record();shift=self.start-OLD_START
            for field in ('created_ms','signal_candle_ms','expires_ms'):r[field]+=shift
            r.update(key=f'synthetic-{source}-{i:04d}',setup_id=f'synthetic-setup-{i:04d}',
                     status='EXPIRED',outcome_ms=r['expires_ms'],cohort_start_ms=self.start,
                     observed_ms=self.start+1000,execution_start_ms=self.start+pending_live.STEP,
                     next_candle_ms=r['expires_ms'],roll_level=100,dataset='LIVE_CAPTURE_HYPOTHETICAL')
            with server._db_connect() as conn:
                if source=='pending':conn.execute('INSERT INTO pending_live_signals VALUES(?,?,?,?)',
                                                 (r['key'],r['ticker'],json.dumps(r),r['created_ms']))
                else:
                    r['model']=vwap_live.study.MODEL
                    conn.execute('INSERT INTO vwap_live_signals VALUES(?,?,?)',(r['key'],json.dumps(r),r['created_ms']))

    def get(self,source='pending',**params):
        return self.client.get('/api/captured-shadow-ledger',params=dict(source=source,**params))

    def test_both_sources_exceed_legacy_cap_without_changing_the_legacy_api(self):
        for source in pages.SOURCES:
            self.seed(source)
            original=self.client.get('/api/'+('pending' if source=='pending' else 'vwap')+'-entry-shadow?limit=500').json()
            a=self.get(source,limit=500);self.assertEqual(a.status_code,200)
            self.assertIn('no-store',a.headers['cache-control']);p=a.json()
            b=self.get(source,limit=500,cursor=p['ledger_page']['next_cursor']);self.assertEqual(b.status_code,200)
            all_rows=p['latest']+b.json()['latest'];self.assertEqual(len(all_rows),721)
            self.assertEqual(len({r['key'] for r in all_rows}),721)
            self.assertEqual(len(original['latest']),500)
            self.assertEqual({k:v for k,v in p.items() if k not in ('latest','ledger_page')},
                             {k:v for k,v in original.items() if k!='latest'})
            self.assertEqual(self.client.get('/api/'+source+'-entry-shadow?limit=500').json(),original)

    def test_reads_do_not_modify_database_schema_bytes_rows_or_capture_origin(self):
        self.seed('pending',4);before=self.path.read_bytes()
        with server._db_connect() as c:dump=list(c.iterdump())
        p=self.get(limit=2).json();self.get(limit=2,cursor=p['ledger_page']['next_cursor'])
        self.assertEqual(self.path.read_bytes(),before)
        with server._db_connect() as c:self.assertEqual(list(c.iterdump()),dump)

    def test_update_between_pages_returns_conflict_and_fresh_restart_is_complete(self):
        self.seed('pending',4);p=self.get(limit=2).json()
        with server._db_connect() as c:
            r=json.loads(c.execute('SELECT payload FROM pending_live_signals LIMIT 1').fetchone()[0]);r['next_candle_ms']+=pending_live.STEP
            c.execute('UPDATE pending_live_signals SET payload=? WHERE signal_key=?',(json.dumps(r),r['key']))
        res=self.get(limit=2,cursor=p['ledger_page']['next_cursor'])
        self.assertEqual(res.status_code,409);self.assertEqual(res.json()['detail'],'LEDGER_CHANGED_RESTART_REQUIRED')
        fresh=self.get(limit=2).json();self.assertNotEqual(fresh['ledger_page']['snapshot_sha256'],p['ledger_page']['snapshot_sha256'])
        self.assertTrue(self.get(limit=2,cursor=fresh['ledger_page']['next_cursor']).json()['ledger_page']['complete'])

    def test_query_only_connection_blocks_accidental_writer_in_review(self):
        def writer(conn,**kw):conn.execute('DELETE FROM pending_live_signals')
        self.seed('pending',1)
        with patch.object(server.pending_live,'review',side_effect=writer):r=self.get()
        self.assertEqual(r.status_code,503)
        with server._db_connect() as c:self.assertEqual(len(pending_live.records(c)),1)

    def test_api_is_get_only_and_parameter_or_cursor_errors_do_not_echo_private_text(self):
        for params in ({'source':'other'},{'limit':501},{'limit':0}):self.assertEqual(self.get(**params).status_code,422)
        r=self.get(cursor='synthetic-private-marker')
        self.assertEqual(r.status_code,400);self.assertNotIn('synthetic-private-marker',r.text)
        self.assertEqual(self.client.post('/api/captured-shadow-ledger').status_code,405)

    def test_corrupt_saved_payload_is_503_without_rows_or_raw_price_leakage(self):
        self.seed('pending',1)
        with server._db_connect() as c:c.execute('UPDATE pending_live_signals SET payload=?',('synthetic-private-invalid-json',))
        r=self.get();self.assertEqual(r.status_code,503);self.assertNotIn('synthetic-private',r.text)
        self.assertNotIn('latest',r.json())

    def test_missing_database_is_not_created_by_reading_the_ledger(self):
        missing=self.path.parent/'missing.sqlite'
        with patch.object(server,'DB_PATH',missing):r=self.get()
        self.assertEqual(r.status_code,503);self.assertFalse(missing.exists())

    def test_each_page_uses_one_sqlite_snapshot_even_when_a_writer_commits_mid_read(self):
        self.seed('pending',4)
        with server._db_connect() as c:c.execute('PRAGMA journal_mode=WAL')
        engine_review=pending_live.review
        def changing_review(conn,**kwargs):
            result=engine_review(conn,**kwargs)
            with server._db_connect() as writer:
                row=json.loads(writer.execute('SELECT payload FROM pending_live_signals ORDER BY signal_key LIMIT 1').fetchone()[0])
                row['trigger']+=.001
                writer.execute('UPDATE pending_live_signals SET payload=? WHERE signal_key=?',(json.dumps(row),row['key']))
            return result
        original=pending_live.records(server._db_connect())[0]['trigger']
        with patch.object(server.pending_live,'review',side_effect=changing_review):r=self.get(limit=2)
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['latest'][0]['trigger'],original)
        self.assertEqual(self.get(limit=2,cursor=r.json()['ledger_page']['next_cursor']).status_code,409)

    def test_week_boundary_does_not_change_read_clock_or_complete_a_partial_cohort(self):
        self.seed('pending',4);end=self.start+pending_live.WEEK
        with patch.object(server.time,'time',return_value=(end-1)/1000):p=self.get(limit=2).json()
        self.assertFalse(p['cohorts'][0]['period_complete'])
        with patch.object(server.time,'time',return_value=(end+1000)/1000):q=self.get(limit=2,cursor=p['ledger_page']['next_cursor']).json()
        self.assertEqual(q['ledger_page']['snapshot_ms'],end-1)
        self.assertEqual(q['cohorts'],p['cohorts'])
        self.assertFalse(q['cohorts'][0]['period_complete'])
