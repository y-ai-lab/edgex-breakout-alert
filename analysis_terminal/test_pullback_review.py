import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal import test_storage as _storage

server = _storage.server


class PullbackReviewAPITests(unittest.IsolatedAsyncioTestCase):
    setUp = _storage.StorageTests.setUp

    async def test_report_cannot_change_live_cohorts_database_or_notifications(self):
        with server._db_connect() as conn:
            before = list(conn.iterdump())
        with patch.object(server, '_scan_market_rows', side_effect=AssertionError('No market scan')):
            async with AsyncClient(transport=ASGITransport(app=server.app), base_url='http://test') as client:
                response = await client.get('/api/pullback-review')
                self.assertEqual(response.status_code, 200)
                report = response.json()
                self.assertEqual(report['decision'], 'KILL')
                self.assertEqual([p['role'] for p in report['periods']],
                                 ['REUSED_DEVELOPMENT_HISTORY', 'NEW_HISTORICAL_VALIDATION'])
                for p in [report] + report['periods']:
                    self.assertEqual(p['dataset'], 'RETROSPECTIVE')
                    self.assertFalse(p['eligible_for_live_promotion'])
                    self.assertFalse(p['automatic_promotion'])
                self.assertEqual((await client.get('/api/strategy-comparison')).json()['shadow']['resolved'], 0)
                self.assertFalse((await client.get('/api/shadow-v2')).json()['metrics']['promotion_pass'])
        with server._db_connect() as conn:
            self.assertEqual(before, list(conn.iterdump()))

    async def test_invalid_child_dataset_and_automatic_promotion_are_rejected(self):
        report = json.loads(Path(__file__).with_name('pullback_latest.json').read_text())
        report['periods'][0]['eligible_for_live_promotion'] = True
        async with AsyncClient(transport=ASGITransport(app=server.app), base_url='http://test') as client:
            with patch.object(server, '_retrospective_report', return_value=report):
                self.assertEqual((await client.get('/api/pullback-review')).status_code, 503)
            report['periods'][0]['eligible_for_live_promotion'] = False
            report['automatic_promotion'] = True
            with patch.object(server, '_retrospective_report', return_value=report):
                self.assertEqual((await client.get('/api/pullback-review')).status_code, 503)
            with patch.object(server, '_retrospective_report', return_value=dict(status='NOT_RUN',dataset='RETROSPECTIVE',eligible_for_live_promotion=False)):
                self.assertEqual((await client.get('/api/pullback-review')).json()['status'], 'NOT_RUN')


class PullbackReviewUITests(unittest.TestCase):
    def test_full_script_actual_report_metrics_and_strict_separation(self):
        html = Path(__file__).with_name('index.html').read_text()
        report = json.loads(Path(__file__).with_name('pullback_latest.json').read_text())
        script = r"""
const fs=require('fs'),assert=require('assert'),input=JSON.parse(fs.readFileSync(0,'utf8')),html=input.html;
for(const m of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new Function(m[1]);
const elements={};['Status','Body','Notes'].forEach(k=>elements['pullbackReview'+k]={textContent:'',innerHTML:'',style:{}});
const document={getElementById:id=>elements[id]},fmt=(v,d)=>Number(v).toFixed(d);
eval(html.slice(html.indexOf('function lifecycleEscape('),html.indexOf('function lifecycleTime(')));
eval(html.slice(html.indexOf('function renderPullbackReview('),html.indexOf('function renderChronologyReview(')));
renderPullbackReview(input.report);
assert(elements.pullbackReviewStatus.textContent.includes('KILL'));
assert(elements.pullbackReviewStatus.textContent.includes('本番昇格対象外'));
assert((elements.pullbackReviewBody.innerHTML.match(/<tr>/g)||[]).length===6);
assert(elements.pullbackReviewBody.innerHTML.includes('EMA20押し目Shadow'));
assert(elements.pullbackReviewBody.innerHTML.includes('未見過去期間'));
for(const p of input.report.periods){
  const m=p.metrics.pullback;
  assert(elements.pullbackReviewBody.innerHTML.includes(m.signals+' / '+m.resolved));
  assert(elements.pullbackReviewBody.innerHTML.includes(m.avg_r.toFixed(3)));
  assert(elements.pullbackReviewBody.innerHTML.includes(m.avg_mae_r.toFixed(3)));
}
assert(elements.pullbackReviewBody.innerHTML.includes('INSUFFICIENT SAMPLE'));
assert(elements.pullbackReviewNotes.textContent.includes('観測点カバー率'));
assert(elements.pullbackReviewNotes.textContent.includes('実際の取引回数ではありません'));
input.report.periods[0].eligible_for_live_promotion=true;
renderPullbackReview(input.report);assert(elements.pullbackReviewBody.innerHTML==='');
assert(elements.pullbackReviewNotes.textContent==='');
input.report.periods[0].eligible_for_live_promotion=false;input.report.automatic_promotion=true;
renderPullbackReview(input.report);assert(elements.pullbackReviewBody.innerHTML==='');
renderPullbackReview(null);assert(elements.pullbackReviewBody.innerHTML==='');
assert(html.includes("jf('/api/pullback-review')"));
console.log('Pullback review syntax/render/metrics/separation passed');
"""
        result = subprocess.run(['node', '-e', script], input=json.dumps(dict(html=html, report=report)), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
