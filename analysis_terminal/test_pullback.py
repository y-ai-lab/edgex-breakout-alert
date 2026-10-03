from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from analysis_terminal import pullback_replay as pullback
from analysis_terminal import test_replay as fixture

server=fixture.server
STEP,MONITOR,START=fixture.STEP,fixture.MONITOR,fixture.START
CONTRACT=fixture.CONTRACT
SETTINGS=replace(server.SETTINGS,trend_fast_ema=2,trend_slow_ema=3,atr_period=2,roll_lookback=2)


def anchor(time=START-MONITOR, direction='LONG',level=99.5):
    return dict(time_ms=time,level=level,direction=direction,raw_extension=140 if direction=='LONG' else 60)


def replay(*,end=START+4*STEP,rows=None,anchor_time=START-MONITOR):
    m,e=fixture.dataset(end)
    e=[replace(c,open=99) for c in e] if rows is None else rows
    with patch.object(pullback,'trend',return_value=('LONG',99.5)),patch.object(pullback,'discover_anchor',side_effect=lambda m,s:anchor(anchor_time) if m[-1].time_ms==anchor_time else None):
        return pullback.replay_pullback(CONTRACT,m,e,SETTINGS,start_ms=START,end_ms=end,window=4)


class PullbackEntryTests(unittest.TestCase):
    def test_identity_has_separate_namespace_and_normalized_frozen_level(self):
        x=anchor(level=100)
        y=anchor(level=100.0)
        self.assertEqual(pullback.pullback_identity('TESTUSDC','LONG',x),pullback.pullback_identity('TESTUSDC','LONG',y))
        self.assertTrue(pullback.pullback_identity('TESTUSDC','LONG',x).startswith('pullback-v1:'))

    def test_discovery_requires_trend_previous_position_and_actual_ema_touch(self):
        m=[fixture.c(START-(8-i)*MONITOR,close=100+i,open=100+i,high=110+i,low=90) for i in range(8)]
        a=pullback.discover_anchor(m,SETTINGS)
        self.assertIsNotNone(a)
        self.assertEqual(a['raw_extension'],max(c.high for c in m[-3:-1]))
        m[-1]=replace(m[-1],low=106.9)
        self.assertIsNone(pullback.discover_anchor(m,SETTINGS))
        self.assertIsNone(pullback.discover_anchor([replace(c,close=100,open=100) for c in m],SETTINGS))

    def test_post_anchor_retest_and_both_confirmation_conditions_are_required(self):
        m,e=fixture.dataset();m=[c for c in m if c.time_ms+MONITOR<=START+STEP][-4:];e=[replace(c,open=99) for c in e if c.time_ms<START+STEP][-4:]
        def entry(rows):
            with patch.object(pullback,'trend',return_value=('LONG',99.5)):
                return pullback.entry_for_anchor(CONTRACT,m,rows,anchor(),SETTINGS)
        self.assertTrue(entry(e)['ready'])
        self.assertFalse(entry(e[:-1]+[replace(e[-1],open=101)])['ready'])
        self.assertFalse(entry(e[:-1]+[replace(e[-1],close=99,open=98)])['ready'])
        distant=[replace(c,open=109,close=110,high=111,low=108) for c in e]
        self.assertFalse(entry(distant)['retest'])
        before=[replace(c,time_ms=c.time_ms-4*STEP) for c in e]
        self.assertEqual(entry(before)['stage'],'POST_ANCHOR_WAIT')

    def test_structural_stop_and_room_are_not_relaxed(self):
        m,e=fixture.dataset();m=[c for c in m if c.time_ms+MONITOR<=START+STEP][-4:];e=[replace(c,open=99) for c in e if c.time_ms<START+STEP][-4:]
        with patch.object(pullback,'trend',return_value=('LONG',99.5)),patch.object(pullback.scanner,'_atr',return_value=2):
            r=pullback.entry_for_anchor(CONTRACT,m,e,anchor(),SETTINGS)
            self.assertEqual(r['stop'],min(99.5,min(c.low for c in m if c.time_ms>=START-MONITOR))-1)
            self.assertEqual(r['target'],r['entry']+2*(r['entry']-r['stop']))
            tight=anchor();tight['raw_extension']=r['entry']+(r['entry']-r['stop'])*1.99+.5
            self.assertFalse(pullback.entry_for_anchor(CONTRACT,m,e,tight,SETTINGS)['ready'])
        with patch.object(pullback,'trend',return_value=('SHORT',99.5)):
            self.assertEqual(pullback.entry_for_anchor(CONTRACT,m,e,anchor(),SETTINGS)['stage'],'TREND_WAIT')

    def test_short_has_inverse_confirmation_stop_and_target(self):
        m,e=fixture.dataset();m=[c for c in m if c.time_ms+MONITOR<=START+STEP][-4:];e=[replace(c,open=101) for c in e if c.time_ms<START+STEP][-4:]
        with patch.object(pullback,'trend',return_value=('SHORT',100.5)),patch.object(pullback.scanner,'_atr',return_value=2):
            r=pullback.entry_for_anchor(CONTRACT,m,e,anchor(direction='SHORT',level=100.5),SETTINGS)
        self.assertTrue(r['ready']);self.assertGreater(r['stop'],r['entry'])
        self.assertEqual(r['target'],r['entry']-2*(r['stop']-r['entry']))

    def test_anchor_reference_is_frozen_when_current_ema_moves(self):
        m,e=fixture.dataset();m=[c for c in m if c.time_ms+MONITOR<=START+STEP][-4:];e=[replace(c,open=99) for c in e if c.time_ms<START+STEP][-4:]
        frozen=anchor()
        with patch.object(pullback,'trend',return_value=('LONG',105)):
            r=pullback.entry_for_anchor(CONTRACT,m,e,frozen,SETTINGS)
        self.assertTrue(r['ready'])
        self.assertEqual(r['anchor_level'],99.5)
        self.assertEqual(r['setup_id'],pullback.pullback_identity('TESTUSDC','LONG',frozen))
        self.assertEqual(frozen,anchor())

    def test_old_setup_expires_at_six_four_hour_bars(self):
        m,e=fixture.dataset();m=[c for c in m if c.time_ms+MONITOR<=START+STEP][-4:];e=[replace(c,open=99) for c in e if c.time_ms<START+STEP][-4:]
        with patch.object(pullback,'trend',return_value=('LONG',99.5)):
            r=pullback.entry_for_anchor(CONTRACT,m,e,anchor(time=m[-1].time_ms-6*MONITOR),SETTINGS)
        self.assertEqual(r['stage'],'EXPIRED')
        self.assertFalse(r['ready'])


