import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal import test_storage as _storage

server = _storage.server


class ReplayReviewAPITests(unittest.IsolatedAsyncioTestCase):
    setUp = _storage.StorageTests.setUp

    async def test_real_report_is_read_only_and_never_counts_toward_live_gate(self):
        with server._db_connect() as conn:
            before = list(conn.iterdump())
        with patch.object(server, '_scan_market_rows', side_effect=AssertionError('No market scan')):
            async with AsyncClient(transport=ASGITransport(app=server.app), base_url='http://test') as client:
                replay = (await client.get('/api/replay-review')).json()
                live = (await client.get('/api/strategy-comparison')).json()
                shadow = (await client.get('/api/shadow-v2?refresh=false')).json()
        self.assertEqual(replay['comparison']['shadow']['resolved'], 179)
        self.assertFalse(replay['eligible_for_live_promotion'])
        self.assertEqual(live['shadow']['resolved'], 0)
        self.assertFalse(live['automatic_promotion'])
        self.assertFalse(shadow['metrics']['promotion_pass'])
        with server._db_connect() as conn:
            self.assertEqual(before, list(conn.iterdump()))

    async def test_missing_invalid_json_or_wrong_dataset_fail_closed(self):
        async with AsyncClient(transport=ASGITransport(app=server.app), base_url='http://test') as client:
            with patch.object(Path, 'exists', return_value=False):
                response = await client.get('/api/replay-review')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['status'], 'NOT_RUN')
                self.assertFalse(response.json()['eligible_for_live_promotion'])
            for raw in ('{', '[]', '{"dataset":"LIVE","eligible_for_live_promotion":false}',
                        '{"dataset":"RETROSPECTIVE","eligible_for_live_promotion":true}'):
                with self.subTest(raw=raw), patch.object(Path, 'read_text', return_value=raw):
                    self.assertEqual((await client.get('/api/replay-review')).status_code, 503)


class ReplayReviewUITests(unittest.TestCase):
    def test_actual_report_rendering_syntax_and_dataset_guard(self):
        html = Path(__file__).with_name('index.html').read_text()
        report = json.loads(Path(__file__).with_name('replay_latest.json').read_text())
        script = r'''
const fs=require('fs'),assert=require('assert'),input=JSON.parse(fs.readFileSync(0,'utf8')),html=input.html;
for(const m of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new Function(m[1]);
const elements={};['Status','Summary','Body','Notes'].forEach(k=>elements['replayReview'+k]={innerHTML:'',textContent:''});
const document={getElementById:id=>elements[id]},fmt=(v,d)=>Number(v).toFixed(d),card=(k,v)=>k+':'+v;
eval(html.slice(html.indexOf('function renderReplayReview('),html.indexOf('function lifecycleTime(')));
renderReplayReview(input.report);
assert(elements.replayReviewStatus.textContent.includes('本番昇格対象外'));
assert(elements.replayReviewBody.innerHTML.includes('235 / 179'));
assert(elements.replayReviewBody.innerHTML.includes('-0.45'));
assert(elements.replayReviewNotes.textContent.includes('INSUFFICIENT SAMPLE: 現行'));
assert(elements.replayReviewSummary.innerHTML.includes('88.7%'));
assert(!elements.replayReviewBody.innerHTML.includes('—%'));
renderReplayReview({...input.report,eligible_for_live_promotion:true});
assert(elements.replayReviewBody.innerHTML==='');assert(elements.replayReviewNotes.textContent==='');
renderReplayReview({dataset:'RETROSPECTIVE',eligible_for_live_promotion:false,status:'NOT_RUN'});
assert(elements.replayReviewSummary.innerHTML==='');
assert(html.includes("jf('/api/replay-review')"));
console.log('replay UI syntax/render/separation passed');
'''
        p = subprocess.run(['node', '-e', script], input=json.dumps(dict(html=html, report=report)),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
