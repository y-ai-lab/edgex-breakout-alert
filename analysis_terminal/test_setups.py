import unittest
import subprocess
from pathlib import Path

from analysis_terminal.setups import first_per_setup, later_ready_time, ready_times_by_setup, setup_identity


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(ticker="TESTUSDC", direction="LONG", breakout_time_ms=14400000, breakout_level=100)

    def test_identity_stays_stable_as_confirmation_and_prices_change(self):
        expected = setup_identity(self.row)
        self.assertEqual(expected, setup_identity(dict(self.row, breakout_level="1e2", stage="READY", latest_15m_time_ms=900000, entry_reference=101)))
        self.assertEqual(expected, setup_identity(dict(self.row, breakout_level=100.0, stage="RR_WAIT", latest_15m_time_ms=1800000)))

    def test_identity_changes_for_each_setup_component(self):
        for delta in (dict(ticker="OTHERUSDC"), dict(direction="SHORT"), dict(breakout_time_ms=28800000), dict(breakout_level=101)):
            self.assertNotEqual(setup_identity(self.row), setup_identity(dict(self.row, **delta)))

    def test_incomplete_or_invalid_legacy_metadata_is_not_inferred(self):
        for delta in (dict(breakout_time_ms=None), dict(breakout_time_ms=0), dict(breakout_level=None), dict(breakout_level=float("nan")), dict(breakout_level=float("inf")), dict(breakout_level=0), dict(direction=None), dict(ticker="")):
            self.assertIsNone(setup_identity(dict(self.row, **delta)))
        self.assertIsNone(setup_identity(dict(ticker="TESTUSDC", direction="LONG")))

    def test_ready_matches_same_setup_and_bounded_time_only(self):
        event = dict(ticker="TESTUSDC", setup_id="first", created_ms=100)
        times = ready_times_by_setup([
            dict(ticker="TESTUSDC", setup_id="second", kind="READY", created_ms=200),
            dict(ticker="TESTUSDC", setup_id="first", kind="READY", created_ms=300),
            dict(ticker="TESTUSDC", kind="READY", created_ms=150),
        ])
        self.assertIsNone(later_ready_time(event, times, 250))
        self.assertEqual(later_ready_time(event, times, 300), 300)
        self.assertIsNone(later_ready_time(dict(event, setup_id=None), times, 400))
        self.assertIsNone(later_ready_time(dict(event, created_ms=300), times, 400))

    def test_dedup_selects_first_entry_not_first_resolved_record(self):
        early = dict(setup_id="same", created_ms=1, result=None)
        late = dict(setup_id="same", created_ms=2, result=dict(status="TP"))
        other = dict(setup_id="other", created_ms=3)
        self.assertEqual(first_per_setup([late, other, early, dict(created_ms=0)]), [early, other])

    def test_browser_transitions_and_journal_use_identity_without_migration_replay(self):
        js = r"""
const fs=require('fs'),assert=require('assert');
const script=fs.readFileSync(process.argv[1],'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new Function(script);
function extract(a,b){eval.call(null,script.slice(script.indexOf(a),script.indexOf(b,script.indexOf(a))))}
global.candidateState={initialized:true,ready:[],near:['TESTUSDC']};
global.localStorage={setItem(){}};
global.alerts=[];global.pushCandidateAlert=x=>alerts.push(x);global.stageJa=x=>x;
extract('function processCandidateTransitions(', 'function syncCandidateNotifyButton(');
const first={ticker:'TESTUSDC',setup_id:'first',stage:'CONFIRMATION_WAIT'};
processCandidateTransitions({qualified_near_candidates:[first]});assert.equal(alerts.length,0);
processCandidateTransitions({ready_candidates:[{...first,stage:'READY'}]});assert.equal(alerts.length,1);
assert(alerts[0].label.includes('昇格'));assert.equal(alerts[0].setup_id,'first');
processCandidateTransitions({ready_candidates:[{...first,stage:'READY'}]});assert.equal(alerts.length,1);
processCandidateTransitions({ready_candidates:[{...first,stage:'READY',setup_id:'second'}]});assert.equal(alerts.length,2);
assert.equal(alerts[1].label,'新しくエントリー可能');
global.autoJournalReady=true;global.journal=[];global.saveJournalStore=()=>{};
extract('function autoJournalReadyCandidates(', 'async function scan(');
const row={...first,stage:'READY',direction:'LONG',entry_reference:100,stop_loss:90,take_profit:120,latest_15m_time_ms:900000};
assert.equal(autoJournalReadyCandidates([row]),1);
assert.equal(autoJournalReadyCandidates([{...row,latest_15m_time_ms:1800000,entry_reference:101}]),0);
assert.equal(journal[0].entry,100);assert.equal(journal[0].created_ms,1800001);
assert.equal(autoJournalReadyCandidates([{...row,setup_id:'second'}]),1);
assert.equal(autoJournalReadyCandidates([{...row,setup_id:undefined}]),0);
"""
        subprocess.run(["node", "-e", js, str(Path(__file__).with_name("index.html"))], capture_output=True, text=True, check=True)
