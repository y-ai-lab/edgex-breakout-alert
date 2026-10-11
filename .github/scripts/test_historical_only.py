"""Historical conclusion never invents future data or releases uncertain money."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import historical_only as audit
from analysis_terminal.test_pending_followup import fixture
from analysis_terminal.test_pending_entry_replay import candle, STEP


def report(index=0, resolved=50):
    p = audit.protocol(); x = p['periods'][index]
    m = dict(resolved=resolved, win_rate=50, avg_net_r=.5, profit_factor=2)
    return dict(role=x['id'], **{k:x[k] for k in ('start_ms','end_ms','exit_cutoff_ms')},
        protocol_sha256=audit.PIN, period_complete=True,
        coverage=dict(failed_markets=0, valid_market_observations=1),
        final=dict(metrics={audit.vwap.MODEL:m}, stress_metrics={audit.vwap.MODEL:deepcopy(m)},
            portfolios={audit.vwap.MODEL:dict(resolved=20, active=0, uncertain=0, closed_portfolio_roi_pct=1)},
            comparison=dict(net_filled_count_difference=1,capped_filled_count_difference=1),
            blocked_records={audit.vwap.MODEL:0}))


class DecisionTests(unittest.TestCase):
    def test_two_positive_completed_past_intervals_do_not_wait_for_future(self):
        self.assertEqual(audit.decision([report(),report(1)]),
            'BACKTEST_CANDIDATE_BASE_COSTS_ONLY_FUNDING_AND_REAL_FILL_UNVERIFIED')

    def test_period_samples_are_not_pooled_to_meet_floor(self):
        self.assertEqual(audit.decision([report(resolved=30),report(1,resolved=30)]),
            'BACKTEST_INSUFFICIENT_SAMPLE_NO_QUALIFIED_REPLACEMENT')
        with self.assertRaises(ValueError):audit.decision([report(),report()])

    def test_negative_sufficient_sample_kills_before_second(self):
        r = report(resolved=20); r['final']['metrics'][audit.vwap.MODEL].update(avg_net_r=-.1,profit_factor=.8)
        self.assertEqual(audit.decision([r]),'KILL_HISTORICAL_ONLY_NO_LIVE_CHANGE')
        r['final']['metrics'][audit.vwap.MODEL]['resolved']=19
        self.assertEqual(audit.decision([r]),'BACKTEST_INSUFFICIENT_SAMPLE_NO_QUALIFIED_REPLACEMENT')

    def test_win_rate_cost_pure_fill_and_closed_capital_are_required(self):
        for kind in ('win','pf','stress','stress_pf','net','capped','roi','active','uncertain','capital_sample','blocked'):
            a,b=report(),report(1); f=a['final']
            if kind=='win':f['metrics'][audit.vwap.MODEL]['win_rate']=39
            elif kind=='pf':f['metrics'][audit.vwap.MODEL]['profit_factor']=1.1
            elif kind=='stress':f['stress_metrics'][audit.vwap.MODEL]['avg_net_r']=0
            elif kind=='stress_pf':f['stress_metrics'][audit.vwap.MODEL]['profit_factor']=1
            elif kind in ('net','capped'):f['comparison']['net_filled_count_difference' if kind=='net' else 'capped_filled_count_difference']=0
            elif kind=='roi':f['portfolios'][audit.vwap.MODEL]['closed_portfolio_roi_pct']=None
            elif kind in ('active','uncertain'):f['portfolios'][audit.vwap.MODEL][kind]=1
            elif kind=='capital_sample':f['portfolios'][audit.vwap.MODEL]['resolved']=19
            else:f['blocked_records'][audit.vwap.MODEL]=1
            with self.subTest(kind=kind):self.assertEqual(audit.decision([a,b]),'BACKTEST_NO_QUALIFIED_REPLACEMENT')

    def test_no_coverage_is_not_a_zero_profit_result(self):
        r=report();r['coverage']['valid_market_observations']=0
        self.assertEqual(audit.decision([r]),'BLOCKED_DATA_QUALITY')

    def test_future_exit_cutoff_and_unknown_period_rejected(self):
        x=audit.protocol()['periods'][0]
        with self.assertRaises(ValueError):audit.period(0,now_ms=x['exit_cutoff_ms']-1)
        with self.assertRaises(ValueError):audit.period(True)
        with self.assertRaises(ValueError):audit.period(2)

    def test_second_collection_cannot_skip_first_reproduction(self):
        with self.assertRaisesRegex(ValueError,'First full historical'):
            audit.collect(1,None,None,None)
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'first.json';r=report();f.write_text(json.dumps(r))
            with patch.object(audit,'run',return_value=r|{'changed':True}):
                with self.assertRaisesRegex(ValueError,'does not reproduce'):audit.collect(1,None,None,None,previous_report=f,previous_source=d)
            r['final']['metrics'][audit.vwap.MODEL].update(avg_net_r=-1,profit_factor=0);f.write_text(json.dumps(r))
            with patch.object(audit,'run',return_value=r):
                with self.assertRaisesRegex(ValueError,'stopped'):audit.collect(1,None,None,None,previous_report=f,previous_source=d)


class ChronologyTests(unittest.TestCase):
    def test_existing_long_and_short_open_followup_retains_entry(self):
        for side in ('LONG','SHORT'):
            base,_=fixture(side=side);r=base['records'][0];start=base['end_ms']
            r['model']=audit.vwap.MODEL;before=deepcopy(r)
            cs=[candle(start,open=110,low=109,high=121,close=120)] if side=='LONG' else [candle(start,open=90,high=91,low=79,close=80)]
            result=audit.resume_open(r,cs,start_ms=start,end_ms=start+STEP)
            self.assertEqual(result['status'],'TP');self.assertAlmostEqual(result['final_net_r'],2)
            self.assertEqual((result['model'],result['filled_ms'],result['entry']),(r['model'],r['filled_ms'],r['entry']))
            self.assertEqual(r,before)

    def test_missing_first_followup_bar_never_uses_later_profit(self):
        base,_=fixture();r=base['records'][0];start=base['end_ms']
        result=audit.resume_open(r,[candle(start+STEP,high=130)],start_ms=start,end_ms=start+2*STEP)
        self.assertEqual(result['status'],'DATA_GAP');self.assertIsNone(result['final_net_r'])
        self.assertIsNone(audit.control.portfolio([result])['closed_portfolio_roi_pct'])

    def test_same_bar_both_exits_are_ambiguous_and_money_stays_locked(self):
        base,_=fixture();start=base['end_ms']
        result=audit.resume_open(base['records'][0],[candle(start,high=130,low=80)],start_ms=start,end_ms=start+STEP)
        self.assertEqual(result['status'],'AMBIGUOUS')
        self.assertIsNone(audit.control.portfolio([result])['closed_portfolio_roi_pct'])

    def test_signal_fill_bar_and_beyond_fixed_cutoff_are_not_used(self):
        base,_=fixture();r=base['records'][0];start=base['end_ms']
        cs=[candle(r['signal_candle_ms'],high=130,low=80),candle(start),candle(start+STEP,high=130)]
        result=audit.resume_open(r,cs,start_ms=start,end_ms=start+STEP)
        self.assertEqual(result['status'],'OPEN')
        with self.assertRaises(ValueError):audit.resume_open(r,cs,start_ms=r['filled_ms'],end_ms=start+STEP)

    def test_pending_and_terminal_records_are_immutable(self):
        for status in ('PENDING','SL'):
            base,_=fixture(status);r=base['records'][0];before=deepcopy(r)
            result=audit.resume_open(r,[candle(base['end_ms'],high=130)],start_ms=base['end_ms'],end_ms=base['end_ms']+STEP)
            self.assertEqual(result,before);self.assertEqual(r,before)


class SourceTests(unittest.TestCase):
    def test_frozen_dependency_or_protocol_edit_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'p.json';f.write_bytes(audit.PROTOCOL.read_bytes()+b' ')
            with patch.object(audit,'PROTOCOL',f):
                with self.assertRaises(ValueError):audit.protocol()

    def test_raw_manifest_edit_fails_before_market_execution(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);raw=b'{"unexpected":true}'
            (root/'manifest.json').write_bytes(raw)
            (root/'seal.json').write_text(json.dumps(dict(manifest_sha256='changed')))
            p=audit.protocol()
            with patch.object(audit,'strategy_parameters',return_value=p['production_parameters']),patch.object(audit,'rule_fingerprint',return_value=p['production_rule_fingerprint']),patch.object(audit.control,'replay_market') as run:
                with self.assertRaisesRegex(ValueError,'seal'):audit.run(root,0,None,None)
                run.assert_not_called()

    def test_failed_public_page_is_not_empty_history(self):
        with self.assertRaises(ValueError):audit.history(Mock(return_value={'code':'FAIL'}),Mock(contract_id='1'),'MINUTE_15',STEP,3*STEP)

    def test_nonempty_cursor_with_empty_page_is_rejected(self):
        get=Mock(return_value=dict(code='SUCCESS',data=dict(dataList=[],nextPageOffsetData='repeat')))
        with self.assertRaises(ValueError):audit.history(get,Mock(contract_id='1'),'MINUTE_15',STEP,3*STEP)


if __name__=='__main__':unittest.main()
