import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from httpx import ASGITransport,AsyncClient
from analysis_terminal import test_storage as _storage

server=_storage.server


class ChronologyReviewAPITests(unittest.IsolatedAsyncioTestCase):
    setUp=_storage.StorageTests.setUp

    async def test_two_periods_are_isolated_from_live_data_and_promotion(self):
        with server._db_connect() as conn:before=list(conn.iterdump())
        with patch.object(server,'_scan_market_rows',side_effect=AssertionError('No scan')):
            async with AsyncClient(transport=ASGITransport(app=server.app),base_url='http://test') as client:
                response=await client.get('/api/chronology-review')
                self.assertEqual(response.status_code,200)
                report=response.json()
                self.assertFalse(report['eligible_for_live_promotion'])
                self.assertEqual([p['role'] for p in report['periods']],['EXPLORATORY','UNINSPECTED_RETROSPECTIVE'])
                for p in report['periods']:
                    self.assertFalse(p['eligible_for_live_promotion'])
                    self.assertFalse(p['comparison']['automatic_promotion'])
                self.assertEqual(report['periods'][0]['entry_changes']['delayed'],72)
                self.assertEqual((await client.get('/api/strategy-comparison')).json()['shadow']['resolved'],0)
                self.assertFalse((await client.get('/api/shadow-v2')).json()['metrics']['promotion_pass'])
        with server._db_connect() as conn:self.assertEqual(before,list(conn.iterdump()))


class ChronologyReviewUITests(unittest.TestCase):
    def test_actual_report_two_periods_and_strict_dataset_guard(self):
        html=Path(__file__).with_name('index.html').read_text()
        report=json.loads(Path(__file__).with_name('chronology_latest.json').read_text())
        script=r'''
const fs=require('fs'),assert=require('assert'),input=JSON.parse(fs.readFileSync(0,'utf8')),html=input.html;
for(const m of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new Function(m[1]);
const elements={};['Status','Body','Changes'].forEach(k=>elements['chronologyReview'+k]={textContent:'',innerHTML:'',style:{}});
const document={getElementById:id=>elements[id]},fmt=(v,d)=>Number(v).toFixed(d);
eval(html.slice(html.indexOf('function renderChronologyReview('),html.indexOf('function lifecycleTime(')));
renderChronologyReview(input.report);
assert(elements.chronologyReviewStatus.textContent.includes('本番昇格対象外'));
assert(elements.chronologyReviewBody.innerHTML.includes('既存Shadow'));
assert(elements.chronologyReviewBody.innerHTML.includes('確定後retest Shadow'));
assert(elements.chronologyReviewBody.innerHTML.includes('未見過去期間'));
assert((elements.chronologyReviewBody.innerHTML.match(/<tr>/g)||[]).length===4);
assert(elements.chronologyReviewBody.innerHTML.includes('-0.407'));
assert(elements.chronologyReviewChanges.textContent.includes('後へ移動 72'));
assert(elements.chronologyReviewChanges.textContent.includes('観測点カバー率'));
input.report.periods[1].comparison.shadow.sample_status='INSUFFICIENT SAMPLE';
renderChronologyReview(input.report);assert(elements.chronologyReviewBody.innerHTML.includes('INSUFFICIENT SAMPLE'));
input.report.periods[0].eligible_for_live_promotion=true;
renderChronologyReview(input.report);assert(elements.chronologyReviewBody.innerHTML==='');
assert(elements.chronologyReviewChanges.textContent==='');
renderChronologyReview({dataset:'RETROSPECTIVE',eligible_for_live_promotion:false,status:'NOT_RUN'});
assert(elements.chronologyReviewBody.innerHTML==='');
assert(html.includes("jf('/api/chronology-review')"));
console.log('Chronology review syntax/render/separation passed');
'''
        p=subprocess.run(['node','-e',script],input=json.dumps(dict(html=html,report=report)),capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
