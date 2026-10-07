"""Adjacent confirmation, original SL, causal fill and immutable control checks."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from analysis_terminal import two_close_replay as study,confirmation_zone_replay as zone,pending_entry_replay as control
from analysis_terminal.test_confirmation_zone_replay import seed
from analysis_terminal.test_pending_entry_replay import candle,START,STEP


def first(side='LONG',**changes):
    return seed(side,latest_15m_time_ms=START-STEP,**changes)


def second(side='LONG',**changes):
    return first(side,**changes)|dict(latest_15m_time_ms=START,entry_reference=102 if side=='LONG' else 98)


def confirmation(side='LONG',**changes):
    return candle(START,open=101 if side=='LONG' else 99,close=102 if side=='LONG' else 98,
                  low=100 if side=='LONG' else 97,high=103 if side=='LONG' else 100,**changes)


def record(side='LONG'):
    r,reason=study.candidate(first(side),second(side),confirmation(side))
    assert reason is None
    return r|dict(step_size=.01,min_order_size=.01,max_order_size=1000)


def evaluate(r,cs,states=None):
    seen=[]
    def state_at(stamp):
        seen.append(stamp)
        return (states or {}).get(stamp,second(r['side'])|dict(setup_id=r['setup_id']))
    return study.evaluate(r,cs,state_at,end_ms=max(c.time_ms for c in cs)+STEP),seen


class TwoCloseTests(unittest.TestCase):
    def test_both_strict_adjacent_confirmations_and_original_structure(self):
        for side in ['LONG','SHORT']:
            f,s,c=first(side),second(side),confirmation(side)
            old=copy.deepcopy((f,s));r,reason=study.candidate(f,s,c)
            self.assertIsNone(reason);self.assertEqual((f,s),old)
            self.assertEqual(r['stop'],f['shadow_stop_loss']);self.assertEqual(r['extension_target'],f['shadow_v2_extension_target'])
            self.assertEqual(r['created_ms'],START+STEP+1);self.assertEqual(r['first_created_ms'],START+1)
            self.assertAlmostEqual(control.cost_levels(r['trigger'],r['stop'],r['target'],side)['net_rr'],2)

    def test_invalid_initial_seed_cannot_be_resurrected(self):
        for changes in [dict(shadow_v2_ready=False),dict(confirmation_color_ok=False),dict(retest_touched=False),dict(stop_valid=False)]:
            with self.assertRaises(ValueError):study.candidate(first(**changes),second(),confirmation())

    def test_latest_confirmation_color_level_and_retest_are_required(self):
        for key in study.GATES:
            r,reason=study.candidate(first(),second()|{key:False},confirmation())
            self.assertIsNone(r);self.assertEqual(reason,'SECOND_CONFIRMATION_LOST')
        for side,open_,close in [('LONG',102,102),('LONG',103,102),('LONG',99,100),
                                 ('SHORT',98,98),('SHORT',97,98),('SHORT',101,100)]:
            c=candle(START,open=open_,close=close,low=min(open_,close)-1,high=max(open_,close)+1)
            _,reason=study.candidate(first(side),second(side)|dict(entry_reference=close),c)
            self.assertEqual(reason,'SECOND_CONFIRMATION_LOST')

    def test_setup_replacement_and_trend_loss_are_exclusions_not_new_attempts(self):
        for s in [second()|dict(breakout_time_ms=START),second()|dict(breakout_level=100.1),
                  second()|dict(direction=None,entry_reference=None)]:
            _,reason=study.candidate(first(),s,confirmation());self.assertEqual(reason,'SETUP_CHANGED')

    def test_original_stop_touch_invalidates_before_entry_not_an_sl_result(self):
        for side in ['LONG','SHORT']:
            c=candle(START,open=101 if side=='LONG' else 99,close=102 if side=='LONG' else 98,
                     low=90 if side=='LONG' else 97,high=103 if side=='LONG' else 110)
            r,reason=study.candidate(first(side),second(side),c)
            self.assertIsNone(r);self.assertEqual(reason,'ORIGINAL_STOP_TOUCHED_BEFORE_CONFIRMATION')

    def test_second_row_cannot_widen_stop_or_extension(self):
        r,reason=study.candidate(first(),second()|dict(shadow_stop_loss=80,shadow_v2_extension_target=1000),confirmation())
        self.assertIsNone(reason);self.assertEqual(r['stop'],90);self.assertEqual(r['extension_target'],140)
        _,reason=study.candidate(first(shadow_v2_extension_target=125),second()|dict(shadow_v2_extension_target=1000),confirmation())
        self.assertEqual(reason,'SECOND_CLOSE_NET_ROOM_INELIGIBLE')

    def test_first_fee_adjusted_room_failure_can_be_independently_assessed_at_second_close(self):
        f=first(shadow_v2_extension_target=123.2)
        self.assertIsNone(zone.candidate(f,candle_ms=START-STEP))
        c=candle(START,open=100.05,close=100.1,low=100,high=101)
        r,reason=study.candidate(f,second()|dict(entry_reference=100.1),c)
        self.assertIsNone(reason);self.assertLess(r['target'],123.2)

    def test_skipped_candle_and_mismatched_reference_cannot_confirm(self):
        for s,c in [(second()|dict(latest_15m_time_ms=START+STEP),confirmation()),
                    (second(),candle(START+STEP)),(second()|dict(entry_reference=102.1),confirmation())]:
            with self.assertRaises(ValueError):study.candidate(first(),s,c)

    def test_both_signal_candles_excluded_from_fills_and_results(self):
        r=record();old=copy.deepcopy(r)
        out,seen=evaluate(r,[candle(START-STEP,low=1,high=1000),candle(START,low=1,high=1000),
                            candle(START+STEP,open=102,low=101,high=103,close=102)])
        self.assertEqual(out['filled_ms'],START+STEP);self.assertEqual(out['status'],'OPEN')
        self.assertEqual(seen,[START+STEP]);self.assertEqual(out['mfe_r'],0);self.assertEqual(r,old)
        self.assertEqual(out['model'],study.MODEL);self.assertTrue(out['key'].startswith(study.MODEL+':'))

    def test_first_limit_fill_exit_touch_is_ambiguous_for_either_side(self):
        for side in ['LONG','SHORT']:
            r=record(side);bar=candle(START+STEP,open=102 if side=='LONG' else 98,low=1,high=1000)
            out,_=evaluate(r,[bar]);self.assertEqual(out['status'],'AMBIGUOUS');self.assertIsNone(out['final_net_r'])

    def test_later_both_exit_touch_gap_loss_and_positive_net2r_target(self):
        r=record();fill=candle(START+STEP,open=102,low=101,high=103)
        both,_=evaluate(r,[fill,candle(START+2*STEP,low=1,high=1000)])
        self.assertEqual(both['status'],'AMBIGUOUS')
        gap,_=evaluate(r,[fill,candle(START+2*STEP,open=80,low=79,high=81,close=80)])
        self.assertLess(gap['final_net_r'],-1)
        win,_=evaluate(r,[fill,candle(START+2*STEP,open=125,low=124,high=140,close=130)])
        self.assertAlmostEqual(win['final_net_r'],2);self.assertEqual(win['status'],'TP')

    def test_pending_closed_confirmation_loss_or_missing_bar_never_uses_later_success(self):
        r=record();fill=candle(START+STEP,open=102,low=101,high=103)
        invalid,_=evaluate(r,[fill],{START+STEP:second()|dict(setup_id=r['setup_id'],confirmation_level_ok=False)})
        self.assertEqual(invalid['status'],'INVALIDATED');self.assertIsNone(invalid['filled_ms'])
        gap,_=evaluate(r,[candle(START+2*STEP,high=1000)])
        self.assertEqual(gap['status'],'DATA_GAP');self.assertIsNone(gap['filled_ms'])

    def test_expiry_clock_starts_at_second_close_and_no_future_profit_sizing(self):
        r=record();cs=[candle(START+(1+i)*STEP,open=104,low=103,high=105,close=104) for i in range(4)]
        expired,_=evaluate(r,cs+[candle(START+5*STEP,low=101)])
        self.assertEqual(expired['status'],'EXPIRED');self.assertIsNone(expired['filled_ms'])
        self.assertEqual(expired['outcome_ms'],START+5*STEP)
        fill=candle(START+STEP,open=102,low=101,high=103)
        opened,_=evaluate(r,[fill]);portfolio=control.portfolio([opened])
        self.assertIsNone(portfolio['closed_portfolio_roi_pct']);self.assertIsNone(portfolio['equity_usdc'])

    def test_registered_gate_rejects_negative_results_without_tuning(self):
        def report(n,avg,pf,extra=1):return {'metrics':{study.MODEL:{'resolved':n,'avg_net_r':avg,'profit_factor':pf}},'comparison':{'net_filled_count_difference':extra}}
        self.assertEqual(study.decision([report(20,-.1,.9),report(1,2,'INF')]),'KILL_NO_LIVE_PROMOTION')
        self.assertEqual(study.decision([report(19,-1,0),report(19,-1,0)]),'CONTINUE_INSUFFICIENT_SAMPLE')
        self.assertEqual(study.decision([report(20,1,2),report(20,1,2)]),'FORWARD_SHADOW_REQUIRED')
        self.assertEqual(study.decision([report(20,1,2,0),report(20,1,2)]),'PIVOT_NO_INCREMENTAL_FILLS')

    def test_frozen_dependencies_no_server_hook_and_distinct_identity(self):
        p=json.loads(study.PROTOCOL.read_text())
        for name,digest in p['frozen_dependencies_sha256'].items():
            self.assertEqual(hashlib.sha256(study.PROTOCOL.with_name(name).read_bytes()).hexdigest(),digest)
        self.assertFalse(p['eligible_for_live_promotion']);self.assertFalse(p['automatic_promotion'])
        self.assertNotEqual(study.MODEL,zone.MODEL);self.assertNotEqual(study.MODEL,control.MODEL)
        text=Path(study.__file__).read_text();self.assertNotIn('sqlite3',text);self.assertNotIn('webpush(',text)


if __name__=='__main__':unittest.main()
