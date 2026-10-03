import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal import test_storage as _storage
from analysis_terminal import test_outcome_audit as fixture

server = _storage.server


class OutcomeQualityAPITests(unittest.IsolatedAsyncioTestCase):
    setUp = _storage.StorageTests.setUp

    async def test_display_limit_cannot_truncate_metrics_or_quality_cohort(self):
        s = fixture.evaluated([fixture.fixture.c(fixture.START)])
        s['key'] = 'identified'
        server._insert_shadow_v2_signal(s)
        with server._db_connect() as conn:
            conn.executemany('INSERT INTO shadow_v2_signals VALUES (?,?,?,?)', [
                (str(i), json.dumps(dict(key=str(i), created_ms=self.now_ms+i,
                                         result=dict(status='TP',final_r=2))), self.now_ms+i, self.now_ms+i)
                for i in range(1001)])
            conn.commit()
        with patch.object(server, '_scan_market_rows', AsyncMock(return_value=({}, []))):
            async with AsyncClient(transport=ASGITransport(app=server.app), base_url='http://test') as client:
                small=(await client.get('/api/shadow-v2?limit=1')).json()
                large=(await client.get('/api/shadow-v2?limit=2000')).json()
        self.assertEqual(small['metrics'], large['metrics'])
        self.assertEqual((len(small['latest']),len(large['latest'])), (1,50))
        m=small['metrics']
        self.assertEqual((m['tracked'],m['setup_tracked'],m['resolved']), (1002,1,0))
        self.assertEqual((m['setup_unverified_results'],m['legacy_unverified_results'],m['unverified_results']), (0,1001,1001))
        self.assertEqual(m['unverified_results_basis'], 'all_records_including_legacy')
        self.assertFalse(m['promotion_pass'])

    async def test_static_audit_does_not_mutate_or_add_promotion_samples(self):
        with server._db_connect() as conn:before=list(conn.iterdump())
        async with AsyncClient(transport=ASGITransport(app=server.app), base_url='http://test') as client:
            r=(await client.get('/api/outcome-audit')).json()
            self.assertEqual(r['dataset'],'LIVE_RECORD_RECONCILIATION')
            self.assertFalse(r['eligible_for_live_promotion'])
            self.assertFalse(r['changes_live_results'])
            self.assertFalse(r['automatic_promotion'])
            self.assertEqual((await client.get('/api/strategy-comparison')).json()['shadow']['signals'],0)
        with server._db_connect() as conn:self.assertEqual(before,list(conn.iterdump()))

    async def test_wrong_dataset_and_write_flags_fail_closed(self):
        r=json.loads(Path(__file__).with_name('outcome_audit_latest.json').read_text())
        async with AsyncClient(transport=ASGITransport(app=server.app),base_url='http://test') as client:
            for key,value in (('dataset','RETROSPECTIVE'),('eligible_for_live_promotion',True),
                              ('changes_live_results',True),('automatic_promotion',True)):
                bad=dict(r,**{key:value})
                with patch.object(server.Path,'read_text',return_value=json.dumps(bad)):
                    self.assertEqual((await client.get('/api/outcome-audit')).status_code,503)


class OutcomeQualityUITests(unittest.TestCase):
    def test_full_script_quality_scopes_actual_audit_and_flags(self):
        html=Path(__file__).with_name('index.html').read_text()
        report=json.loads(Path(__file__).with_name('outcome_audit_latest.json').read_text())
        script=r'''
const fs=require('fs'),assert=require('assert'),input=JSON.parse(fs.readFileSync(0,'utf8')),html=input.html;
for(const s of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new Function(s[1]);
const elements={};for(const id of ['shadowV2Summary','shadowV2Status','shadowV2Current','shadowV2Body','outcomeAuditStatus','outcomeAuditSummary','outcomeAuditBody'])elements[id]={innerHTML:'',textContent:'',querySelectorAll:()=>[],appendChild(){}};
const document={getElementById:id=>elements[id],createElement:()=>({})},fmt=(v,d)=>Number(v).toFixed(d),card=(k,v)=>k+':'+v,directionJa=x=>x,resultJa=x=>x;
eval(html.slice(html.indexOf('function lifecycleEscape('),html.indexOf('function lifecycleReason(')));
eval(html.slice(html.indexOf('function renderShadowV2('),html.indexOf('function renderStrategyComparison(')));
renderShadowV2({metrics:{resolved:1,setup_unverified_results:0,legacy_unverified_results:13,legacy_unidentified_signals:297}});
assert(elements.shadowV2Status.innerHTML.includes('setup未検証・履歴不足 0件'));
assert(elements.shadowV2Status.innerHTML.includes('うち結果未検証 13件'));
renderShadowV2({metrics:{resolved:1,unverified_results:13,legacy_unidentified_signals:297}});
assert(elements.shadowV2Status.innerHTML.includes('setupの品質内訳を確認中'));
assert(!elements.shadowV2Status.innerHTML.includes('setup未検証・履歴不足 13件'));
eval(html.slice(html.indexOf('function renderOutcomeAudit('),html.indexOf('function renderReplayReview(')));
renderOutcomeAudit(input.report);
assert(elements.outcomeAuditStatus.textContent.includes('時点監査・現在の結果とは別'));
assert(elements.outcomeAuditStatus.textContent.includes('昇格標本への追加なし'));
assert(elements.outcomeAuditBody.innerHTML.includes('MATCH'));
const n=['current','shadow'].reduce((n,k)=>n+input.report.models[k].summary.records,0);
assert((elements.outcomeAuditBody.innerHTML.match(/<tr>/g)||[]).length===n);
for(const key of ['eligible_for_live_promotion','changes_live_results','automatic_promotion']){
 const bad={...input.report,[key]:true};renderOutcomeAudit(bad);
 assert(elements.outcomeAuditBody.innerHTML==='');assert(elements.outcomeAuditSummary.innerHTML==='');
}
renderOutcomeAudit(null);assert(elements.outcomeAuditBody.innerHTML==='');
console.log('Outcome quality scopes and reconciliation render/syntax/flags passed');
'''
        result=subprocess.run(['node','-e',script],input=json.dumps(dict(html=html,report=report)),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
