"""Causal continuation setups, frozen local structure and cost-stress gates."""
from dataclasses import replace
import copy
import hashlib
import json
import unittest

import app
from analysis_terminal import break_retest_replay as study,pending_entry_replay as control
from analysis_terminal.test_pending_entry_replay import candle,CONTRACT

STEP,STEP4,START=study.STEP,study.STEP4,800*study.STEP4


def market(side='LONG'):
    sign=1 if side=='LONG' else -1
    monitor=[replace(candle(START-STEP4*(200-i),open=110+sign*i*.05,close=110+sign*i*.05,low=80,high=130),interval='HOUR_4') for i in range(200)]
    entries=[candle(START-STEP*(400-i),open=104.5,close=104.5,low=104,high=105) for i in range(400)]
    entries[-15]=replace(entries[-15],low=95)
    entries[-1]=candle(START-STEP,open=104.5,close=106,low=104.2,high=107)
    ret=candle(START,open=105.5,close=106,low=104.8,high=106.5)
    if side=='SHORT':
        entries=[replace(c,open=210-c.open,close=210-c.close,low=210-c.high,high=210-c.low) for c in entries]
        ret=replace(ret,open=210-ret.open,close=210-ret.close,low=210-ret.high,high=210-ret.low)
    return monitor,entries,ret


def setup_and_record(side='LONG'):
    m,e,c=market(side);s,episode=study.breakout(CONTRACT,m[-180:],e[-180:])
    active,r,reason=study.advance(s,m[-180:],(e+[c])[-180:]);assert r is not None,(reason,s)
    r.update(step_size=CONTRACT.step_size,min_order_size=CONTRACT.min_order_size,max_order_size=CONTRACT.max_order_size)
    return s,r


def evaluate(r,cs,*,states=None):
    def state_at(stamp):
        return (states or {}).get(stamp,dict(setup_id=r['setup_id'],confirmation_color_ok=True,confirmation_level_ok=True,retest_touched=True,stop_valid=True))
    return study.evaluate(r,cs,state_at,end_ms=max(c.time_ms for c in cs)+STEP)


