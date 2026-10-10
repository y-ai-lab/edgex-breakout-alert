"""Causal inside-release/expiry/quality/capital tests, no network or live writes."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import swing_neck_replay as study
from analysis_terminal import server
from analysis_terminal.test_pending_entry_replay import CONTRACT, candle
from analysis_terminal.test_vwap_reclaim_replay import market as vwap_market, ORIGIN, SIGNAL, STEP


def market(side='LONG'):
    monitor, entries = vwap_market('LONG')
    for i in range(len(entries)-25,len(entries)):
        entries[i]=replace(entries[i],open=100,close=100,low=99.8,high=100.2)
    entries[-12]=replace(entries[-12],low=99.0)
    entries[-9]=replace(entries[-9],high=100.5)
    entries[-6]=replace(entries[-6],low=99.3)
    entries[-1]=replace(entries[-1],open=100.45,close=100.6,low=100.45,high=100.7)
    if side=='SHORT':
        def mirror(c):return replace(c,open=210-c.open,close=210-c.close,high=210-c.low,low=210-c.high)
        monitor,entries=[mirror(c) for c in monitor],[mirror(c) for c in entries]
    return monitor,entries

def record(side='LONG'):
    m,e=market(side);r,reason=study.candidate(CONTRACT,m[-180:],e[-180:])
    assert r,reason
    return r


def state(r):
    return dict(setup_id=r['setup_id'],confirmation_color_ok=True,confirmation_level_ok=True,retest_touched=True,stop_valid=True)


def fill(r,offset=0):
    p=r['trigger'];return candle(SIGNAL+(1+offset)*STEP,open=p,close=p,low=p-.01,high=p+.01)


def evaluate(r,cs,state_at=None):
    return study.evaluate(r,cs,state_at or (lambda _:state(r)),end_ms=max(c.time_ms for c in cs)+STEP)


def positive(role='historical_replication_1', resolved=50):
    p = study.periods(study.protocol())[role]
    metric = dict(resolved=resolved, avg_net_r=.5, profit_factor=2., win_rate=50.)
    return dict(role=role, start_ms=p['start_ms'], end_ms=p['end_ms'],
                protocol_sha256=study.PROTOCOL_SHA256, period_complete=True,
                coverage=dict(valid_points=100, failed_markets=0), metrics={study.MODEL:metric},
                stress_metrics={study.MODEL:deepcopy(metric)},
                portfolios={study.MODEL:dict(active=0, uncertain=0, resolved=20, closed_portfolio_roi_pct=1.)},
                comparison=dict(net_filled_count_difference=10, capped_filled_count_difference=1))


class SwingTests(unittest.TestCase):
    def test_confirmed_improving_pivots_both_sides_net_2r_and_clock(self):
        for side in ('LONG','SHORT'):
            m,e=market(side);r=record(side)
            self.assertEqual((r['first_pivot_ms'],r['second_pivot_ms']),(SIGNAL-11*STEP,SIGNAL-5*STEP))
            self.assertEqual(r['pivot_confirmed_close_ms'],SIGNAL-2*STEP)
            self.assertEqual(r['roll_level'],100.5 if side=='LONG' else 109.5)
            self.assertEqual((r['created_ms'],r['expires_ms']),(SIGNAL+STEP+1,SIGNAL+5*STEP))
            self.assertAlmostEqual(study.control.cost_levels(r['trigger'],r['stop'],r['target'],side)['net_rr'],2)
            av=study.scanner._atr(e[-180:],14)
            self.assertEqual(r['stop'],99.3-.5*av if side=='LONG' else 110.7+.5*av)

    def test_right_confirmation_must_not_use_signal_or_future(self):
        m,e=market()
        for c in range(-25,-1):e[c]=replace(e[c],low=99.8)
        e[-12]=replace(e[-12],low=99.0);e[-3]=replace(e[-3],low=99.3)
        # Pivot at -3 needs signal (-1) as second right bar: not confirmed.
        self.assertIsNone(study.candidate(CONTRACT,m[-180:],e[-180:])[0])
        self.assertFalse(study.crossings(CONTRACT,e[-180:]))

    def test_ties_and_latest_unfavorable_pivot_cannot_be_skipped(self):
        m,e=market();e[-5]=replace(e[-5],low=99.3)
        self.assertIsNone(study.candidate(CONTRACT,m[-180:],e[-180:])[0])
        m,e=market();e[-20]=replace(e[-20],low=98.9);e[-6]=replace(e[-6],low=98.95)
        self.assertIsNone(study.candidate(CONTRACT,m[-180:],e[-180:])[0])

    def test_pivot_separation_and_lookback_are_fixed(self):
        m,e=market();e[-6]=replace(e[-6],low=99.8);e[-8]=replace(e[-8],low=99.3)
        self.assertIsNone(study.candidate(CONTRACT,m[-180:],e[-180:])[0])
        m,e=market();e[-12]=replace(e[-12],low=99.8);e[-22]=replace(e[-22],low=99)
        self.assertIsNone(study.candidate(CONTRACT,m[-180:],e[-180:])[0])

    def test_pair_already_broken_at_discovery_is_never_rearmed(self):
        m,e=market();e[-4]=replace(e[-4],open=100.55,close=100.6,high=100.7)
        self.assertIsNone(study.candidate(CONTRACT,m[-180:],e[-180:])[0])
        e[-2]=replace(e[-2],close=100)
        self.assertIsNone(study.candidate(CONTRACT,m[-180:],e[-180:])[0])

    def test_first_failed_color_or_trend_cross_consumes_pair(self):
        for failure in ('color','trend'):
            m,e=market()
            if failure=='color':e[-1]=replace(e[-1],open=100.65)
            else:m=[replace(c,open=100,high=101,low=99,close=100) for c in m]
            e.append(candle(SIGNAL+STEP,open=100,close=100,high=100.2,low=99.8))
            e.append(candle(SIGNAL+2*STEP,open=100.45,close=100.6,high=100.7,low=100.45))
            r=study.replay_market(CONTRACT,m,e,start_ms=SIGNAL+STEP,end_ms=SIGNAL+4*STEP)
            self.assertFalse(r['records']);self.assertGreaterEqual(r['exclusions'].get('ALREADY_CONSUMED_PAIR',0),1)

    def test_failed_room_first_cross_is_consumed_and_not_repriced(self):
        m,e=market();e.append(candle(SIGNAL+STEP,open=100,close=100,high=100.2,low=99.8))
        e.append(candle(SIGNAL+2*STEP,open=100.45,close=100.6,high=100.7,low=100.45))
        with patch.object(study,'price_candidate',return_value=(None,'NET_ROOM_BELOW_2R')) as pricing:
            r=study.replay_market(CONTRACT,m,e,start_ms=SIGNAL+STEP,end_ms=SIGNAL+4*STEP)
            self.assertFalse(r['records']);self.assertEqual(pricing.call_count,1)
            self.assertEqual(r['exclusions']['NET_ROOM_BELOW_2R'],1)
            self.assertEqual(r['exclusions']['ALREADY_CONSUMED_PAIR'],1)

    def test_preperiod_cross_identity_only_and_gap_origin_not_reconstructed(self):
        m,e=market();e.append(candle(SIGNAL+STEP,open=100,close=100,high=100.2,low=99.8))
        e.append(candle(SIGNAL+2*STEP,open=100.45,close=100.6,high=100.7,low=100.45))
        with patch.object(study,'price_candidate',wraps=study.price_candidate) as pricing:
            r=study.replay_market(CONTRACT,m,e,start_ms=SIGNAL+2*STEP,end_ms=SIGNAL+4*STEP)
            self.assertFalse(r['records']);pricing.assert_not_called()
        m,e=market();e=[c for c in e if c.time_ms!=SIGNAL-STEP]
        r=study.replay_market(CONTRACT,m,e,start_ms=SIGNAL+STEP,end_ms=SIGNAL+2*STEP)
        self.assertFalse(r['records']);self.assertEqual(r['valid_points'],0)
        m,e=market()
        with patch.object(study,'complete_window',side_effect=lambda cs,idx,n,step,now:
                None if now==SIGNAL-STEP+1 else original(cs,idx,n,step,now)):
            r=study.replay_market(CONTRACT,m,e,start_ms=SIGNAL+STEP,end_ms=SIGNAL+2*STEP)
            self.assertFalse(r['records']);self.assertEqual(r['exclusions']['UNKNOWN_PAIR_ORIGIN'],1)

    def test_strict_color_level_touch_and_closed_trend_required(self):
        for side in ('LONG','SHORT'):
            m,e=market(side);c=e[-1];level=100.5 if side=='LONG' else 109.5
            for x in (replace(c,open=c.close),replace(c,close=level),
                      replace(c,low=level+.01) if side=='LONG' else replace(c,high=level-.01)):
                self.assertIsNone(study.candidate(CONTRACT,m[-180:],(e[:-1]+[x])[-180:])[0])
            self.assertIsNone(study.candidate(CONTRACT,m[-180:],e[-180:],direction='SHORT' if side=='LONG' else 'LONG')[0])
            with self.assertRaises(ValueError):study.candidate(CONTRACT,m[-179:],e[-180:])

    def test_signal_high_cannot_create_target_room(self):
        m,e=market();e=[replace(c,high=100.5) if c.time_ms<SIGNAL and c.high>100.5 else c for c in e]
        e[-1]=replace(e[-1],high=1000)
        r,reason=study.candidate(CONTRACT,m[-180:],e[-180:])
        self.assertIsNone(r);self.assertEqual(reason,'NET_ROOM_BELOW_2R')

    def test_signal_extremes_excluded_and_later_net_tp_both_sides(self):
        for side in ('LONG','SHORT'):
            r=record(side);f=fill(r)
            result=evaluate(r,[candle(SIGNAL,high=1000,low=1),f]);self.assertEqual(result['status'],'OPEN')
            tp=replace(f,time_ms=f.time_ms+STEP,high=r['target']+.01) if side=='LONG' else replace(f,time_ms=f.time_ms+STEP,low=r['target']-.01)
            result=evaluate(r,[f,tp]);self.assertEqual(result['status'],'TP');self.assertAlmostEqual(result['final_net_r'],2)

    def test_unknown_exit_order_and_missing_candle_or_state_preserve_uncertainty(self):
        for side in ('LONG','SHORT'):
            r=record(side);f=fill(r);both=replace(f,high=max(r['stop'],r['target'])+1,low=min(r['stop'],r['target'])-1)
            for cs in ([both],[f,replace(both,time_ms=f.time_ms+STEP)]):
                result=evaluate(r,cs);self.assertEqual(result['status'],'AMBIGUOUS')
                self.assertIsNone(study.control.portfolio([result])['closed_portfolio_roi_pct'])
            result=evaluate(r,[f,fill(r,2)]);self.assertEqual(result['status'],'DATA_GAP');self.assertIsNone(result['final_net_r'])
            self.assertEqual(evaluate(r,[f],lambda _:None)['status'],'DATA_GAP')

    def test_four_bar_expiry_and_prior_strict_state_no_rearm(self):
        r=record();cs=[replace(fill(r,i),open=r['trigger']+.1,close=r['trigger']+.1,low=r['trigger']+.05,high=r['trigger']+.2) for i in range(4)]
        result=evaluate(r,cs+[fill(r,4)]);self.assertEqual(result['status'],'EXPIRED');self.assertIsNone(result['filled_ms'])
        for field in ('confirmation_color_ok','confirmation_level_ok','retest_touched','stop_valid'):
            self.assertEqual(evaluate(r,[fill(r)],lambda _,field=field:state(r)|{field:False})['status'],'INVALIDATED')


original = study.complete_window


class DecisionTests(unittest.TestCase):
    def test_twenty_negative_kills_fixed_specification(self):
        r=positive('development',20);r['metrics'][study.MODEL].update(avg_net_r=1e-13,profit_factor=1+1e-13)
        self.assertEqual(study.decision([r]),'KILL_NO_LIVE_PROMOTION')
        r['metrics'][study.MODEL]['resolved']=19
        self.assertEqual(study.decision([r]),'CONTINUE_INSUFFICIENT_SAMPLE')

    def test_no_pooling_duplicates_or_partial_periods(self):
        a,b=positive(resolved=40),positive('historical_replication_2',40)
        self.assertEqual(study.decision([a,b]),'CONTINUE_INSUFFICIENT_SAMPLE')
        with self.assertRaises(ValueError):study.decision([a,a])
        a=positive();b=positive('historical_replication_2');a['period_complete']=False
        self.assertEqual(study.decision([a,b]),'CONTINUE_INSUFFICIENT_SAMPLE')

    def test_known_history_positive_requires_untouched_future_and_live(self):
        self.assertIn('UNTOUCHED_FUTURE',study.decision([positive(),positive('historical_replication_2')]))
        self.assertIn('CAPTURED_LIVE',study.decision([positive('future_validation_1'),positive('future_validation_2')]))

    def test_practical_gates_win_cost_capital_and_increment(self):
        for kind in ('win','pf','stress','stress_pf','roi','open','uncertain','capital_sample','net','capped'):
            a,b=positive(),positive('historical_replication_2');m=a['metrics'][study.MODEL];s=a['stress_metrics'][study.MODEL];p=a['portfolios'][study.MODEL]
            if kind=='win':m['win_rate']=39
            if kind=='pf':m['profit_factor']=1.19
            if kind=='stress':s['avg_net_r']=0
            if kind=='stress_pf':s['profit_factor']=1
            if kind=='roi':p['closed_portfolio_roi_pct']=None
            if kind in ('open','uncertain'):p['active' if kind=='open' else 'uncertain']=1
            if kind=='capital_sample':p['resolved']=19
            if kind in ('net','capped'):a['comparison']['net_filled_count_difference' if kind=='net' else 'capped_filled_count_difference']=0
            self.assertEqual(study.decision([a,b]),'NO_QUALIFIED_REPLACEMENT')

    def test_failed_sources_block_qualification_not_zero_trades(self):
        a=positive();a['coverage']['failed_markets']=1
        self.assertEqual(study.decision([a]),'BLOCKED_DATA_QUALITY')

    def test_predecessor_full_evidence_required_and_kill_stops_next(self):
        with self.assertRaisesRegex(ValueError,'predecessor'):
            study.validate_previous('historical_replication_1',[],None,None)
        r=positive('development',20)|dict(engine_sha256=study.digest(Path(study.__file__).read_bytes()),source_dir='archive')
        r['metrics'][study.MODEL].update(avg_net_r=-.1,profit_factor=.9)
        with patch.object(study,'run',return_value=r):
            with self.assertRaisesRegex(ValueError,'stays sealed'):study.validate_previous('historical_replication_1',[r],None,None)
        with patch.object(study,'run',return_value=r|dict(mutated=True)):
            with self.assertRaisesRegex(ValueError,'does not reproduce'):study.validate_previous('historical_replication_1',[r],None,None)

    def test_opposite_side_not_shared_and_capped_identity_not_inferred(self):
        r=dict(ticker='X',side='LONG',created_ms=1,filled_ms=2)
        groups={study.control.MODELS[0]:[r],study.MODEL:[r|dict(side='SHORT')]}
        accounts={study.control.MODELS[0]:dict(filled=1),study.MODEL:dict(filled=1)}
        c=study.comparison(groups,accounts)
        self.assertEqual(c['shared_filled_setups'],0);self.assertEqual(c['net_filled_count_difference'],0)
        self.assertIsNone(c['capped_shared_setup_count'])


class RegistrationTests(unittest.TestCase):
    def test_protocol_and_original_dependencies_are_pinned(self):
        self.assertEqual(study.protocol()['model'],study.MODEL)
        with patch.object(study,'PROTOCOL_SHA256','changed'):
            with self.assertRaisesRegex(ValueError,'protocol changed'):study.protocol()

    def test_unknown_role_and_unfinished_future_are_rejected_before_source(self):
        with self.assertRaisesRegex(ValueError,'Unregistered role'):study.run('missing','new',None,None)
        with patch.object(study,'validate_previous'):
            with self.assertRaisesRegex(ValueError,'complete'):study.run('missing','future_validation_1',None,None,now_ms=0)

    def test_original_source_mutation_stops_without_evaluating_model(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'replay-report.json').write_text('{}')
            with patch.object(study,'replay_market') as model:
                with self.assertRaisesRegex(ValueError,'archive changed'):
                    study.run(p,'development',server.analyze_contract,server.SETTINGS)
                model.assert_not_called()

    def test_cli_quality_failure_keeps_unknown_metrics_and_original_source(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'source';source.mkdir();raw=source/'replay-report.json';raw.write_bytes(b'original')
            out=root/'out';argv=['study','--source',str(source),'--role','development','--output',str(out)]
            with patch('sys.argv',argv),patch.object(study,'run',side_effect=ValueError('control mismatch')):
                with self.assertRaisesRegex(ValueError,'control mismatch'):study.main()
            b=json.loads((out/'blocked.json').read_text());self.assertIsNone(b['metrics']);self.assertIsNone(b['portfolios'])
            self.assertEqual(raw.read_bytes(),b'original');self.assertFalse((out/'report.json').exists())


if __name__=='__main__':
    unittest.main()
