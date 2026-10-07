"""Independent structural entry, no lookahead, conservative fills and kill gate."""
from dataclasses import replace
import copy
import hashlib
import json
import unittest

import app
from analysis_terminal import sweep_reclaim_replay as study,pending_entry_replay as control
from analysis_terminal.test_pending_entry_replay import candle,CONTRACT

STEP,STEP4,START=study.STEP,study.STEP4,800*study.STEP4


def market(side='LONG'):
    sign=1 if side=='LONG' else -1
    monitor=[replace(candle(START-STEP4*(200-i),open=110+sign*i*.05,close=110+sign*i*.05,
                           low=80,high=130),interval='HOUR_4') for i in range(200)]
    entries=[candle(START-STEP*(400-i),open=102 if side=='LONG' else 108,
                    close=102 if side=='LONG' else 108,low=100 if side=='LONG' else 106,
                    high=104 if side=='LONG' else 110) for i in range(400)]
    entries[-15]=replace(entries[-15],high=130 if side=='LONG' else 110,low=100 if side=='LONG' else 80)
    entries[-1]=candle(START-STEP,open=100 if side=='LONG' else 110,close=101 if side=='LONG' else 109,
                       low=99 if side=='LONG' else 108,high=102 if side=='LONG' else 111)
    return monitor,entries


def record(side='LONG'):
    m,e=market(side);r,episode,reason=study.candidate(CONTRACT,m[-180:],e[-180:])
    assert r is not None,(episode,reason)
    return r


def evaluate(r,cs,states=None):
    seen=[]
    def state_at(stamp):
        seen.append(stamp)
        return (states or {}).get(stamp,dict(setup_id=r['setup_id'],confirmation_color_ok=True,
            confirmation_level_ok=True,retest_touched=True,stop_valid=True))
    return study.evaluate(r,cs,state_at,end_ms=max(c.time_ms for c in cs)+STEP),seen


