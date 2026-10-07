"""Missed-price diagnostics must never manufacture fills or use future states."""
import copy
from pathlib import Path
import unittest

from analysis_terminal import pending_fill_diagnostics as diag
from analysis_terminal.test_pending_entry_replay import record,candle,row,evaluate,START,STEP


def expired(side='LONG'):
    r=record(side)
    cs=[candle(START+i*STEP,open=101 if side=='LONG' else 99,
               low=100 if side=='LONG' else 98,high=102 if side=='LONG' else 100,
               close=101 if side=='LONG' else 99) for i in range(4)]
    return evaluate(r,cs),cs


def diagnostic(r,cs,states=None,end=None):
    stamps=[]
    def state_at(stamp):
        stamps.append(stamp)
        return (states or {}).get(stamp,{'setup_id':r['setup_id']})
    out=diag.diagnose(r,row(r['side']),cs,state_at,end_ms=end or START+8*STEP)
    return out,stamps


class FillDiagnosticsTests(unittest.TestCase):
    def test_original_records_are_immutable_and_late_touch_is_not_fill(self):
        r,cs=expired();before=copy.deepcopy(r)
        cs.append(candle(START+4*STEP,low=r['trigger']-.01,high=102))
        out,stamps=diagnostic(r,cs)
        self.assertEqual(r,before)
        self.assertEqual(out['later_observation'],'LATE_PRICE_TOUCH')
        self.assertIsNone(out['original_filled_ms'])
        self.assertEqual(out['late_touch_open_ms'],START+4*STEP)
        self.assertFalse(out['late_touch_exit_same_bar'])
        self.assertEqual(stamps,list(range(START,START+5*STEP,STEP)))
        summary=diag.summarize([out])
        self.assertEqual(summary['late_price_touches'],1)
        for field in ['counterfactual_fills','counterfactual_win_rate','counterfactual_avg_r','counterfactual_portfolio_roi_pct']:
            self.assertIsNone(summary[field])

    def test_signal_bar_extremes_cannot_be_counted_as_touch_or_result(self):
        r,cs=expired();cs.insert(0,candle(START-STEP,low=1,high=1000))
        cs.extend(candle(START+i*STEP) for i in range(4,8))
        out,stamps=diagnostic(r,cs)
        self.assertEqual(out['later_observation'],'NO_TOUCH_IN_FIXED_EXTRA_WINDOW')
        self.assertNotIn(START-STEP,stamps)
        self.assertFalse(out['target_touched_before_expiry'])

    def test_signal_close_geometry_and_roll_recrossing_are_not_tp_results(self):
        r,cs=expired();item=row();item['entry_reference']=125
        out=diag.diagnose(r,item,cs,lambda _:dict(setup_id=r['setup_id']),end_ms=START+4*STEP)
        self.assertTrue(out['target_behind_signal_close'])
        self.assertFalse(out['trigger_on_confirmed_side_of_roll'])
        self.assertEqual(out['original_status'],'EXPIRED')
        self.assertEqual(out['later_observation'],'RIGHT_CENSORED')

    def test_target_before_late_touch_and_same_bar_exit_are_separate_unknowns(self):
        r,cs=expired();cs[0]=candle(START,high=125,close=120)
        cs.append(candle(START+4*STEP,low=89,high=125))
        out,_=diagnostic(r,cs)
        self.assertTrue(out['target_touched_before_expiry'])
        self.assertTrue(out['late_touch_after_prior_target'])
        self.assertTrue(out['late_touch_exit_same_bar'])
        self.assertEqual(diag.summarize([out])['late_touches_without_prior_target_or_same_bar_exit'],0)
        self.assertEqual(out['original_status'],'EXPIRED')

    def test_new_setup_missing_state_gap_and_stop_open_halt_later_observation(self):
        for states,bar,status in [({START+4*STEP:{'setup_id':'new'}},candle(START+4*STEP,low=95),'SETUP_CHANGED'),
                                 ({START+4*STEP:None},candle(START+4*STEP,low=95),'DATA_GAP'),
                                 ({},None,'DATA_GAP'),
                                 ({},candle(START+4*STEP,open=89,low=88,high=101,close=90),'OPEN_THROUGH_STOP')]:
            r,cs=expired()
            if bar:cs.append(bar)
            out,_=diagnostic(r,cs,states)
            self.assertEqual(out['later_observation'],status)
            self.assertIsNone(out['late_touch_open_ms'])

    def test_unknown_original_window_and_preexpiry_touch_fail_closed(self):
        r,cs=expired()
        for bars,states in [(cs[1:],None),(cs,{START:None}),
                            ([candle(START,low=95)]+cs[1:],None),
                            (cs,{START:{'setup_id':'other'}})]:
            with self.assertRaises(ValueError):diagnostic(r,bars,states)

    def test_short_touch_uses_high_and_original_four_bar_boundary(self):
        r,cs=expired('SHORT');cs.append(candle(START+4*STEP,open=99,low=98,high=105,close=99))
        out,_=diagnostic(r,cs)
        self.assertEqual(out['later_observation'],'LATE_PRICE_TOUCH')
        self.assertFalse(out['trigger_on_confirmed_side_of_roll'])
        self.assertGreater(out['closest_distance_before_expiry_r'],0)
        r['expires_ms']+=STEP
        with self.assertRaises(ValueError):diagnostic(r,cs)

    def test_current_filled_results_stay_original_and_are_not_second_samples(self):
        r=record();r=evaluate(r,[candle(START,low=95),candle(START+STEP,low=89)])
        out,stamps=diagnostic(r,[])
        self.assertEqual(out['original_status'],'SL')
        self.assertEqual(out['original_filled_ms'],START)
        self.assertEqual(stamps,[])
        self.assertIsNone(out['later_observation'])
        self.assertEqual(diag.summarize([out])['expired'],0)

    def test_all_fixed_distance_bins_are_descriptive_not_strategy_selection(self):
        self.assertEqual([diag.distance_bin(v) for v in [.1,.25,.5,1,2]],['<= 0.25R','<= 0.25R','(0.25, 0.5]R','(0.5, 1]R','> 1R'])
        source=Path(diag.__file__).read_text()
        self.assertNotIn('webpush(',source);self.assertNotIn('sqlite3',source)
        self.assertNotIn('study.WAIT_BARS =',source)


