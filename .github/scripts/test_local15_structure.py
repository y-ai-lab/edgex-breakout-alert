"""Synthetic chronology and preregistration checks, no network or live writes."""
from copy import deepcopy
from dataclasses import asdict,replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import local15_structure as study
from analysis_terminal import server
from analysis_terminal.test_pending_entry_replay import CONTRACT,candle,row,START,STEP


def confirmed(side='LONG'):
    r=row(side);r.update(confirmation_color_ok=True,confirmation_level_ok=True,atr_15m=2)
    return r


def bars(side='LONG'):
    # Local SL=97 LONG or103 SHORT, original 4H SL=90/110.
    return [candle(START-(4-i)*STEP,open=99 if side=='LONG' else 101,
                   high=102,low=98,close=101 if side=='LONG' else 99) for i in range(4)]


def record(side='LONG'):
    r,reason=study.candidate(confirmed(side),bars(side))
    assert reason is None
    return r|dict(step_size=.01,min_order_size=.01,max_order_size=1000)


class ChronologyTests(unittest.TestCase):
    def test_local_stop_target_and_clock_fixed_symmetrically(self):
        for side,stop,target in [('LONG',97,120),('SHORT',103,80)]:
            r=record(side)
            self.assertEqual((r['stop'],r['target'],r['created_ms'],r['expires_ms']),
                             (stop,target,START+1,START+STEP))
            self.assertNotEqual(r['stop'],r['original_4h_stop'])

    def test_no_direction_level_or_retest_relaxation(self):
        for field in ('confirmed','confirmation_color_ok','confirmation_level_ok','retest_touched'):
            r=confirmed();r[field]=False
            self.assertIsNone(study.candidate(r,bars())[0])

    def test_stop_not_narrowed_to_force_rr_or_widened_beyond_4h(self):
        r=confirmed();r['entry_band']['structural_target']=103
        self.assertEqual(study.candidate(r,bars())[1],'NO_NET_2R_AT_CONFIRMATION')
        r=confirmed();r['entry_band']['structural_stop']=99
        self.assertEqual(study.candidate(r,bars())[1],'INVALID_OR_WIDER_LOCAL_STRUCTURE')
        for v in (float('nan'),float('inf'),True,-1):
            r=confirmed();r['atr_15m']=v
            self.assertIsNone(study.candidate(r,bars())[0])

    def test_confirmation_extremes_not_fill_or_exit(self):
        r=record();cs=[candle(START-STEP,high=130,low=80),candle(START,open=101,high=102,low=100)]
        result=study.evaluate(r,cs,end_ms=START+STEP)
        self.assertEqual((result['status'],result['filled_ms']),('OPEN',START))
        self.assertIsNone(result['final_net_r'])

    def test_first_open_not_touch_limit_wait_and_no_rearm(self):
        r=record();result=study.evaluate(r,[candle(START,open=110,high=111,low=100),
            candle(START+STEP,open=101,high=121,low=100)],end_ms=START+2*STEP)
        self.assertEqual(result['status'],'REJECTED_AT_FILL')
        self.assertIsNone(result['filled_ms'])

    def test_costs_can_reject_gross_two_r(self):
        r=record();r['target']=109.1;r['trigger']=101
        levels=study.control.cost_levels(101,r['stop'],r['target'],r['side'])
        self.assertGreaterEqual((r['target']-levels['entry'])/(levels['entry']-r['stop']),2)
        self.assertLess(levels['net_rr'],2)
        result=study.evaluate(r,[candle(START,open=101)],end_ms=START+STEP)
        self.assertEqual(result['status'],'REJECTED_AT_FILL')

    def test_next_open_single_exit_known_both_exits_ambiguous(self):
        r=record()
        for side in ('LONG','SHORT'):
            r=record(side)
            result=study.evaluate(r,[candle(START,open=101 if side=='LONG' else 99,
                high=121 if side=='LONG' else 100,low=100 if side=='LONG' else 79)],end_ms=START+STEP)
            self.assertEqual(result['status'],'TP')
            self.assertGreater(result['final_net_r'],2)
        result=study.evaluate(record(),[candle(START,open=101,high=121,low=96)],end_ms=START+STEP)
        self.assertEqual(result['status'],'AMBIGUOUS');self.assertIsNone(result['final_net_r'])

    def test_missing_first_or_following_bar_stops_without_later_profit(self):
        for cs in ([candle(START+STEP,high=121)],
                   [candle(START),candle(START+2*STEP,high=121)]):
            result=study.evaluate(record(),cs,end_ms=START+3*STEP)
            self.assertEqual(result['status'],'DATA_GAP');self.assertIsNone(result['final_net_r'])

    def test_gap_stop_charges_worse_open_after_fill(self):
        result=study.evaluate(record(),[candle(START),candle(START+STEP,open=90,high=91,low=89,close=90)],
                              end_ms=START+2*STEP)
        self.assertEqual(result['status'],'SL');self.assertLess(result['final_net_r'],-1)
        self.assertEqual(result['stop'],97)

    def test_open_and_ambiguous_capital_not_released(self):
        for cs in ([candle(START)],[candle(START,high=121,low=96)]):
            result=study.evaluate(record(),cs,end_ms=START+STEP)
            a=study.control.portfolio([result])
            self.assertEqual(a['active'],1);self.assertIsNone(a['closed_portfolio_roi_pct'])

    def test_first_failed_confirmation_consumed_even_if_later_valid(self):
        monitor=[replace(candle(START-i*16*STEP),interval='HOUR_4') for i in range(180,0,-1)]
        entries=[candle(START-i*STEP) for i in range(180,0,-1)]+[candle(START)]
        r=confirmed();r['entry_band']['structural_target']=103
        later=confirmed()
        settings=SimpleNamespace(roll_max_age=6,retest_lookback=4)
        def analyze(c,m,e,*,as_of_ms):
            return r if e[-1].time_ms<START else later
        result=study.replay_market(CONTRACT,monitor,entries,analyze,settings,start_ms=START,end_ms=START+2*STEP)
        self.assertEqual(result['records'],[])
        self.assertEqual(result['excluded']['NO_NET_2R_AT_CONFIRMATION'],1)

    def test_warmup_confirmation_not_reintroduced_in_period(self):
        monitor=[replace(candle(START-i*16*STEP),interval='HOUR_4') for i in range(190,0,-1)]
        entries=[candle(START-i*STEP) for i in range(190,0,-1)]+[candle(START)]
        settings=SimpleNamespace(roll_max_age=6,retest_lookback=4)
        result=study.replay_market(CONTRACT,monitor,entries,lambda *a,**k:confirmed(),settings,
            start_ms=START,end_ms=START+2*STEP)
        self.assertFalse(result['records'])

    def test_missing_prior_indicator_state_blocks_old_setup_origin(self):
        monitor=[replace(candle(START-i*16*STEP),interval='HOUR_4') for i in range(180,0,-1)]
        entries=[candle(START-i*STEP) for i in range(180,0,-1)]+[candle(START)]
        r=confirmed();r['breakout_time_ms']=START-32*STEP
        result=study.replay_market(CONTRACT,monitor,entries,lambda *a,**k:r,
            SimpleNamespace(roll_max_age=6,retest_lookback=4),start_ms=START,end_ms=START+2*STEP)
        self.assertFalse(result['records']);self.assertEqual(result['excluded']['UNKNOWN_SETUP_ORIGIN'],1)


