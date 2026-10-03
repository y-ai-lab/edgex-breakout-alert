from dataclasses import asdict,replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from analysis_terminal.chronology_replay import PERIODS,post_breakout_retest,replay_comparison,variant_analyzer
from analysis_terminal.replay import replay_contract,replay_report,rule_fingerprint
from analysis_terminal.test_replay import CONTRACT,START,STEP,MONITOR,c,dataset,analyzer,server


def analyze_fixture():
    original=analyzer(breakout_time_ms=START-MONITOR,atr_15m=2)
    def analyze(contract,monitor,entries,*,as_of_ms):
        row=original(contract,monitor,entries,as_of_ms=as_of_ms)
        entry=100+max(0,(as_of_ms-1-START)//STEP)
        row.update(entry_reference=entry,shadow_v2_target=entry+2*(entry-90),shadow_v2_extension_target=150,
                   shadow_v2_room_rr=(150-entry)/(entry-90))
        return row
    return analyze


def run_replay(analyze,end=START+4*STEP,entries=None):
    m,e=dataset(end)
    return replay_contract(CONTRACT,m,entries or e,analyze,start_ms=START,end_ms=end,monitor_window=2,entry_window=4)


class ChronologyReplayTests(unittest.TestCase):
    def test_delayed_first_entry_is_recomputed_with_new_prices(self):
        analyze=analyze_fixture();baseline=run_replay(analyze);variant=run_replay(variant_analyzer(analyze,server.SETTINGS))
        b,v=baseline['shadow'][0],variant['shadow'][0]
        self.assertEqual(v['created_ms'],b['created_ms']+STEP)
        self.assertEqual((b['entry'],v['entry']),(100,101))
        self.assertEqual((b['target'],v['target']),(120,123))
        self.assertEqual(b['stop'],v['stop'])
        self.assertEqual(v['created_ms'],v['signal_candle_ms']+STEP+1)
        self.assertEqual(baseline['current'],variant['current'])
        self.assertEqual(len(variant['shadow']),1)

    def test_breakout_close_boundary_long_short_and_no_predating_touch(self):
        row=analyze_fixture()(CONTRACT,[],[],as_of_ms=START+STEP+1)
        for side in ('LONG','SHORT'):
            row['direction']=side
            self.assertFalse(post_breakout_retest(row,[c(START-STEP)],server.SETTINGS))
            self.assertTrue(post_breakout_retest(row,[c(START)],server.SETTINGS))
        row['direction']='LONG'
        self.assertFalse(post_breakout_retest(row,[c(START,low=103,open=104,close=104)],server.SETTINGS))

    def test_old_warmup_entry_can_become_new_variant_entry_in_reporting_period(self):
        analyze=analyzer(ready_from=START-STEP,breakout_time_ms=START-MONITOR-STEP,atr_15m=2)
        b=run_replay(analyze);v=run_replay(variant_analyzer(analyze,server.SETTINGS))
        self.assertEqual(b['shadow'],[])
        self.assertEqual(v['shadow'][0]['created_ms'],START+1)

    def test_no_loosening_of_confirmation_room_or_other_baseline_conditions(self):
        analyze=analyzer(shadow_v2_ready=False,atr_15m=2)
        self.assertEqual(run_replay(variant_analyzer(analyze,server.SETTINGS))['shadow'],[])

    def test_signal_wick_and_future_bars_do_not_resolve_delayed_entry(self):
        analyze=variant_analyzer(analyze_fixture(),server.SETTINGS)
        _,e=dataset();e=[replace(x,high=200,low=1) if x.time_ms==START else x for x in e]
        r=run_replay(analyze,entries=e)
        self.assertEqual(r['shadow'][0]['result']['status'],'OPEN')
        end=START+4*STEP
        future=[replace(x,high=200,low=1) if x.time_ms+STEP>end else x for x in e]
        self.assertEqual(r,run_replay(analyze,entries=future))
        e=[replace(x,high=200,low=1) if x.time_ms==START+STEP else x for x in e]
        self.assertEqual(run_replay(analyze,entries=e)['shadow'][0]['result']['status'],'AMBIGUOUS')

    def test_gap_before_first_variant_entry_is_not_silently_accepted(self):
        analyze=variant_analyzer(analyze_fixture(),server.SETTINGS)
        _,e=dataset();e=[x for x in e if x.time_ms!=START]
        r=run_replay(analyze,end=START+8*STEP,entries=e)
        self.assertEqual(r['shadow'],[])
        self.assertGreater(r['excluded_points']['incomplete_indicator_window'],0)

    def test_full_artifact_baseline_and_fixed_period_are_verified(self):
        analyze=analyze_fixture();m,e=dataset()
        raw=json.dumps(dict(contract=asdict(CONTRACT),HOUR_4=[asdict(x) for x in m],MINUTE_15=[asdict(x) for x in e])).encode()
        manifest=dict(rule_fingerprint=rule_fingerprint(analyze,server.SETTINGS),indicator_windows={'HOUR_4':2,'MINUTE_15':4},
                      sources=[dict(file='candles.json',sha256=hashlib.sha256(raw).hexdigest())])
        report=replay_report([run_replay(analyze)],start_ms=START,end_ms=START+4*STEP,manifest=manifest)
        with tempfile.TemporaryDirectory() as directory,patch.dict(PERIODS,EXPLORATORY=(START,START+4*STEP)):
            p=Path(directory);(p/'candles.json').write_bytes(raw);(p/'replay-report.json').write_text(json.dumps(report))
            result=replay_comparison(p,analyze,server.SETTINGS,role='EXPLORATORY')
            self.assertFalse(result['eligible_for_live_promotion'])
            self.assertEqual(result['entry_changes']['delayed'],1)
            self.assertEqual(result['comparison']['current'],report['comparison']['shadow'])
            with self.assertRaises(ValueError):replay_comparison(p,analyze,server.SETTINGS,role='UNINSPECTED_RETROSPECTIVE')
            (p/'candles.json').write_bytes(raw+b' ')
            with self.assertRaises(ValueError):replay_comparison(p,analyze,server.SETTINGS,role='EXPLORATORY')


class FrozenUniverseTests(unittest.IsolatedAsyncioTestCase):
    async def test_archived_universe_skips_live_contract_discovery(self):
        from analysis_terminal import run_replay as runner
        with tempfile.TemporaryDirectory() as directory,patch.object(server.CLIENT,'get_contracts',AsyncMock(side_effect=AssertionError('No discovery'))),patch.object(runner,'fetch_history',AsyncMock(return_value=[])),patch('builtins.print'):
            report=await runner.run(1000*MONITOR,7,Path(directory),universe={'1':CONTRACT})
            self.assertEqual(report['coverage']['requested_markets'],1)
            self.assertEqual(report['coverage']['failed_markets'],0)
