import copy
import json
import subprocess
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from analysis_terminal.readiness_history import JST, STEP, STAGES, daily_readiness, shadow_observation
from analysis_terminal.setups import setup_identity
from analysis_terminal import test_storage as _storage

server = _storage.server
START = int(datetime(2026, 10, 3, tzinfo=JST).timestamp() * 1000)
NOW = START + 96 * STEP


def snapshot(t=START, **values):
    row = dict(time_ms=t, scanned=8, universe=10, stages={k: 1 for k in STAGES})
    row.update(values)
    return row


def signal(t=START+STEP+1, key="first", breakout=START-16*STEP, **values):
    row = dict(key=key, ticker="TESTUSDC", side="LONG", direction="LONG",
               breakout_time_ms=breakout, breakout_level=100, created_ms=t,
               entry=101, stop=99, target=105)
    row["setup_id"] = setup_identity(row)
    row.update(values)
    return row


def report(snapshots=None, current=None, shadow=None, **values):
    return daily_readiness(snapshots or [], current or [], shadow or [], now_ms=NOW, days=2, **values)


class ReadinessHistoryTests(unittest.TestCase):
    def test_gate_conservation_and_conditional_rates(self):
        s=report([snapshot()])["summary"]
        self.assertEqual(list(s["passed"].values()), [8,7,6,5,4,3,2,1])
        self.assertEqual(sum(s["dropped"].values())+s["passed"]["ready"],8)
        self.assertEqual(s["confirmation_pass_pct"],75)
        self.assertEqual(s["post_confirmation_blocked_pct"],66.67)
        self.assertEqual(s["scan_coverage_pct"],80)

    def test_missing_days_and_legacy_shadow_are_not_zero_measurements(self):
        r=report([snapshot()])
        today,yesterday=r["daily"]
        self.assertEqual((today["expected_buckets"],yesterday["expected_buckets"]),(1,96))
        self.assertEqual((today["missing_buckets"],yesterday["missing_buckets"]),(1,95))
        self.assertTrue(today["partial"])
        self.assertIsNone(today["confirmation_pass_pct"])
        self.assertIsNone(yesterday["shadow_ready_buckets"])
        self.assertEqual(r["summary"]["missing_shadow_observations"],1)

    def test_zero_saved_shadow_is_distinct_from_unsaved(self):
        s=report([snapshot(readiness_shadow=shadow_observation([]))])["summary"]
        self.assertEqual((s["shadow_ready_buckets"],s["shadow_ready_exposures"],s["shadow_observations"]),(0,0,1))

    def test_shadow_current_rr_and_structure_cross_tab_uses_existing_flags(self):
        rows=[dict(stage=k,shadow_v2_ready=True) for k in ("STRUCTURE_WAIT","RR_WAIT","READY")]
        saved=shadow_observation(rows)
        s=report([snapshot(readiness_shadow=saved)])["summary"]
        self.assertEqual((s["shadow_ready_exposures"],s["shadow_structure_blocked_exposures"],s["shadow_rr_blocked_exposures"]),(3,1,1))
        self.assertEqual(s["shadow_ready_buckets"],1)

    def test_invalid_stage_totals_unknown_stages_and_shadow_values_are_excluded(self):
        for raw in ({"READY":1}, {"FUTURE_STAGE":8}, {"READY":True}, {"READY":-8}):
            s=report([snapshot(stages=raw)])["summary"]
            self.assertEqual(s["funnel_observations"],0)
            self.assertEqual(s["missing_funnel_observations"],1)
            self.assertIsNone(s["confirmation_pass_pct"])
        for changed in (dict(ready=4),dict(current_rr_blocked=2),dict(ready=-1)):
            saved=dict(version=1,ready=1,current_structure_blocked=0,current_rr_blocked=0,**{})
            saved.update(changed)
            self.assertIsNone(report([snapshot(readiness_shadow=saved)])["summary"]["shadow_ready_exposures"])

    def test_jst_day_boundary_and_signal_candle_sentinel(self):
        r=report([snapshot(NOW)],current=[signal(NOW-STEP+1),signal(NOW+1,key="future",breakout=START)])
        self.assertEqual([x["date"] for x in r["daily"]],["2026-10-04","2026-10-03"])
        self.assertEqual([x["signals"]["current"]["signals"] for x in r["daily"]],[0,1])
        self.assertEqual(r["summary"]["observations"],1)

    def test_same_ticker_new_setup_counts_but_repeated_setup_never_does(self):
        records=[signal(),signal(START+2*STEP+1,key="repeat"),signal(START+3*STEP+1,key="new",breakout=START)]
        r=report([snapshot(),snapshot(START+STEP)],shadow=records)
        self.assertEqual(r["summary"]["signals"]["shadow"]["signals"],2)
        self.assertEqual(r["excluded_records"]["shadow"]["duplicates"],1)
        self.assertEqual(r["summary"]["observed_ready_buckets"],2)

    def test_global_first_entry_before_window_prevents_repeat_from_becoming_new(self):
        r=report(shadow=[signal(START-STEP,key="old"),signal()])
        self.assertEqual(r["summary"]["signals"]["shadow"]["signals"],0)
        self.assertEqual(r["excluded_records"]["shadow"]["duplicates"],1)

    def test_legacy_and_invalid_identity_not_used_and_open_not_resolved(self):
        r=report(shadow=[signal(),signal(key="legacy",setup_id=None),signal(key="bad",setup_id="wrong")])
        m=r["summary"]["signals"]["shadow"]
        self.assertEqual((m["signals"],m["resolved"],m["open"]),(1,0,1))
        self.assertEqual(m["sample_status"],"INSUFFICIENT SAMPLE")
        self.assertEqual((r["excluded_records"]["shadow"]["legacy_unidentified"],r["excluded_records"]["shadow"]["invalid_identity"]),(1,1))
        self.assertFalse(r["automatic_promotion"])
        self.assertFalse(r["changes_live_rules"])

    def test_restart_bucket_dedup_future_and_malformed_times(self):
        r=report([snapshot(),snapshot(),snapshot(NOW+STEP),snapshot(START+1),snapshot(time_ms=True)])
        self.assertEqual(r["summary"]["observations"],1)
        self.assertEqual(r["invalid_snapshot_times"],2)

    def test_inputs_are_immutable(self):
        rows=[snapshot(readiness_shadow=shadow_observation([]))]; signals=[signal()]
        original=copy.deepcopy((rows,signals))
        report(rows,current=signals,shadow=signals)
        self.assertEqual((rows,signals),original)


