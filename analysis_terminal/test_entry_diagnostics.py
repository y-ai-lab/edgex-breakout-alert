from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from analysis_terminal.entry_diagnostics import BINS, bucket, build_diagnostics, diagnostic_summary, entry_features
from analysis_terminal.replay import rule_fingerprint
from analysis_terminal.comparison import metrics
from analysis_terminal.setups import setup_identity
from analysis_terminal.test_replay import CONTRACT, START, STEP, MONITOR, analyzer, dataset, server
from analysis_terminal import test_storage as _storage


def fixture_analyzer():
    return analyzer(breakout_time_ms=START-MONITOR, atr_4h=5, atr_15m=2,
                    ema20_4h=102, ema50_4h=99, confirmation_body_atr=.6,
                    confirmation_roll_margin_atr=.5, volume_ratio=1.1)


def fixture_signal(close_ms, analyze=None):
    row=(analyze or fixture_analyzer())(CONTRACT,[],[],as_of_ms=close_ms+1)
    return dict(key='replay-fixture',ticker=CONTRACT.contract_name,side='LONG',
                breakout_time_ms=row['breakout_time_ms'],breakout_level=row['breakout_level'],
                setup_id=setup_identity(row),created_ms=close_ms+1,signal_candle_ms=close_ms-STEP,
                entry=100,stop=90,target=120,room_rr=4,extension_target=140,
                result=dict(status='OPEN',final_r=None,evaluation_version=2,coverage_complete=True))


class EntryDiagnosticsTests(unittest.TestCase):
    def feature(self, close=START, entries=None, signal=None):
        m,e=dataset()
        return entry_features(signal or fixture_signal(close),CONTRACT,m,entries or e,fixture_analyzer(),
                              server.SETTINGS,{'HOUR_4':2,'MINUTE_15':4})

    def test_breakout_close_boundary_and_same_candle_confirmation_are_distinct(self):
        cases=[(START,'ONLY_BEFORE_BREAKOUT_CLOSE'),
               (START+STEP,'ONLY_CONFIRMATION_CANDLE_AFTER_BREAKOUT'),
               (START+2*STEP,'PRIOR_CANDLE_AFTER_BREAKOUT')]
        for close,label in cases:
            with self.subTest(close=close):
                f=self.feature(close)
                self.assertEqual(f['retest_chronology'],label)
                self.assertEqual(f['breakout_close_ms'],START)
                self.assertTrue(all(t>=START for t in f['post_breakout_retest_candle_ms']))

    def test_post_breakout_non_touch_does_not_relabel_prior_touch(self):
        _,e=dataset()
        e=[replace(c,low=101,high=105,open=102,close=102) if c.time_ms>=START else c for c in e]
        self.assertEqual(self.feature(START+STEP,entries=e)['retest_chronology'],'ONLY_BEFORE_BREAKOUT_CLOSE')

    def test_future_bars_and_outcomes_cannot_change_entry_features(self):
        _,e=dataset()
        future=[replace(c,high=1000,low=1) if c.time_ms+STEP>START else c for c in e]
        signal=fixture_signal(START);signal['result']=dict(status='SL',final_r=-1)
        self.assertEqual(self.feature(),self.feature(entries=future,signal=signal))

    def test_wrong_identity_clock_prices_grid_or_history_are_rejected(self):
        for values in ({'created_ms':START},{'setup_id':'wrong'},{'entry':101}):
            with self.subTest(values=values),self.assertRaises(ValueError):
                self.feature(signal=dict(fixture_signal(START),**values))
        _,e=dataset()
        for rows in ([c for c in e if c.time_ms!=START-STEP],e[::-1],
                     [replace(c,contract_id='2') for c in e]):
            with self.assertRaises(ValueError):self.feature(entries=rows)

    def test_buckets_use_predeclared_boundaries_and_open_stays_in_denominator(self):
        self.assertEqual([bucket(v,(.5,1)) for v in (.49,.5,.99,1)],['<0.5','[0.5,1)','[0.5,1)','>=1'])
        base=dict(fixture_signal(START),entry_features=self.feature())
        tp=dict(base,result=dict(status='TP',final_r=2,evaluation_version=2,coverage_complete=True,outcome_time_ms=START+STEP))
        summary=diagnostic_summary([base,tp])
        self.assertEqual((summary['metrics']['signals'],summary['metrics']['resolved'],summary['metrics']['open']),(2,1,1))
        for rows in summary['strata'].values():
            self.assertEqual(sum(s['signals'] for s in rows),2)
            self.assertEqual(sum(s['resolved'] for s in rows),1)
            self.assertTrue(all(s['sample_status']=='INSUFFICIENT SAMPLE' for s in rows))
        self.assertEqual(summary['feature_distributions']['TP']['features']['room_rr']['samples'],1)

    def test_artifact_hash_fingerprint_and_original_cohort_are_validated(self):
        analyze=fixture_analyzer();m,e=dataset();signal=fixture_signal(START,analyze)
        payload=dict(contract=asdict(CONTRACT),HOUR_4=[asdict(c) for c in m],MINUTE_15=[asdict(c) for c in e])
        raw=json.dumps(payload).encode()
        report=dict(dataset='RETROSPECTIVE',eligible_for_live_promotion=False,start_ms=START,end_ms=START+4*STEP,
                    manifest=dict(rule_fingerprint=rule_fingerprint(analyze,server.SETTINGS),indicator_windows={'HOUR_4':2,'MINUTE_15':4},
                                  sources=[dict(ticker=CONTRACT.contract_name,file='candles.json',sha256=hashlib.sha256(raw).hexdigest())]),
                    signals=dict(shadow=[signal]),comparison=dict(shadow=metrics([signal])))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);(path/'candles.json').write_bytes(raw)
            def write(): (path/'replay-report.json').write_text(json.dumps(report))
            write();result=build_diagnostics(path,analyze,server.SETTINGS)
            self.assertFalse(result['eligible_for_live_promotion'])
            self.assertEqual(result['reconstructed_entries'],1)
            report['signals']['shadow'].append(signal);write()
            with self.assertRaises(ValueError):build_diagnostics(path,analyze,server.SETTINGS)
            report['signals']['shadow']=[signal];report['manifest']['rule_fingerprint']='wrong';write()
            with self.assertRaises(ValueError):build_diagnostics(path,analyze,server.SETTINGS)
            report['manifest']['rule_fingerprint']=rule_fingerprint(analyze,server.SETTINGS);write()
            (path/'candles.json').write_bytes(raw+b' ')
            with self.assertRaises(ValueError):build_diagnostics(path,analyze,server.SETTINGS)