class BreakRetestTests(unittest.TestCase):
    def test_prebreakout_range_and_measured_extension_exclude_breakout_both_sides(self):
        for side in ('LONG','SHORT'):
            m,e,c=market(side);s,ep=study.breakout(CONTRACT,m[-180:],e[-180:])
            prior=e[-21:-1];self.assertEqual(s['range_end_ms'],e[-2].time_ms)
            self.assertEqual(s['range_high'],max(b.high for b in prior));self.assertEqual(s['range_low'],min(b.low for b in prior))
            width=s['range_high']-s['range_low'];atr=app._atr(e[-180:],14)
            self.assertAlmostEqual(s['extension_target'],s['range_high']+width-.25*atr if side=='LONG' else s['range_low']-width+.25*atr)

    def test_breakout_cannot_retest_or_confirm_itself_and_future_monitor_rejected(self):
        m,e,c=market();s,ep=study.breakout(CONTRACT,m[-180:],e[-180:])
        with self.assertRaises(ValueError):study.advance(s,m[-180:],e[-180:])
        with self.assertRaises(ValueError):study.breakout(CONTRACT,m[-179:]+[replace(m[-1],time_ms=START)],e[-180:])

    def test_wick_retest_freezes_actual_extreme_atr_and_net2r_both_sides(self):
        for side in ('LONG','SHORT'):
            m,e,c=market(side);s,r=setup_and_record(side);atr=app._atr((e+[c])[-180:],14)
            self.assertAlmostEqual(r['stop'],c.low-.5*atr if side=='LONG' else c.high+.5*atr)
            self.assertEqual(r['created_ms'],START+STEP+1);self.assertEqual(r['extension_target'],s['extension_target'])
            self.assertAlmostEqual(control.cost_levels(r['trigger'],r['stop'],r['target'],side)['net_rr'],2)

    def test_retest_and_confirmation_can_use_different_closed_bars_without_future_stop(self):
        m,e,c=market();s,ep=study.breakout(CONTRACT,m[-180:],e[-180:]);before=copy.deepcopy(s)
        red=replace(c,open=106.2,close=105.8)
        waiting,r,reason=study.advance(s,m[-180:],(e+[red])[-180:]);self.assertIsNone(r);self.assertTrue(waiting['touched']);self.assertEqual(s,before)
        green=candle(START+STEP,open=105.8,close=106.1,low=105.5,high=106.3)
        _,r,reason=study.advance(waiting,m[-180:],(e+[red,green])[-180:])
        self.assertAlmostEqual(r['extreme'],red.low);self.assertEqual(r['signal_candle_ms'],green.time_ms)

    def test_lost_edge_equal_close_or_trend_invalidates_before_confirmation(self):
        m,e,c=market();s,ep=study.breakout(CONTRACT,m[-180:],e[-180:])
        for last,side in [(replace(c,close=105),None),(replace(c,close=104.9),None),(c,'SHORT')]:
            active,r,reason=study.advance(s,m[-180:],(e+[last])[-180:],direction=side)
            self.assertIsNone(active);self.assertIsNone(r);self.assertIn(reason,('CLOSE_LOST_BREAKOUT_EDGE','TREND_INVALIDATED'))

    def test_no_wick_retest_no_confirmation_and_six_bar_expiry(self):
        m,e,c=market();s,ep=study.breakout(CONTRACT,m[-180:],e[-180:]);after=[]
        for i in range(6):
            after.append(candle(START+i*STEP,open=106.2,close=106.4,low=105.2,high=106.5))
            s,r,reason=study.advance(s,m[-180:],(e+after)[-180:]);self.assertIsNone(r)
        self.assertIsNone(s);self.assertEqual(reason,'SETUP_EXPIRED')

    def test_first_confirmed_room_failure_does_not_narrow_stop(self):
        m,e,c=market();s,ep=study.breakout(CONTRACT,m[-180:],e[-180:]);before=copy.deepcopy(s)
        active,r,reason=study.advance(s,m[-180:],(e+[replace(c,low=99)])[-180:])
        self.assertIsNone(active);self.assertIsNone(r);self.assertEqual(reason,'CONFIRMED_NET_ROOM_BELOW_2R');self.assertEqual(s,before)

    def test_breakout_and_confirmation_candles_do_not_fill_resolve_or_add_excursions(self):
        s,r=setup_and_record();cs=[candle(START-STEP,low=1,high=1000),candle(START,low=1,high=1000),candle(START+STEP,open=106,low=105.9,high=106.1,close=106)]
        out=evaluate(r,cs);self.assertEqual(out['status'],'OPEN');self.assertEqual(out['filled_ms'],START+STEP);self.assertEqual(out['mfe_r'],0)

    def test_intrabar_fill_exit_and_later_both_are_ambiguous_both_sides(self):
        for side in ('LONG','SHORT'):
            s,r=setup_and_record(side)
            self.assertEqual(evaluate(r,[candle(START+STEP,open=r['trigger'],low=1,high=1000)])['status'],'AMBIGUOUS')
            fill=candle(START+STEP,open=r['trigger'],low=r['trigger']-.1,high=r['trigger']+.1,close=r['trigger'])
            self.assertEqual(evaluate(r,[fill,candle(START+2*STEP,low=1,high=1000)])['status'],'AMBIGUOUS')

    def test_missing_first_fill_bar_blocks_later_profit_and_open_capital_roi_unknown(self):
        s,r=setup_and_record();out=evaluate(r,[candle(START+2*STEP,open=106,low=105.9,high=1000)])
        self.assertEqual(out['status'],'DATA_GAP');self.assertIsNone(out['filled_ms'])
        opened=evaluate(r,[candle(START+STEP,open=106,low=105.9,high=106.1)])
        self.assertIsNone(control.portfolio([opened])['closed_portfolio_roi_pct'])

    def test_stress_reprices_same_tp_outcomes_with_higher_cost_both_sides(self):
        for side in ('LONG','SHORT'):
            s,r=setup_and_record(side);before=copy.deepcopy(r)
            fill=candle(START+STEP,open=r['trigger'],low=r['trigger']-.1,high=r['trigger']+.1)
            exitbar=candle(START+2*STEP,open=r['target'],low=r['target']-.1,high=r['target']+.1,close=r['target'])
            out=evaluate(r,[fill,exitbar]);stressed=study.stressed_metrics([out])
            self.assertEqual(out['status'],'TP');self.assertAlmostEqual(out['final_net_r'],2)
            self.assertLess(stressed['avg_net_r'],2);self.assertEqual(stressed['resolved'],1);self.assertEqual(r,before)

    def test_adverse_gap_loss_preserves_prices_and_stressed_r_basis(self):
        s,r=setup_and_record();fill=candle(START+STEP,open=106,low=105.9,high=106.1)
        out=evaluate(r,[fill,candle(START+2*STEP,open=90,low=89,high=91,close=90)])
        self.assertEqual(out['status'],'SL');self.assertLess(out['final_net_r'],-1);self.assertEqual(out['stop'],r['stop'])
        self.assertLess(study.stressed_metrics([out])['avg_net_r'],-1)

    def test_replay_excludes_preperiod_breakouts_and_never_duplicates_setup(self):
        m,e,c=market();cs=e+[c]+[candle(START+i*STEP,open=106,close=106.2,low=105.9,high=106.4) for i in range(1,7)]
        a=study.replay_market(CONTRACT,m,cs,start_ms=START,end_ms=START+7*STEP)
        self.assertEqual(len(a['records']),1);self.assertEqual(len({r['setup_id'] for r in a['records']}),len(a['records']))
        b=study.replay_market(CONTRACT,m,cs,start_ms=START+STEP,end_ms=START+7*STEP)
        self.assertFalse(b['records']);self.assertGreater(b['exclusions'].get('WARMUP_BREAKOUT_CONFIRMATION',0),0)

    def test_duplicate_bad_identity_and_ohlc_are_rejected(self):
        m,e,c=market()
        for cs in [e+[e[-1]],e[:-1]+[replace(e[-1],contract_id='bad')],e[:-1]+[replace(e[-1],low=200)]]:
            with self.assertRaises(ValueError):study.replay_market(CONTRACT,m,cs,start_ms=START,end_ms=START+STEP)

    def test_practical_gate_needs_margin_two_periods_and_known_capital_roi(self):
        def report(n=50,avg=.3,pf=1.5,win=45,stress=.2,roi=2,extra=1):
            return dict(metrics={study.MODEL:dict(resolved=n,avg_net_r=avg,profit_factor=pf,win_rate=win)},stress_metrics={study.MODEL:dict(avg_net_r=stress,profit_factor=1.3)},comparison=dict(net_filled_count_difference=extra),portfolios={study.MODEL:dict(resolved=20,closed_portfolio_roi_pct=roi)})
        self.assertEqual(study.decision([report(n=20,avg=-.1,pf=.9)]),'KILL_NO_LIVE_PROMOTION')
        self.assertEqual(study.decision([report(n=20,avg=1e-15,pf=1+1e-15)]),'KILL_NO_LIVE_PROMOTION')
        self.assertEqual(study.decision([report(n=49)]),'CONTINUE_INSUFFICIENT_SAMPLE')
        self.assertEqual(study.decision([report()]),'HISTORICAL_VALIDATION_REQUIRED')
        self.assertEqual(study.decision([report(),report(stress=-.1)]),'PIVOT_NO_PRACTICAL_MARGIN')
        self.assertEqual(study.decision([report(),report(roi=None)]),'CONTINUE_ROI_UNVERIFIED')
        self.assertEqual(study.decision([report(),report()]),'LIVE_CAPTURE_SHADOW_REQUIRED')

    def test_frozen_dependencies_and_required_development_baseline(self):
        p=json.loads(study.PROTOCOL.read_text())
        for name,digest in p['frozen_dependencies_sha256'].items():
            self.assertEqual(hashlib.sha256(study.PROTOCOL.with_name(name).read_bytes()).hexdigest(),digest)
        with self.assertRaises(ValueError):study.build(study.PROTOCOL.parent,None,None,role='development')
        self.assertFalse(p['automatic_promotion']);self.assertFalse(p['changes_live_rules'])


if __name__=='__main__':unittest.main()
