"""Additional diagnostic stays separate from registered VWAP future samples."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import vwap_robustness as study


def report(index=0,resolved=50):
    p=study.protocol();period=p['periods'][index]
    m=dict(resolved=resolved,win_rate=50,avg_net_r=.5,profit_factor=2)
    return dict(role=period['id'],start_ms=period['start_ms'],end_ms=period['end_ms'],
        protocol_sha256=study.PINNED_PROTOCOL_SHA256,period_complete=True,
        coverage={'failed_markets':0,'valid_points':1},metrics={study.vwap.MODEL:m},
        stress_metrics={study.vwap.MODEL:deepcopy(m)},
        portfolios={study.vwap.MODEL:dict(resolved=20,active=0,uncertain=0,closed_portfolio_roi_pct=1)},
        comparison=dict(net_filled_count_difference=5,capped_filled_count_difference=1))


class DecisionTests(unittest.TestCase):
    def test_positive_past_diagnostic_never_replaces_future_or_live_validation(self):
        self.assertEqual(study.decision([report(),report(1)]),
            'RETROSPECTIVE_DIAGNOSTIC_POSITIVE_ORIGINAL_FUTURE_AND_LIVE_VALIDATION_STILL_REQUIRED')

    def test_separate_fifty_floor_and_no_daily_repeat_pooling(self):
        self.assertEqual(study.decision([report(resolved=40),report(1,resolved=40)]),'CONTINUE_INSUFFICIENT_SAMPLE')
        with self.assertRaises(ValueError):study.decision([report(),report()])

    def test_negative_diagnostic_does_not_change_original_live_decision(self):
        r=report(resolved=20);r['metrics'][study.vwap.MODEL].update(avg_net_r=-.1,profit_factor=.8)
        self.assertEqual(study.decision([r]),'REJECTED_HISTORICAL_ROBUSTNESS_NO_LIVE_CHANGE')
        r['metrics'][study.vwap.MODEL]['resolved']=19
        self.assertEqual(study.decision([r]),'CONTINUE_INSUFFICIENT_SAMPLE')

    def test_roi_costs_fill_deltas_and_data_quality_are_required(self):
        for kind in ('roi','stress','capped','net','win','pf','active'):
            a=report();b=report(1)
            if kind=='roi':a['portfolios'][study.vwap.MODEL]['closed_portfolio_roi_pct']=None
            if kind=='stress':a['stress_metrics'][study.vwap.MODEL]['avg_net_r']=0
            if kind in ('capped','net'):a['comparison']['capped_filled_count_difference' if kind=='capped' else 'net_filled_count_difference']=0
            if kind=='win':a['metrics'][study.vwap.MODEL]['win_rate']=39
            if kind=='pf':a['metrics'][study.vwap.MODEL]['profit_factor']=1.19
            if kind=='active':a['portfolios'][study.vwap.MODEL]['active']=1
            self.assertEqual(study.decision([a,b]),'NO_QUALIFIED_REPLACEMENT')
        a['period_complete']=False
        self.assertEqual(study.decision([a,b]),'BLOCKED_DATA_QUALITY')


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);p=study.protocol();period=p['periods'][0]
        source={'manifest':{'sources':[]}}
        (self.root/'replay-report.json').write_bytes(study.baseline.canonical(source))
        baseline=dict(role=period['id'],start_ms=period['start_ms'],end_ms=period['end_ms'],records=[],
            coverage=dict(valid_points=0,failed_markets=0),portfolios={})
        self.control=self.root/'control.json';self.control.write_bytes(study.baseline.canonical(baseline))
        period['source_report_sha256']=study.digest((self.root/'replay-report.json').read_bytes())
        period['control_report_sha256']=study.digest(self.control.read_bytes())
        self.proto=self.root/'protocol.json';self.proto.write_bytes(study.baseline.canonical(p))
        self.addCleanup(patch.stopall)
        patch.object(study,'PROTOCOL',self.proto).start()
        patch.object(study,'PINNED_PROTOCOL_SHA256',study.digest(self.proto.read_bytes())).start()
        self.baseline=patch.object(study.baseline,'run',return_value=baseline).start()

    def run_archive(self):return study.run(self.root,self.control,0,None,None)

    def test_empty_unobserved_source_not_valid_zero_profit(self):
        r=self.run_archive();self.assertEqual(study.decision([r]),'BLOCKED_DATA_QUALITY')
        self.assertFalse(r['eligible_for_live_promotion']);self.assertTrue(r['original_future_validation_unchanged'])

    def test_changed_source_or_original_full_control_stops(self):
        self.control.write_bytes(self.control.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'complete control'):self.run_archive()
        self.baseline.assert_not_called()

    def test_original_control_reproduction_mismatch_stops(self):
        self.baseline.return_value=self.baseline.return_value|{'unexpected_change':True}
        with self.assertRaisesRegex(ValueError,'does not reproduce'):self.run_archive()

    def test_protocol_tamper_stops_before_replay(self):
        self.proto.write_bytes(self.proto.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'audit changed'):self.run_archive()
        self.baseline.assert_not_called()


if __name__=='__main__':unittest.main()