class EntryDiagnosticsAPITests(unittest.IsolatedAsyncioTestCase):
    setUp = _storage.StorageTests.setUp

    async def test_static_diagnostics_preserve_live_database_and_gates(self):
        with server._db_connect() as conn:before=list(conn.iterdump())
        with patch.object(server,'_scan_market_rows',side_effect=AssertionError('No market scan')):
            async with AsyncClient(transport=ASGITransport(app=server.app),base_url='http://test') as client:
                response=await client.get('/api/entry-diagnostics')
                self.assertEqual(response.status_code,200)
                result=response.json()
                self.assertEqual(result['protocol'],'entry_loss_diagnostics_v1')
                self.assertEqual(result['reconstructed_entries'],235)
                self.assertFalse(result['eligible_for_live_promotion'])
                self.assertEqual((await client.get('/api/strategy-comparison')).json()['shadow']['resolved'],0)
        with server._db_connect() as conn:self.assertEqual(before,list(conn.iterdump()))


class EntryDiagnosticsUITests(unittest.TestCase):
    def test_actual_script_renders_all_groups_and_marks_small_samples(self):
        html=Path(__file__).with_name('index.html').read_text()
        report=json.loads(Path(__file__).with_name('entry_diagnostics_latest.json').read_text())
        script=r'''
const fs=require('fs'),assert=require('assert'),input=JSON.parse(fs.readFileSync(0,'utf8')),html=input.html;
for(const m of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g))new Function(m[1]);
const elements={entryDiagnosticsStatus:{textContent:''},entryDiagnosticsBody:{innerHTML:''}};
const document={getElementById:id=>elements[id]},fmt=(v,d)=>Number(v).toFixed(d);
eval(html.slice(html.indexOf('function renderEntryDiagnostics('),html.indexOf('function lifecycleTime(')));
renderEntryDiagnostics(input.report);
assert(elements.entryDiagnosticsStatus.textContent.includes('235 Entry再構成'));
assert(elements.entryDiagnosticsBody.innerHTML.includes('確定後retest足なし'));
assert(elements.entryDiagnosticsBody.innerHTML.includes('87 / 63'));
assert(elements.entryDiagnosticsBody.innerHTML.includes('-0.714'));
assert(elements.entryDiagnosticsBody.innerHTML.includes('INSUFFICIENT SAMPLE'));
assert(elements.entryDiagnosticsBody.innerHTML.includes('0 / 0'));
const rows=Object.values(input.report.summary.strata).flat().length;
assert((elements.entryDiagnosticsBody.innerHTML.match(/<tr>/g)||[]).length===rows);
input.report.summary.strata.side[0].group='<script>bad</script>';
renderEntryDiagnostics(input.report);
assert(!elements.entryDiagnosticsBody.innerHTML.includes('<script>'));
assert(elements.entryDiagnosticsBody.innerHTML.includes('&lt;script&gt;'));
renderEntryDiagnostics({...input.report,eligible_for_live_promotion:true});
assert(elements.entryDiagnosticsBody.innerHTML==='');
assert(html.includes("jf('/api/entry-diagnostics')"));
console.log('Entry diagnostics syntax/render/separation passed');
'''
        p=subprocess.run(['node','-e',script],input=json.dumps(dict(html=html,report=report)),capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
