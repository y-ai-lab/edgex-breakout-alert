"""Causal pre-exit observations, censoring, denominators and frozen ledgers."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from analysis_terminal import continuation_diagnostics as diagnostic
from analysis_terminal.test_confirmation_zone_replay import record, evaluate, seed
from analysis_terminal.test_pending_entry_replay import candle, START, STEP


def filled(side='LONG'):
    r=record(side)
    bar=candle(START,open=101 if side=='LONG' else 99,low=100 if side=='LONG' else 98,
               high=102 if side=='LONG' else 100,close=101 if side=='LONG' else 99)
    return evaluate(r,[bar])[0]


def features():
    return dict(breakout_age_hours=0,signal_stop_atr4=1,signal_stop_atr15=4,
                confirmation_roll_margin_atr=.5,confirmation_body_atr=.5,trend_spread_atr4=1)


class ContinuationTests(unittest.TestCase):
    def test_signal_fill_and_terminal_extrema_cannot_inflate_preterminal_mfe(self):
        r=filled();r.update(status='SL',outcome_ms=START+3*STEP)
        cs=[candle(START-STEP,low=1,high=1000),candle(START,low=1,high=1000),
            candle(START+STEP,high=103,low=100),candle(START+2*STEP,high=1000,low=1)]
        before=copy.deepcopy(r);out=diagnostic.diagnose(r,cs,end_ms=START+3*STEP)
        self.assertEqual(out['preterminal_bars'],1)
        self.assertAlmostEqual(out['preterminal_mfe_price_r'],(103-r['entry'])/(r['entry']-r['stop']))
        self.assertEqual(out['elapsed_bars'],3);self.assertEqual(r,before)

    def test_no_safe_bar_is_unknown_not_zero_excursion(self):
        r=filled();r.update(status='SL',outcome_ms=START+2*STEP)
        out=diagnostic.diagnose(r,[candle(START),candle(START+STEP,low=89,high=150)],end_ms=START+2*STEP)
        self.assertEqual(out['preterminal_bars'],0)
        self.assertIsNone(out['preterminal_mfe_price_r']);self.assertIsNone(out['preterminal_mae_price_r'])
        self.assertIsNone(out['first_adverse_roll_close_ms'])

    def test_gap_stops_before_later_prices(self):
        r=filled()
        out=diagnostic.diagnose(r,[candle(START),candle(START+STEP),candle(START+3*STEP,high=1000)],end_ms=START+4*STEP)
        self.assertEqual(out['observation_stop'],'DATA_GAP');self.assertEqual(out['preterminal_bars'],1)
        self.assertLess(out['preterminal_mfe_price_r'],1)
        r.update(status='DATA_GAP',outcome_ms=START+3*STEP)
        self.assertEqual(diagnostic.diagnose(r,[candle(START),candle(START+STEP)],end_ms=START+5*STEP)['preterminal_bars'],1)

    def test_open_is_censored_and_forming_candle_is_excluded(self):
        r=filled();out=diagnostic.diagnose(r,[candle(START),candle(START+STEP),
             candle(START+2*STEP,high=1000)],end_ms=START+2*STEP)
        self.assertEqual(out['preterminal_bars'],1);self.assertIsNone(out['elapsed_bars'])
        self.assertEqual(out['observation_stop'],'PERIOD_END_CENSORED')

    def test_roll_failure_requires_adverse_close_and_keeps_exact_level_separate(self):
        for side in ['LONG','SHORT']:
            r=filled(side);roll=r['roll_level'];bad=99 if side=='LONG' else 101
            cs=[candle(START),candle(START+STEP,open=roll,close=roll,low=98,high=102),
                candle(START+2*STEP,open=100,close=bad,low=98,high=102)]
            out=diagnostic.diagnose(r,cs,end_ms=START+3*STEP)
            self.assertEqual(out['first_equal_roll_close_ms'],START+2*STEP)
            self.assertEqual(out['first_adverse_roll_close_ms'],START+3*STEP)

    def test_terminal_roll_loss_cannot_be_called_preceding_failure(self):
        r=filled();r.update(status='SL',outcome_ms=START+3*STEP)
        out=diagnostic.diagnose(r,[candle(START),candle(START+STEP),
             candle(START+2*STEP,open=100,low=89,close=90)],end_ms=START+3*STEP)
        self.assertIsNone(out['first_adverse_roll_close_ms'])

    def test_fixed_latency_and_excursion_boundaries_include_all_bins(self):
        self.assertEqual([diagnostic.latency_bucket(n) for n in [4,5,16,17,96,97]],
                         ['<=_4_bars','<=_16_bars','<=_16_bars','<=_96_bars','<=_96_bars','>_96_bars'])
        self.assertEqual([diagnostic.mfe_bucket(n) for n in [.49,.5,.99,1]],['<_0.5','[0.5,1)','[0.5,1)','>=_1'])

    def test_signal_features_require_matching_identity_and_closed_breakout(self):
        r=record();row=seed();row.update(atr_4h=10,atr_15m=2,confirmation_roll_margin_atr=.5,
            confirmation_body_atr=.5,ema20_4h=101,ema50_4h=99)
        out=diagnostic.signal_features(r,row);self.assertEqual(out['breakout_age_hours'],0)
        self.assertAlmostEqual(out['signal_stop_atr15'],5.5)
        for change in [dict(breakout_time_ms=START),dict(atr_4h=0),dict(shadow_stop_loss=89),dict(entry_reference=102)]:
            with self.assertRaises(ValueError):diagnostic.signal_features(r,row|change)

    def test_summary_keeps_censored_and_missing_path_denominators(self):
        sl=filled();sl.update(status='SL',outcome_ms=START+2*STEP)
        opened=filled();pending=record()
        rows=[diagnostic.diagnose(r,[candle(START),candle(START+STEP)],end_ms=START+2*STEP)
              | dict(signal_features=features()) for r in [sl,opened,pending]]
        s=diagnostic.summarize(rows)
        self.assertEqual(s['filled'],2);self.assertEqual(s['statuses'],{'SL':1,'OPEN':1,'PENDING':1})
        self.assertEqual(s['groups']['SL']['without_preterminal_path'],1)
        self.assertEqual(sum(s['groups']['SL']['preterminal_mfe_bins'].values()),0)
        self.assertEqual(s['unfilled_excluded_from_path'],1)

    def test_cost_share_uses_nominal_price_distance_not_price_r_unit(self):
        for side in ['LONG','SHORT']:
            r=filled(side);out=diagnostic.diagnose(r,[candle(START)],end_ms=START+STEP)
            expected=(r['net_risk']-abs(r['nominal_fill']-r['stop']))/r['net_risk']
            self.assertAlmostEqual(out['stop_cost_share'],expected);self.assertGreater(expected,0)

    def test_invalid_clock_and_duplicate_candles_rejected(self):
        r=filled()
        with self.assertRaises(ValueError):diagnostic.diagnose(r,[candle(START),candle(START)],end_ms=START+STEP)
        with self.assertRaises(ValueError):diagnostic.diagnose(r|dict(filled_ms=START-STEP),[],end_ms=START+STEP)

    def test_modified_or_unknown_frozen_ledger_is_rejected_before_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'ledger.json';p.write_text(json.dumps({'role':'development','start_ms':0,'end_ms':STEP}))
            with self.assertRaisesRegex(ValueError,'Unregistered cohort'):
                diagnostic.build(Path(directory),p,None,None)
        p=json.loads(diagnostic.PROTOCOL.read_text())
        self.assertFalse(p['changes_live_rules']);self.assertFalse(p['eligible_for_live_promotion'])
        self.assertFalse(p['untouched_validation'])


if __name__=='__main__':unittest.main()