class ReadinessHistoryAPITests(unittest.IsolatedAsyncioTestCase):
    setUp = _storage.StorageTests.setUp

    async def test_read_only_api_uses_full_history_and_preserves_database(self):
        stamp=self.now_ms//STEP*STEP
        server._save_market_snapshot(snapshot(stamp))
        s=signal(stamp-STEP+1)
        server._insert_shadow_v2_signal(s)
        # More than the public display limit; no legacy record enters setup frequency.
        for i in range(51):
            server._insert_shadow_v2_signal(dict(key="legacy"+str(i),created_ms=stamp-i))
        with server._db_connect() as conn: before=list(conn.iterdump())
        with patch.object(server, '_scan_market_rows', AsyncMock(side_effect=AssertionError('unexpected market scan'))):
            async with AsyncClient(transport=ASGITransport(app=server.app),base_url='http://test') as client:
                r=await client.get('/api/readiness-history?days=7')
                self.assertEqual(r.status_code,200)
                data=r.json()
                self.assertEqual(data['summary']['signals']['shadow']['signals'],1)
                self.assertEqual(data['excluded_records']['shadow']['legacy_unidentified'],51)
                self.assertEqual(data['summary']['observations'],1)
                for days in (0,31):self.assertEqual((await client.get('/api/readiness-history?days='+str(days))).status_code,422)
        with server._db_connect() as conn:self.assertEqual(before,list(conn.iterdump()))
        self.assertEqual(server._subscription_count(),1)
        self.assertEqual(server._load_push_events(),[])

    def test_collector_snapshot_is_additive_and_does_not_create_shadow_signals(self):
        rows=[dict(stage='RR_WAIT',ticker='TESTUSDC',rr=1.1,shadow_v2_ready=True,retest_touched=True,confirmed=True)]
        p=server._persist_scan_result({},rows)
        self.assertEqual(p['readiness_shadow'],dict(version=1,ready=1,current_rr_blocked=1,current_structure_blocked=0))
        self.assertEqual(server._load_shadow_v2_signals(),[])
        self.assertEqual(server._load_paper_signals(),[])
        self.assertEqual(server._load_push_events(),[])


class ReadinessHistoryUITests(unittest.TestCase):
    def test_actual_ui_missing_values_sample_status_flags_and_loading_race(self):
        html=Path(__file__).with_name('index.html').read_text()
        r=report([snapshot()])
        code=r"""
const fs=require('fs'),assert=require('assert'),x=JSON.parse(fs.readFileSync(0,'utf8')),html=x.html;
for(const s of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new Function(s[1]);
const nodes={};['Status','Summary','Funnel','Frequency','Notes'].forEach(k=>nodes['readinessHistory'+k]={innerHTML:'',textContent:''});
nodes.readinessHistoryDays={value:'7'};
const document={getElementById:id=>nodes[id]},fmt=(v,d)=>Number(v).toFixed(d),card=(k,v)=>k+':'+v;
eval(html.slice(html.indexOf('function lifecycleEscape('),html.indexOf('function lifecycleTime(')));
let readinessHistoryRequest=0;
eval(html.slice(html.indexOf('function renderReadinessHistory('),html.indexOf('var readinessHistorySelect=')));
renderReadinessHistory(x.report);
assert(nodes.readinessHistoryFrequency.innerHTML.includes('INSUFFICIENT SAMPLE'));
assert(nodes.readinessHistoryFunnel.innerHTML.includes('（途中）'));
assert(nodes.readinessHistoryFrequency.innerHTML.includes('>—</td>'));
assert(nodes.readinessHistoryNotes.textContent.includes('欠測 96 / 97枠'));
for(const k of ['automatic_promotion','changes_live_rules']){
 renderReadinessHistory({...x.report,[k]:true});assert.equal(nodes.readinessHistoryFunnel.innerHTML,'');
}
let pending=[],jf=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));
(async()=>{
const first=loadReadinessHistory();nodes.readinessHistoryDays.value='30';const second=loadReadinessHistory();
pending[1].resolve({...x.report,days:30});await second;pending[0].resolve(x.report);await first;
assert(nodes.readinessHistoryStatus.textContent.includes('過去30日'));
const third=loadReadinessHistory();pending[2].reject(new Error('offline'));await third;
assert(nodes.readinessHistoryStatus.textContent.includes('取得失敗'));
assert.equal(nodes.readinessHistorySummary.innerHTML,'');assert.equal(nodes.readinessHistoryFunnel.innerHTML,'');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        p=subprocess.run(['node','-e',code],input=json.dumps(dict(html=html,report=r)),text=True,capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr)
