"""Causal opening-range/expiry/quality/capital tests, no network or live writes."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import opening_range_replay as study
from analysis_terminal import server
from analysis_terminal.test_pending_entry_replay import CONTRACT, candle
from analysis_terminal.test_vwap_reclaim_replay import market as vwap_market, ORIGIN, SIGNAL, STEP


def market(side='LONG'):
    monitor, entries = vwap_market('LONG')
    entries[-1] = replace(entries[-1], open=100., close=100.3, low=99.99, high=100.4)
    if side == 'SHORT':
        def mirror(c):
            return replace(c, open=210-c.open, close=210-c.close, high=210-c.low, low=210-c.high)
        monitor, entries = [mirror(c) for c in monitor], [mirror(c) for c in entries]
    return monitor, entries


def record(side='LONG'):
    m, e = market(side)
    r, _, reason = study.candidate(CONTRACT, m[-180:], e[-180:])
    assert r, reason
    return r


def state(r):
    return dict(setup_id=r['setup_id'], confirmation_color_ok=True, confirmation_level_ok=True,
                retest_touched=True, stop_valid=True)


def fill(r, offset=0):
    p = r['trigger']
    return candle(SIGNAL+(1+offset)*STEP, open=p, close=p, low=p-.01, high=p+.01)


def evaluate(r, cs, state_at=None, end_ms=None):
    return study.evaluate(r, cs, state_at or (lambda _:state(r)),
                          end_ms=end_ms or max(c.time_ms for c in cs)+STEP)


def positive(role='historical_replication_1', resolved=50):
    p = study.periods(study.protocol())[role]
    metric = dict(resolved=resolved, avg_net_r=.5, profit_factor=2., win_rate=50.)
    return dict(role=role, start_ms=p['start_ms'], end_ms=p['end_ms'],
                protocol_sha256=study.PROTOCOL_SHA256, period_complete=True,
                coverage=dict(valid_points=100, failed_markets=0), metrics={study.MODEL:metric},
                stress_metrics={study.MODEL:deepcopy(metric)},
                portfolios={study.MODEL:dict(active=0, uncertain=0, resolved=20, closed_portfolio_roi_pct=1.)},
                comparison=dict(net_filled_count_difference=10, capped_filled_count_difference=1))


class ChronologyTests(unittest.TestCase):
    def test_opening_range_exact_hour_excludes_signal_and_previous_day(self):
        for side in ('LONG', 'SHORT'):
            m, e = market(side)
            r = record(side)
            self.assertEqual(r['session_start_ms'], ORIGIN)
            self.assertEqual((r['opening_range_high'],r['opening_range_low']),
                             (100.2,99.8) if side=='LONG' else (110.2,109.8))
            extreme = replace(e[-1], high=1000, low=1)
            self.assertEqual(study.opening_range(e[-180:]), study.opening_range((e[:-1]+[extreme])[-180:]))
            self.assertEqual((r['created_ms'],r['expires_ms']), (SIGNAL+STEP+1,SIGNAL+5*STEP))
            levels = study.control.cost_levels(r['trigger'],r['stop'],r['target'],side)
            self.assertAlmostEqual(levels['net_rr'],2)

    def test_color_close_and_first_boundary_cross_are_strict(self):
        for side in ('LONG','SHORT'):
            m,e=market(side);c=e[-1];level=100.2 if side=='LONG' else 109.8
            for changed in (replace(c,open=c.close),replace(c,close=level),
                            replace(c,low=level+.01) if side=='LONG' else replace(c,high=level-.01)):
                self.assertIsNone(study.candidate(CONTRACT,m[-180:],(e[:-1]+[changed])[-180:])[0])
            e[-2]=replace(e[-2],close=100.3 if side=='LONG' else 109.7)
            self.assertIsNone(study.candidate(CONTRACT,m[-180:],e[-180:])[0])

    def test_trend_mismatch_first_cross_is_consumed(self):
        m,e=market();r,episode,reason=study.candidate(CONTRACT,m[-180:],e[-180:],direction='SHORT')
        self.assertIsNone(r);self.assertEqual(episode,('LONG',ORIGIN))
        self.assertEqual(reason,'FIRST_BREAK_WRONG_4H_TREND')

    def test_missing_opening_bar_or_unfinished_hour_no_range(self):
        m,e=market();self.assertIsNone(study.opening_range(e[:-1][-180:]))
        self.assertIsNone(study.opening_range([c for c in e if c.time_ms!=ORIGIN][-180:]))

    def test_missing_or_future_4h_not_used(self):
        m,e=market()
        for changed in (m[-179:],m[-179:]+[replace(m[-1],time_ms=ORIGIN)]):
            with self.assertRaises(ValueError):study.candidate(CONTRACT,changed,e[-180:])

    def test_target_room_excludes_signal_high_and_stop_is_not_fitted(self):
        m,e=market();e=[replace(c,high=100.2) if c.time_ms<SIGNAL and c.high>100.2
                      else replace(c,high=1000) if c.time_ms==SIGNAL else c for c in e]
        r,episode,reason=study.candidate(CONTRACT,m[-180:],e[-180:])
        self.assertIsNone(r);self.assertIsNotNone(episode);self.assertEqual(reason,'NET_ROOM_BELOW_2R')
        r=record();m,e=market();atr=study.scanner._atr(e[-180:],14)
        self.assertEqual(r['stop'],99.8-.5*atr)

    def test_signal_and_prior_exit_touches_never_resolve_or_fill(self):
        r=record();cs=[candle(SIGNAL,high=1000,low=1),fill(r)]
        result=evaluate(r,cs)
        self.assertEqual((result['status'],result['filled_ms']),('OPEN',SIGNAL+STEP))
        self.assertIsNone(result['final_net_r'])

    def test_unknown_fill_exit_and_both_later_exits_ambiguous(self):
        for side in ('LONG','SHORT'):
            r=record(side)
            both=replace(fill(r),high=max(r['stop'],r['target'])+1,low=min(r['stop'],r['target'])-1)
            self.assertEqual(evaluate(r,[both])['status'],'AMBIGUOUS')
            result=evaluate(r,[fill(r),replace(both,time_ms=SIGNAL+2*STEP)])
            self.assertEqual(result['status'],'AMBIGUOUS');self.assertIsNone(result['final_net_r'])

    def test_later_single_tp_costs_and_adverse_stop_gap(self):
        for side in ('LONG','SHORT'):
            r=record(side);f=fill(r)
            tp=replace(f,time_ms=f.time_ms+STEP,high=r['target']+.01) if side=='LONG' else replace(f,time_ms=f.time_ms+STEP,low=r['target']-.01)
            result=evaluate(r,[f,tp]);self.assertEqual(result['status'],'TP');self.assertAlmostEqual(result['final_net_r'],2)
            p=r['stop']-1 if side=='LONG' else r['stop']+1
            gap=candle(f.time_ms+STEP,open=p,close=p,low=p-.01,high=p+.01)
            result=evaluate(r,[f,gap]);self.assertEqual(result['status'],'SL');self.assertLess(result['final_net_r'],-1)

    def test_gap_or_missing_state_halts_no_later_profit(self):
        r=record();later=replace(fill(r,2),high=r['target']+1)
        for cs in ([later],[fill(r),later]):
            result=evaluate(r,cs);self.assertEqual(result['status'],'DATA_GAP');self.assertIsNone(result['final_net_r'])
        self.assertEqual(evaluate(r,[fill(r)],state_at=lambda _:None)['status'],'DATA_GAP')

    def test_pending_uses_prior_strict_state_and_expires_without_fifth_bar(self):
        r=record()
        for key in ('confirmation_color_ok','confirmation_level_ok','retest_touched','stop_valid'):
            result=evaluate(r,[fill(r)],state_at=lambda _,key=key:state(r)|{key:False})
            self.assertEqual(result['status'],'INVALIDATED')
        cs=[replace(fill(r,i),open=r['trigger']+.1,close=r['trigger']+.1,low=r['trigger']+.05,high=r['trigger']+.2) for i in range(4)]
        result=evaluate(r,cs+[fill(r,4)])
        self.assertEqual(result['status'],'EXPIRED');self.assertIsNone(result['filled_ms'])

    def test_open_and_unknown_capital_roi_remains_null(self):
        r=record()
        for cs in ([fill(r)],[replace(fill(r),high=r['target']+1)]):
            result=evaluate(r,cs);a=study.control.portfolio([result])
            self.assertEqual(a['active'],1);self.assertIsNone(a['closed_portfolio_roi_pct'])

    def test_first_room_failure_is_not_rearmed_on_later_valid_break(self):
        m,e=market();e=[replace(c,high=100.2) if c.time_ms<SIGNAL and c.high>100.2 else c for c in e]
        # First large high does not extend its own TP room; later window would qualify.
        e[-1]=replace(e[-1],high=110)
        e.extend([candle(SIGNAL+STEP,open=100.3,close=100.,low=99.99,high=100.4),
                  candle(SIGNAL+2*STEP,open=100.,close=100.3,low=99.99,high=100.4)])
        r=study.replay_market(CONTRACT,m,e,start_ms=SIGNAL+STEP,end_ms=SIGNAL+4*STEP)
        self.assertEqual(r['records'],[]);self.assertEqual(r['exclusions']['NET_ROOM_BELOW_2R'],1)
        self.assertGreater(r['exclusions']['ALREADY_CONSUMED_FIRST_BREAK'],0)

    def test_preperiod_first_cross_and_session_gap_are_not_new_entries(self):
        m,e=market()
        r=study.replay_market(CONTRACT,m,e,start_ms=SIGNAL+2*STEP,end_ms=SIGNAL+3*STEP)
        self.assertFalse(r['records']);self.assertGreater(r['exclusions'].get('WARMUP_FIRST_BREAK',0),0)
        e=[c for c in e if c.time_ms!=ORIGIN]
        r=study.replay_market(CONTRACT,m,e,start_ms=SIGNAL+STEP,end_ms=SIGNAL+2*STEP)
        self.assertFalse(r['records'])

    def test_warmup_consumes_only_identity_without_pricing_sealed_inputs(self):
        m,e=market()
        with patch.object(study.scanner,'_atr',side_effect=AssertionError('No warmup pricing')):
            r,episode,reason=study.candidate(CONTRACT,m[-180:],e[-180:],price=False)
        self.assertIsNone(r);self.assertEqual(episode,('LONG',ORIGIN));self.assertEqual(reason,'WARMUP_FIRST_BREAK')


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