class SourceIntegrityTests(unittest.TestCase):
    def test_exact_source_reconstruction_immutability_and_fail_closed_integrity(self):
        import hashlib
        import json
        import tempfile
        from dataclasses import asdict
        from analysis_terminal.test_storage import server
        from analysis_terminal.test_execution_funnel import report
        from analysis_terminal.test_pending_entry_replay import CONTRACT
        from analysis_terminal.replay import rule_fingerprint,strategy_parameters
        def analyze(*args,**kwargs):return row()
        settings=server.SETTINGS
        r,cs=expired();cs.extend(candle(START+i*STEP) for i in range(4,8))
        frames={'MINUTE_15':[asdict(candle(START+i*STEP)) for i in range(-180,8)],
                'HOUR_4':[asdict(candle(START+i*16*STEP)) | {'interval':'HOUR_4'} for i in range(-180,0)]}
        fingerprint=rule_fingerprint(analyze,settings)
        ledger=report([r],end=START+8*STEP)
        ledger.update(source_rule_fingerprint=fingerprint,production_rule_fingerprint=fingerprint)
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder);data=source/'candles.json'
            data.write_text(json.dumps(dict(contract=asdict(CONTRACT),**frames)))
            manifest={'dataset':'RETROSPECTIVE','eligible_for_live_promotion':False,'start_ms':START,'end_ms':START+8*STEP,
                      'manifest':{'rule_fingerprint':fingerprint,'parameters':strategy_parameters(settings),
                                  'sources':[{'ticker':'TESTUSDC','file':'candles.json','sha256':hashlib.sha256(data.read_bytes()).hexdigest()}]}}
            source_report=source/'replay-report.json';source_report.write_text(json.dumps(manifest))
            ledger_path=source/'report.json';ledger_path.write_text(json.dumps(ledger))
            before={p:p.read_bytes() for p in [data,source_report,ledger_path]}
            result=diag.build(source,ledger_path,analyze,settings)
            self.assertEqual(result['source_files_verified'],1)
            self.assertEqual(result['original_candidates_reproduced'],1)
            self.assertEqual(result['summary']['expired'],1)
            self.assertEqual(result['original_metrics'],ledger['metrics'][r['model']])
            self.assertEqual(result['original_portfolio'],ledger['portfolios'][r['model']])
            for p,raw in before.items():self.assertEqual(p.read_bytes(),raw)
            data.write_text(data.read_text()+' ')
            with self.assertRaisesRegex(ValueError,'checksum'):diag.build(source,ledger_path,analyze,settings)
            data.write_bytes(before[data])
            manifest['start_ms']+=STEP;source_report.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError,'mismatch'):diag.build(source,ledger_path,analyze,settings)
            source_report.write_bytes(before[source_report]);ledger['records'][0]['trigger']+=.1
            ledger_path.write_text(json.dumps(ledger))
            with self.assertRaises(ValueError):diag.build(source,ledger_path,analyze,settings)