class SweepTests(unittest.TestCase):
    def test_prior_range_structural_wick_stop_and_net2r_both_sides(self):
        for side in ['LONG','SHORT']:
            m,e=market(side);before=copy.deepcopy((m,e));r,episode,reason=study.candidate(CONTRACT,m[-180:],e[-180:])
            self.assertIsNone(reason);self.assertEqual((m,e),before)
            self.assertEqual(r['range_end_ms'],e[-2].time_ms);self.assertEqual(r['range_start_ms'],e[-21].time_ms)
            expected=e[-1].low-.5*app._atr(e[-180:],14) if side=='LONG' else e[-1].high+.5*app._atr(e[-180:],14)
            self.assertAlmostEqual(r['stop'],expected);self.assertEqual(r['created_ms'],START+1)
            self.assertAlmostEqual(control.cost_levels(r['trigger'],r['stop'],r['target'],side)['net_rr'],2)
            self.assertTrue(r['setup_id'].startswith('sweep-reclaim-v1:'))

    def test_equal_edge_touch_or_wrong_candle_color_never_qualifies(self):
        for side in ['LONG','SHORT']:
            m,e=market(side)
            boundary=min(c.low for c in e[-21:-1]) if side=='LONG' else max(c.high for c in e[-21:-1])
            for last in [replace(e[-1],low=boundary) if side=='LONG' else replace(e[-1],high=boundary),
                         replace(e[-1],close=boundary),replace(e[-1],open=e[-1].close)]:
                r,episode,reason=study.candidate(CONTRACT,m[-180:],e[-180:-1]+[last])
                self.assertIsNone(r);self.assertIsNone(episode);self.assertEqual(reason,'SWEEP_WAIT')

    def test_4h_trend_regime_required_and_forming_monitor_bar_rejected(self):
        m,e=market();flat=[replace(c,open=110,close=110) for c in m]
        self.assertEqual(study.candidate(CONTRACT,flat[-180:],e[-180:])[2],'TREND_WAIT')
        with self.assertRaises(ValueError):study.candidate(CONTRACT,m[-179:]+[replace(m[-1],time_ms=START)],e[-180:])

    def test_insufficient_room_is_excluded_not_repaired_by_narrower_stop(self):
        m,e=market();e[-15]=replace(e[-15],high=104)
        r,episode,reason=study.candidate(CONTRACT,m[-180:],e[-180:])
        self.assertIsNone(r);self.assertIsNotNone(episode);self.assertEqual(reason,'NET_ROOM_BELOW_2R')

    def test_signal_candle_does_not_fill_resolve_or_contribute_excursion(self):
        r=record();before=copy.deepcopy(r)
        out,seen=evaluate(r,[candle(START-STEP,low=1,high=1000),candle(START,open=101,low=100.5,high=102)])
        self.assertEqual(out['status'],'OPEN');self.assertEqual(out['filled_ms'],START)
        self.assertEqual(out['mfe_r'],0);self.assertEqual(seen,[START]);self.assertEqual(r,before)

    def test_unknown_intrabar_fill_exit_is_ambiguous_not_a_win_or_loss(self):
        for side in ['LONG','SHORT']:
            r=record(side);out,_=evaluate(r,[candle(START,open=r['trigger'],low=1,high=1000)])
            self.assertEqual(out['status'],'AMBIGUOUS');self.assertIsNone(out['final_net_r'])

    def test_later_both_touch_and_gap_stop_have_conservative_outcomes(self):
        r=record();fill=candle(START,open=101,low=100.5,high=102)
        both,_=evaluate(r,[fill,candle(START+STEP,low=1,high=1000)])
        self.assertEqual(both['status'],'AMBIGUOUS')
        gap,_=evaluate(r,[fill,candle(START+STEP,open=80,low=79,high=81,close=80)])
        self.assertEqual(gap['status'],'SL');self.assertLess(gap['final_net_r'],-1)

    def test_later_tp_has_cost_adjusted_two_r_and_original_prices(self):
        for side in ['LONG','SHORT']:
            r=record(side);fill=candle(START,open=r['trigger'],low=r['trigger']-.1,high=r['trigger']+.1)
            exitbar=candle(START+STEP,open=r['target'],low=r['target']-.1,high=r['target']+.1,close=r['target'])
            out,_=evaluate(r,[fill,exitbar]);self.assertEqual(out['status'],'TP');self.assertAlmostEqual(out['final_net_r'],2)
            self.assertEqual(out['stop'],r['stop']);self.assertEqual(out['target'],r['target'])

    def test_pending_trend_retest_color_level_and_structure_checks(self):
        m,e=market();r=record()
        state=study.pending_state(r,m[-180:],e[-180:]);self.assertEqual(state['setup_id'],r['setup_id'])
        self.assertTrue(all(state[k] for k in ['confirmation_color_ok','confirmation_level_ok','retest_touched','stop_valid']))
        self.assertIsNone(study.pending_state(r,m[-180:],e[-180:],direction='SHORT')['setup_id'])
        for key in ['confirmation_color_ok','confirmation_level_ok','retest_touched','stop_valid']:
            out,_=evaluate(r,[candle(START,low=100.5)],{START:state|{key:False}})
            self.assertEqual(out['status'],'INVALIDATED');self.assertIsNone(out['filled_ms'])

    def test_gap_and_expiry_do_not_skip_to_profitable_future_bar(self):
        r=record();gap,_=evaluate(r,[candle(START+STEP,high=1000)])
        self.assertEqual(gap['status'],'DATA_GAP');self.assertIsNone(gap['filled_ms'])
        cs=[candle(START+i*STEP,open=103,low=102,high=104,close=103) for i in range(4)]
        out,_=evaluate(r,cs+[candle(START+4*STEP,low=100)])
        self.assertEqual(out['status'],'EXPIRED');self.assertIsNone(out['filled_ms'])

    def test_report_boundary_and_warmup_never_create_an_earlier_entry(self):
        m,e=market();e=e+[candle(START+i*STEP,open=101,close=102,low=100.5,high=103) for i in range(4)]
        out=study.replay_market(CONTRACT,m,e,start_ms=START,end_ms=START+4*STEP)
        self.assertTrue(out['records']);self.assertTrue(all(r['created_ms']>=START+1 for r in out['records']))
        after=study.replay_market(CONTRACT,m,e,start_ms=START+STEP,end_ms=START+4*STEP)
        self.assertFalse(after['records']);self.assertGreater(after['exclusions'].get('WARMUP_FIRST_EPISODE',0),0)

    def test_invalid_or_duplicated_candle_data_is_rejected(self):
        m,e=market()
        for changed in [e+[e[-1]],e[:-1]+[replace(e[-1],contract_id='bad')],e[:-1]+[replace(e[-1],low=200)]]:
            with self.assertRaises(ValueError):study.replay_market(CONTRACT,m,changed,start_ms=START,end_ms=START+STEP)

    def test_cost_r_and_open_portfolio_do_not_claim_full_roi(self):
        r=record();opened,_=evaluate(r,[candle(START,open=101,low=100.5,high=102)])
        p=control.portfolio([opened]);self.assertIsNone(p['closed_portfolio_roi_pct']);self.assertIsNone(p['equity_usdc'])
        self.assertGreater(opened['net_risk'],abs(opened['entry']-opened['stop']))

    def test_development_gate_stops_before_unused_validation_and_zero_noise_cannot_pass(self):
        def report(n,avg,pf,extra=1):return {'metrics':{study.MODEL:{'resolved':n,'avg_net_r':avg,'profit_factor':pf}},'comparison':{'net_filled_count_difference':extra}}
        self.assertEqual(study.decision([report(20,-.1,.9)]),'KILL_NO_LIVE_PROMOTION')
        self.assertEqual(study.decision([report(20,1e-15,1+1e-15)]),'KILL_NO_LIVE_PROMOTION')
        self.assertEqual(study.decision([report(19,-1,0)]),'CONTINUE_INSUFFICIENT_SAMPLE')
        self.assertEqual(study.decision([report(20,1,2)]),'HISTORICAL_VALIDATION_REQUIRED')
        self.assertEqual(study.decision([report(20,1,2),report(20,1,2)]),'FORWARD_SHADOW_REQUIRED')
        self.assertEqual(study.decision([report(20,1,2,0),report(20,1,2)]),'PIVOT_NO_INCREMENTAL_FILLS')

    def test_original_indicators_and_execution_dependencies_stay_frozen(self):
        p=json.loads(study.PROTOCOL.read_text())
        for name,digest in p['frozen_dependencies_sha256'].items():
            path=study.PROTOCOL.parent.parent/name if name=='app.py' else study.PROTOCOL.with_name(name)
            raw=path.read_bytes()
            study.verify_frozen_dependency(name,raw,digest)
            with self.assertRaisesRegex(ValueError,'Frozen control changed'):
                study.verify_frozen_dependency(name,raw+b'# changed',digest)
            if name=='app.py':
                study.verify_frozen_dependency(name,raw.rstrip(b'\n')+b'\n',digest)
                with self.assertRaises(ValueError):
                    study.verify_frozen_dependency(name,raw.rstrip(b'\n')+b'\n\n\n',digest)
        self.assertFalse(p['eligible_for_live_promotion']);self.assertFalse(p['changes_live_rules'])
        self.assertTrue(p['validation']['run_only_if_development_not_killed'])
        with self.assertRaisesRegex(ValueError,'registered frozen comparator'):
            study.build(study.PROTOCOL.parent,None,None,role='development')


if __name__=='__main__':unittest.main()