def positive(role='unused_historical_week_1',resolved=50):
    period=study.periods(study.protocol())[role]
    m=dict(resolved=resolved,win_rate=50,avg_net_r=.5,profit_factor=2)
    return dict(role=role,start_ms=period['start_ms'],end_ms=period['end_ms'],protocol_sha256=study.PROTOCOL_SHA256,
        period_complete=True,coverage={'failed_markets':0,'valid_points':1},metrics={study.MODEL:m},
        stress_metrics={study.MODEL:deepcopy(m)},portfolios={study.MODEL:dict(resolved=20,active=0,uncertain=0,closed_portfolio_roi_pct=1)},
        comparison=dict(net_filled_count_difference=1,capped_filled_count_difference=1))


class DecisionTests(unittest.TestCase):
    def test_negative_twenty_kills_without_accessing_unused_periods(self):
        r=positive('development',20);r['metrics'][study.MODEL].update(avg_net_r=-.1,profit_factor=.8)
        self.assertEqual(study.decision([r]),'KILL_NO_LIVE_PROMOTION')
        r['metrics'][study.MODEL]['resolved']=19
        self.assertEqual(study.decision([r]),'CONTINUE_INSUFFICIENT_SAMPLE')

    def test_both_fifty_each_and_no_pooling_duplicates_or_partial_period(self):
        a=positive();b=positive('unused_historical_week_2')
        self.assertIn('SEPARATE_LIVE',study.decision([a,b]))
        with self.assertRaises(ValueError):study.decision([a,a])
        a['metrics'][study.MODEL]['resolved']=40;b['metrics'][study.MODEL]['resolved']=40
        self.assertEqual(study.decision([a,b]),'CONTINUE_INSUFFICIENT_SAMPLE')
        a['metrics'][study.MODEL]['resolved']=50;b['metrics'][study.MODEL]['resolved']=50;a['period_complete']=False
        self.assertEqual(study.decision([a,b]),'CONTINUE_INSUFFICIENT_SAMPLE')

    def test_unknown_roi_stress_costs_fill_increment_and_quality_block(self):
        for kind in ('roi','stress','pure','capped','win','pf','open'):
            a=positive();b=positive('unused_historical_week_2')
            if kind=='roi':a['portfolios'][study.MODEL]['closed_portfolio_roi_pct']=None
            if kind=='stress':a['stress_metrics'][study.MODEL]['avg_net_r']=0
            if kind in ('pure','capped'):a['comparison']['net_filled_count_difference' if kind=='pure' else 'capped_filled_count_difference']=0
            if kind=='win':a['metrics'][study.MODEL]['win_rate']=39
            if kind=='pf':a['metrics'][study.MODEL]['profit_factor']=1.19
            if kind=='open':a['portfolios'][study.MODEL]['active']=1
            self.assertEqual(study.decision([a,b]),'NO_QUALIFIED_REPLACEMENT')
        a['coverage']['failed_markets']=1
        self.assertEqual(study.decision([a,b]),'BLOCKED_DATA_QUALITY')

    def test_unused_period_requires_original_predecessor_ledger(self):
        with self.assertRaisesRegex(ValueError,'predecessor'):
            study.validate_predecessors('unused_historical_week_1',[])
        with self.assertRaisesRegex(ValueError,'evidence'):
            study.validate_predecessors('unused_historical_week_1',[positive('development')|dict(engine_sha256='wrong')])

    def test_actual_negative_predecessor_keeps_unused_period_sealed(self):
        period=study.periods(study.protocol())['development']
        records=[record()|dict(key='unique:'+str(i),status='SL',filled_ms=START,outcome_ms=START+STEP,
            nominal_fill=101,entry=101.0202,net_risk=4.16,net_pnl_per_unit=-4.16,final_net_r=-1.,exit_price=96.9806) for i in range(20)]
        groups={m:records if m==study.MODEL else [] for m in study.MODELS}
        account={m:study.control.portfolio(rows) for m,rows in groups.items()}
        r=dict(role='development',start_ms=period['start_ms'],end_ms=period['end_ms'],
            protocol_sha256=study.PROTOCOL_SHA256,engine_sha256=study.digest(Path(study.__file__).read_bytes()),
            period_complete=True,records=records,coverage={'failed_markets':0,'valid_points':1},
            metrics={m:study.control.metrics(rows) for m,rows in groups.items()},
            portfolios=account,stress_metrics={m:study.stressed_metrics(rows) for m,rows in groups.items()},
            comparison=study.compare(groups,account))
        with self.assertRaisesRegex(ValueError,'stays sealed'):
            study.validate_predecessors('unused_historical_week_1',[r])


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);p=study.protocol()
        p['source_universe_sha256']=study.digest(study.canonical([asdict(CONTRACT)]))
        source=dict(dataset='RETROSPECTIVE',eligible_for_live_promotion=False,
            start_ms=p['development']['start_ms'],end_ms=p['development']['end_ms'],
            manifest=dict(parameters=p['production_parameters'],rule_fingerprint=p['production_rule_fingerprint'],
                indicator_windows=p['indicator_windows'],universe=[asdict(CONTRACT)],failures=[],
                sources=[dict(ticker=CONTRACT.contract_name,file='candles.json',sha256='')]),
            signals=dict(current=[],shadow=[]),coverage=dict(valid_points=0,expected_points_all_markets=672,
                failed_markets=0,fetched_markets=1))
        self.candles=self.root/'candles.json';self.candles.write_bytes(study.canonical(dict(contract=asdict(CONTRACT),HOUR_4=[],MINUTE_15=[])))
        source['manifest']['sources'][0]['sha256']=study.digest(self.candles.read_bytes())
        self.source=source;self.write_source()
        p['development']['source_report_sha256']=study.digest((self.root/'replay-report.json').read_bytes())
        self.proto=self.root/'protocol.json';self.proto.write_bytes(study.canonical(p))
        self.addCleanup(patch.stopall)
        patch.object(study,'PROTOCOL',self.proto).start()
        patch.object(study,'PROTOCOL_SHA256',study.digest(self.proto.read_bytes())).start()

    def write_source(self):
        (self.root/'replay-report.json').write_bytes(study.canonical(self.source))

    def run_archive(self):
        return study.run(self.root,'development',server.analyze_contract,server.SETTINGS)

    def test_empty_history_is_not_valid_zero_candidate_performance(self):
        r=self.run_archive();self.assertEqual(study.decision([r]),'BLOCKED_DATA_QUALITY')
        self.assertIsNone(r['metrics'][study.MODEL]['avg_net_r'])

    def test_registered_source_and_candle_tamper_stop(self):
        self.candles.write_bytes(self.candles.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'checksum'):self.run_archive()
        self.source['end_ms']-=STEP;self.write_source()
        with self.assertRaisesRegex(ValueError,'source changed'):self.run_archive()

    def test_registered_protocol_and_changed_analyzer_stop(self):
        self.proto.write_bytes(self.proto.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'protocol changed'):self.run_archive()


if __name__=='__main__':unittest.main()
