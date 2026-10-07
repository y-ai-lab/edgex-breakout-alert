"""Net target geometry, causal fill prices, frozen SL/TP and kill gate checks."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from analysis_terminal import confirmation_zone_replay as zone,pending_entry_replay as control
from analysis_terminal.test_pending_entry_replay import row,candle,START,STEP


def seed(side='LONG',**changes):
    r=row(side);r.update(confirmation_color_ok=True,confirmation_level_ok=True,stop_valid=True,
                        shadow_v2_ready=True,shadow_stop_loss=90 if side=='LONG' else 110,
                        shadow_v2_extension_target=140 if side=='LONG' else 60)
    r.update(changes);return r


def record(side='LONG'):
    return zone.candidate(seed(side),candle_ms=START-STEP) | dict(step_size=.01,min_order_size=.01,max_order_size=1000)


def evaluate(r,cs,states=None,end=None):
    stamps=[]
    def state_at(stamp):
        stamps.append(stamp)
        return (states or {}).get(stamp,seed(r['side']))
    return zone.evaluate(r,cs,state_at,end_ms=end or max(c.time_ms for c in cs)+STEP),stamps


class ZoneTests(unittest.TestCase):
    def test_fixed_net_target_pays_two_r_after_both_fees_and_adverse_slippages(self):
        for side,reference,stop in [('LONG',101,90),('SHORT',99,110)]:
            target=zone.net_target(reference,stop,side)
            self.assertAlmostEqual(control.cost_levels(reference,stop,target,side)['net_rr'],2)
            gross=(reference+2*(reference-stop))
            self.assertGreater(target,gross) if side=='LONG' else self.assertLess(target,gross)
            r=record(side);self.assertEqual(r['stop'],stop);self.assertEqual(r['trigger'],reference)
            self.assertEqual(r['created_ms'],START+1)
        for side,reference,stop in [('NO',101,90),('LONG',90,90),('SHORT',120,110),('LONG',float('nan'),90)]:
            self.assertIsNone(zone.net_target(reference,stop,side))

    def test_initial_confirmation_room_and_exact_roll_price_are_strict_gates(self):
        for changes in [dict(confirmation_color_ok=False),dict(confirmation_level_ok=False),
                        dict(retest_touched=False),dict(stop_valid=False),dict(shadow_v2_ready=False),
                        dict(entry_reference=100),dict(shadow_v2_extension_target=120),dict(entry_reference=True)]:
            self.assertIsNone(zone.candidate(seed(**changes),candle_ms=START-STEP))
        target=zone.net_target(101,90,'LONG')
        self.assertIsNotNone(zone.candidate(seed(shadow_v2_extension_target=target),candle_ms=START-STEP))

    def test_signal_candle_extremes_are_excluded_and_first_touch_bar_has_no_mfe(self):
        r=record();before=copy.deepcopy(r)
        out,stamps=evaluate(r,[candle(START-STEP,low=1,high=1000),candle(START,low=100,high=102)])
        self.assertEqual(out['status'],'OPEN');self.assertEqual(out['filled_ms'],START)
        self.assertEqual(stamps,[START]);self.assertEqual(out['mfe_r'],0);self.assertEqual(out['mae_r'],0)
        self.assertEqual(r,before)

    def test_intrabar_fill_with_single_or_both_exit_touches_is_ambiguous(self):
        for bar in [candle(START,low=89),candle(START,low=100,high=140),candle(START,low=89,high=140)]:
            out,_=evaluate(record(),[bar]);self.assertEqual(out['status'],'AMBIGUOUS')
            self.assertIsNone(out['final_net_r']);self.assertEqual(out['reason'],'EXIT_TOUCH_ON_UNKNOWN_INTRABAR_FILL')

    def test_later_both_touch_is_ambiguous_and_gap_loss_uses_worse_open(self):
        r=record();fill=candle(START,low=100,high=102)
        both,_=evaluate(r,[fill,candle(START+STEP,low=89,high=140)])
        self.assertEqual(both['status'],'AMBIGUOUS');self.assertEqual(both['reason'],'TP_AND_SL_SAME_BAR')
        gap,_=evaluate(r,[fill,candle(START+STEP,open=80,low=79,high=81,close=80)])
        self.assertEqual(gap['status'],'SL');self.assertLess(gap['final_net_r'],-1)

    def test_favorable_open_stays_in_zone_without_future_target_resize(self):
        r=record();out,_=evaluate(r,[candle(START,open=100.5,low=100.1,close=101)])
        self.assertEqual(out['status'],'OPEN');self.assertEqual(out['nominal_fill'],100.5)
        self.assertGreater(out['net_rr'],2);self.assertEqual(out['target'],r['target'])
        outside,_=evaluate(r,[candle(START,open=100,low=99,close=101)])
        self.assertEqual(outside['status'],'REJECTED_AT_FILL');self.assertIsNone(outside['filled_ms'])
        gap,_=evaluate(r,[candle(START,open=89,low=88,close=90)])
        self.assertEqual(gap['status'],'INVALIDATED_GAP')

    def test_closed_confirmation_and_setup_are_checked_before_pending_touch(self):
        for state,status in [(None,'DATA_GAP'),(seed(confirmation_color_ok=False),'INVALIDATED'),
                             (seed(confirmation_level_ok=False),'INVALIDATED'),(seed(stop_valid=False),'INVALIDATED'),
                             (seed(setup_id='other'),'INVALIDATED')]:
            out,_=evaluate(record(),[candle(START,low=100)],{START:state})
            self.assertEqual(out['status'],status);self.assertIsNone(out['filled_ms'])

    def test_expiry_retest_loss_and_gap_do_not_rearm_or_skip_bars(self):
        r=record();miss=[candle(START+i*STEP,open=103,low=102,high=104,close=103) for i in range(4)]
        out,_=evaluate(r,miss+[candle(START+4*STEP,low=100)])
        self.assertEqual(out['status'],'EXPIRED');self.assertIsNone(out['filled_ms'])
        lost,_=evaluate(r,miss[:1]+[candle(START+STEP,low=100)],{START+STEP:seed(retest_touched=False)})
        self.assertEqual(lost['status'],'INVALIDATED')
        gap,_=evaluate(r,miss[:1]+[candle(START+2*STEP,low=100)])
        self.assertEqual(gap['status'],'DATA_GAP')

    def test_resolved_metrics_capped_cash_and_roi_keep_open_exposure_unknown(self):
        r=record();fill=candle(START,low=100)
        win,_=evaluate(r,[fill,candle(START+STEP,open=120,low=119,high=140,close=125)])
        self.assertEqual(win['status'],'TP');self.assertAlmostEqual(win['final_net_r'],2)
        self.assertEqual(win['target'],r['target']);self.assertEqual(win['stop'],r['stop'])
        capital=control.portfolio([win]);self.assertEqual(capital['resolved'],1)
        self.assertGreater(capital['known_cash_usdc'],10000)
        still,_=evaluate(r,[fill]);unknown=control.portfolio([still])
        self.assertIsNone(unknown['closed_portfolio_roi_pct']);self.assertIsNone(unknown['equity_usdc'])

    def test_short_fill_and_target_keep_unchanged_stop_and_confirmed_price_side(self):
        r=record('SHORT');fill=candle(START,open=99,low=98,high=100,close=99)
        out,_=evaluate(r,[fill,candle(START+STEP,open=80,low=60,high=81,close=70)])
        self.assertEqual(out['status'],'TP');self.assertAlmostEqual(out['final_net_r'],2)
        self.assertEqual(out['stop'],110);self.assertEqual(out['roll_level'],100)
        self.assertEqual(out['target'],r['target'])

    def test_kill_gate_is_registered_not_selected_from_profit_subgroups(self):
        def report(n,avg,pf,extra=1):return {'metrics':{zone.MODEL:{'resolved':n,'avg_net_r':avg,'profit_factor':pf}},'comparison':{'net_filled_count_difference':extra}}
        self.assertEqual(zone.decision([report(20,-.1,.8),report(1,2,'INF')]),'KILL_NO_LIVE_PROMOTION')
        self.assertEqual(zone.decision([report(19,-.1,.8),report(19,-1,0)]),'CONTINUE_INSUFFICIENT_SAMPLE')
        self.assertEqual(zone.decision([report(20,1,2),report(20,1,2)]),'FORWARD_SHADOW_REQUIRED')
        self.assertEqual(zone.decision([report(20,1,2,0),report(20,1,2)]),'PIVOT_NO_INCREMENTAL_FILLS')
        protocol=json.loads(zone.PROTOCOL.read_text());self.assertFalse(protocol['automatic_promotion'])
        self.assertEqual(protocol['rules']['pending_bars'],4)

    def test_fill_prices_and_reserved_capital_cannot_use_future_profit(self):
        r=record();before=copy.deepcopy(r)
        fill=candle(START,low=100)
        small,_=evaluate(r,[fill,candle(START+STEP,low=89)])
        big,_=evaluate(r,[fill,candle(START+STEP,high=140,close=125)])
        for k in ['entry','net_risk','trigger','stop','target','created_ms','filled_ms']:
            self.assertEqual(small[k],big[k])
        self.assertEqual(r,before)

    def test_registered_protocol_and_original_controls_remain_separate(self):
        self.assertNotEqual(zone.MODEL,control.MODEL)
        source=Path(zone.__file__).read_text()
        self.assertNotIn('webpush(',source);self.assertNotIn('sqlite3',source)
        protocol=json.loads(zone.PROTOCOL.read_text())
        self.assertEqual(protocol['prospective_start_ms'],1791417600000)
        self.assertEqual(hashlib.sha256(control.PROTOCOL.read_bytes()).hexdigest(),'1ed8af4b9bb0dd7281e80a21956677bb6d9e315f47fb0c00689992662a29ee0f')