class PullbackReplayTests(unittest.TestCase):
    def test_first_entry_is_after_anchor_close_and_signal_candle_excluded(self):
        m,e=fixture.dataset()
        e=[replace(c,open=99,high=150,low=50) if c.time_ms==START else replace(c,open=99,high=130) if c.time_ms==START+STEP else replace(c,open=99) for c in e]
        r=replay(rows=e);self.assertEqual(len(r['signals']),1)
        s=r['signals'][0]
        self.assertEqual((s['signal_candle_ms'],s['created_ms']),(START,START+STEP+1))
        self.assertEqual(s['result']['status'],'TP')
        self.assertEqual(s['result']['outcome_time_ms'],START+STEP)
        self.assertEqual(s['result']['candles_checked'],1)

    def test_same_bar_both_touches_are_ambiguous(self):
        m,e=fixture.dataset()
        e=[replace(c,open=99,high=150,low=50) if c.time_ms==START+STEP else replace(c,open=99) for c in e]
        result=replay(rows=e)['signals'][0]['result']
        self.assertEqual(result['status'],'AMBIGUOUS');self.assertIsNone(result['final_r'])

    def test_gap_holds_result_instead_of_resolving_on_later_tp(self):
        m,e=fixture.dataset()
        e=[replace(c,open=99,high=130) if c.time_ms==START+2*STEP else replace(c,open=99) for c in e if c.time_ms!=START+STEP]
        s=replay(rows=e)['signals'][0]
        self.assertEqual(s['result']['status'],'OPEN')
        self.assertEqual(s['outcome_gap_ms'],START+STEP)

    def test_warmup_entry_does_not_reappear_as_new_reporting_entry(self):
        r=replay(anchor_time=START-2*MONITOR)
        self.assertEqual(r['signals'],[]);self.assertEqual(r['warmup_first_entries'],1)

    def test_first_entry_unknown_after_indicator_gap_is_excluded(self):
        m,e=fixture.dataset()
        e=[replace(c,open=99) for c in e if c.time_ms!=START-2*STEP]
        r=replay(rows=e,end=START+6*STEP)
        self.assertEqual(r['signals'],[])
        self.assertGreater(r['excluded_points'].get('unknown_first_entry',0),0)

    def test_future_prices_do_not_change_first_entry(self):
        m,e=fixture.dataset()
        original=replay()
        e=[replace(c,open=99,high=1000,low=1) if c.time_ms>=START+4*STEP else replace(c,open=99) for c in e]
        self.assertEqual(original['signals'],replay(rows=e)['signals'])

    def test_protocol_decision_does_not_promote_or_tune(self):
        def period(n,r,pf,extra=1):return dict(metrics=dict(pullback=dict(resolved=n,avg_r=r,profit_factor=pf)),opportunities=dict(additional_vs_current=extra))
        self.assertEqual(pullback.decision([period(1,-1,0),period(19,1,2)]),'CONTINUE_INSUFFICIENT_SAMPLE')
        self.assertEqual(pullback.decision([period(20,-.1,.9),period(1,1,2)]),'KILL')
        self.assertEqual(pullback.decision([period(20,.1,1.1),period(20,.2,'INF')]),'CONTINUE_FORWARD_SHADOW_REQUIRED')
        self.assertEqual(pullback.decision([period(20,.1,1.1,0),period(20,.2,2)]),'PIVOT_NO_INCREMENTAL_OPPORTUNITIES')
