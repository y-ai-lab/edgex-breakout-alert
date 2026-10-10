"""Longer fixed periods do not change rules, sample units or sealed windows."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import vwap_long_robustness as study


def report(index=0, resolved=50):
    period = study.protocol()['periods'][index]
    m = dict(resolved=resolved, win_rate=50, avg_net_r=.5, profit_factor=2)
    return dict(role=period['id'], start_ms=period['start_ms'], end_ms=period['end_ms'],
                protocol_sha256=study.PINNED_PROTOCOL_SHA256, period_complete=True,
                coverage=dict(valid_points=1, failed_markets=0), metrics={study.vwap.MODEL:m},
                stress_metrics={study.vwap.MODEL:deepcopy(m)},
                portfolios={study.vwap.MODEL:dict(resolved=20, active=0, uncertain=0, closed_portfolio_roi_pct=1)},
                comparison=dict(net_filled_count_difference=5, capped_filled_count_difference=1))


class DecisionTests(unittest.TestCase):
    def test_both_periods_required_no_pooling_or_repeated_observation(self):
        self.assertEqual(study.decision([report(resolved=49), report(1,resolved=100)]), 'CONTINUE_INSUFFICIENT_SAMPLE')
        self.assertEqual(study.decision([report()]), 'CONTINUE_INSUFFICIENT_SAMPLE')
        with self.assertRaises(ValueError):study.decision([report(), report()])

    def test_historical_positive_requires_original_future_and_live_evidence(self):
        self.assertEqual(study.decision([report(), report(1)]), 'HISTORICAL_CRITERIA_MET_ORIGINAL_FUTURE_AND_LIVE_VALIDATION_REQUIRED')

    def test_negative_twenty_stops_audit_with_registered_tolerance(self):
        r=report(resolved=20);r['metrics'][study.vwap.MODEL].update(avg_net_r=1e-13,profit_factor=1+1e-13)
        self.assertEqual(study.decision([r]), 'REJECTED_HISTORICAL_SPECIFICATION_NO_LIVE_CHANGE')
        r['metrics'][study.vwap.MODEL]['resolved']=19
        self.assertEqual(study.decision([r]), 'CONTINUE_INSUFFICIENT_SAMPLE')

    def test_capital_quality_stress_and_pure_fills_required(self):
        for change in ('open', 'uncertain', 'roi', 'stress', 'stress_pf', 'net', 'capped', 'win', 'pf', 'capital_sample'):
            a,b=report(),report(1);m=a['metrics'][study.vwap.MODEL];s=a['stress_metrics'][study.vwap.MODEL];p=a['portfolios'][study.vwap.MODEL]
            if change=='open':p['active']=1
            if change=='uncertain':p['uncertain']=1
            if change=='roi':p['closed_portfolio_roi_pct']=None
            if change=='stress':s['avg_net_r']=0
            if change=='stress_pf':s['profit_factor']=1
            if change in ('net','capped'):a['comparison']['net_filled_count_difference' if change=='net' else 'capped_filled_count_difference']=0
            if change=='win':m['win_rate']=39
            if change=='pf':m['profit_factor']=1.19
            if change=='capital_sample':p['resolved']=19
            self.assertEqual(study.decision([a,b]), 'NO_QUALIFIED_REPLACEMENT')
        a['coverage']['valid_points']=0
        self.assertEqual(study.decision([a,b]), 'BLOCKED_DATA_QUALITY')

    def test_first_failed_period_cannot_be_replaced_by_better_second(self):
        a=report();a['coverage']['failed_markets']=1
        self.assertEqual(study.decision([a,report(1)]), 'BLOCKED_DATA_QUALITY')
        with patch.object(study,'run',return_value=a), tempfile.TemporaryDirectory() as d:
            f=Path(d)/'report.json';f.write_text(json.dumps(a))
            with self.assertRaisesRegex(ValueError,'stays sealed'):study.next_period_gate(f,None,None,None)

    def test_previous_full_ledger_mismatch_stops_next_collection(self):
        a=report()
        with patch.object(study,'run',return_value=a|{'mutated':True}), tempfile.TemporaryDirectory() as d:
            f=Path(d)/'report.json';f.write_text(json.dumps(a))
            with self.assertRaisesRegex(ValueError,'does not reproduce'):study.next_period_gate(f,None,None,None)

    def test_opposite_directions_are_not_shared_cross_family_entries(self):
        row=dict(ticker='X',side='LONG',created_ms=1,filled_ms=2)
        groups={study.control.MODELS[0]:[row],study.vwap.MODEL:[row|{'side':'SHORT'}]}
        accounts={study.control.MODELS[0]:{'filled':1},study.vwap.MODEL:{'filled':1}}
        c=study.comparison(groups,accounts)
        self.assertEqual((c['shared_filled_setups'],c['proposal_only_filled_setups'],c['current_filled_missing_in_proposal']),(0,1,1))
        self.assertEqual(c['net_filled_count_difference'],0);self.assertIsNone(c['capped_shared_setup_count'])


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source_dir=self.root/'source';self.source_dir.mkdir()
        p=study.protocol();period=p['periods'][0]
        self.contract=dict(contract_id='1',contract_name='X',quote_coin='USDC',enable_trade=True,enable_display=True)
        p['source_universe_sha256']=study.digest(study.canonical([self.contract]))
        candle=study.canonical(dict(contract=self.contract,HOUR_4=[],MINUTE_15=[]))
        (self.source_dir/'1.json').write_bytes(candle)
        manifest=dict(parameters=p['production_parameters'],rule_fingerprint=p['production_rule_fingerprint'],
                      indicator_windows=p['indicator_windows'],universe=[self.contract],failures=[],
                      sources=[dict(ticker='X',file='1.json',sha256=study.digest(candle))])
        self.source=dict(dataset='RETROSPECTIVE',eligible_for_live_promotion=False,start_ms=period['start_ms'],
            end_ms=period['end_ms'],manifest=manifest,signals=dict(current=[],shadow=[]),
            coverage=dict(valid_points=0,failed_markets=0,fetched_markets=1,expected_points_all_markets=28*96))
        self.write_source()
        self.p=p;self.addCleanup(patch.stopall)
        patch.object(study,'protocol',return_value=p).start()
        patch.object(study,'strategy_parameters',return_value=p['production_parameters']).start()
        patch.object(study,'rule_fingerprint',return_value=p['production_rule_fingerprint']).start()
        self.control=patch.object(study.control,'replay_market',return_value=dict(valid_points=0,records=[])).start()
        self.vwap=patch.object(study.vwap,'replay_market',return_value=dict(valid_points=0,records=[])).start()

    def write_source(self):
        raw=study.canonical(self.source);(self.source_dir/'replay-report.json').write_bytes(raw)
        seal=dict(source_report_sha256=study.digest(raw),files={x['file']:x['sha256'] for x in self.source['manifest']['sources']})
        (self.root/'source-seal.json').write_text(json.dumps(seal))

    def run_archive(self,**kw):return study.run(self.source_dir,0,None,None,**kw)

    def test_short_history_is_not_valid_zero_candidates(self):
        r=self.run_archive();self.assertEqual(study.decision([r]),'BLOCKED_DATA_QUALITY')
        self.assertFalse(r['eligible_for_live_promotion']);self.assertTrue(r['original_future_weeks_and_live_capture_unchanged'])

    def test_changed_raw_seal_or_candle_stops_before_evaluation(self):
        (self.source_dir/'1.json').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'checksum'):self.run_archive()
        self.control.assert_not_called();self.vwap.assert_not_called()

    def test_manifest_hash_mismatch_stops_before_reading_records(self):
        raw=(self.source_dir/'replay-report.json').read_bytes();(self.source_dir/'replay-report.json').write_bytes(raw+b' ')
        with self.assertRaisesRegex(ValueError,'initial seal'):self.run_archive()
        self.control.assert_not_called()

    def test_unknown_period_clock_seal_and_inventory_rejected(self):
        with self.assertRaisesRegex(ValueError,'complete'):self.run_archive(now_ms=self.p['periods'][0]['end_ms']-1)
        with self.assertRaisesRegex(ValueError,'Unregistered'):study.run(self.source_dir,True,None,None)
        self.source['manifest']['failures']=[dict(ticker='X')];self.write_source()
        with self.assertRaisesRegex(ValueError,'inventory'):self.run_archive()

    def test_original_comparator_reproduction_cannot_be_skipped(self):
        self.source['signals']['current']=[dict(setup_id='x',created_ms=1,entry=100,stop=90,target=120)]
        self.write_source()
        with self.assertRaisesRegex(ValueError,'first-entry mismatch'):self.run_archive()

    def test_sealed_period_and_source_traversal_are_rejected(self):
        self.p['sealed_periods'].append(self.p['periods'][0])
        with self.assertRaisesRegex(ValueError,'Reserved'):self.run_archive()
        self.p['sealed_periods'].pop()
        self.source['manifest']['sources'][0]['file']='../escape.json';self.write_source()
        with self.assertRaisesRegex(ValueError,'Unsafe'):self.run_archive()


class RegistrationTests(unittest.TestCase):
    def test_protocol_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'protocol.json';f.write_bytes(study.PROTOCOL.read_bytes()+b' ')
            with patch.object(study,'PROTOCOL',f),self.assertRaisesRegex(ValueError,'audit changed'):study.protocol()


if __name__=='__main__':unittest.main()
