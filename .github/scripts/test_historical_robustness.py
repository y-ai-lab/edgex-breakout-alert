"""Synthetic public archives; no live captures, credentials or network calls."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import historical_robustness as audit
from analysis_terminal import server
from analysis_terminal.test_pending_entry_replay import CONTRACT


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = audit.protocol()
        universe = [asdict(CONTRACT)]
        p['source_universe_sha256'] = audit.digest(audit.canonical(universe))
        self.protocol_path = self.root/'protocol.json'
        self.protocol_path.write_bytes(audit.canonical(p))
        self.addCleanup(patch.stopall)
        patch.object(audit,'PROTOCOL',self.protocol_path).start()
        patch.object(audit,'PINNED_PROTOCOL_SHA256',audit.digest(self.protocol_path.read_bytes())).start()
        self.p = p
        data = {'contract':asdict(CONTRACT),'HOUR_4':[],'MINUTE_15':[]}
        self.candles = self.root/'candles.json'
        self.candles.write_bytes(audit.canonical(data))
        period = p['periods'][0]
        self.source = {'dataset':'RETROSPECTIVE','eligible_for_live_promotion':False,
            'start_ms':period['start_ms'],'end_ms':period['end_ms'],
            'manifest':{'parameters':p['production_parameters'],
                'rule_fingerprint':p['production_rule_fingerprint'],
                'indicator_windows':p['indicator_windows'],'universe':universe,'failures':[],
                'sources':[{'ticker':CONTRACT.contract_name,'file':'candles.json',
                            'sha256':audit.digest(self.candles.read_bytes())}]},
            'coverage':{'expected_points':672,'valid_points':0,'expected_points_all_markets':672,
                        'failed_markets':0,'fetched_markets':1,'requested_markets':1},
            'signals':{'current':[],'shadow':[]}}

    def run_archive(self, index=0):
        (self.root/'replay-report.json').write_bytes(audit.canonical(self.source))
        return audit.run(self.root,index,server.analyze_contract,server.SETTINGS)

    def test_empty_candles_are_unobserved_not_profitable_zero_trades(self):
        before = self.candles.read_bytes()
        r = self.run_archive()
        self.assertEqual(r['metrics'][audit.study.MODEL]['filled'],0)
        self.assertIn('proposal_vs_current',r['execution_funnel'])
        self.assertIsNone(r['metrics'][audit.study.MODEL]['win_rate'])
        self.assertEqual(audit.decision([r]),'BLOCKED_DATA_QUALITY')
        self.assertEqual(self.candles.read_bytes(),before)

    def test_price_file_checksum_corruption_stops(self):
        self.candles.write_bytes(self.candles.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'checksum'):
            self.run_archive()

    def test_parent_archive_path_stops(self):
        self.source['manifest']['sources'][0]['file']='../outside.json'
        with self.assertRaisesRegex(ValueError,'outside archive'):
            self.run_archive()

    def test_missing_or_duplicate_market_inventory_stops(self):
        original = deepcopy(self.source)
        self.source['manifest']['sources']=[]
        with self.assertRaisesRegex(ValueError,'inventory'):
            self.run_archive()
        self.source=deepcopy(original)
        self.source['manifest']['sources']*=2
        with self.assertRaisesRegex(ValueError,'inventory'):
            self.run_archive()

    def test_unregistered_partial_period_and_foreign_universe_stop(self):
        original=deepcopy(self.source)
        self.source['end_ms']-=audit.study.STEP
        with self.assertRaisesRegex(ValueError,'preregistration'):
            self.run_archive()
        self.source=original
        self.source['manifest']['universe'][0]['contract_name']='CHANGEDUSDC'
        with self.assertRaisesRegex(ValueError,'preregistration'):
            self.run_archive()

    def test_pinned_protocol_change_stops_before_evaluation(self):
        self.protocol_path.write_bytes(self.protocol_path.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'protocol changed'):
            self.run_archive()

    def test_changed_frozen_dependency_stops(self):
        with patch.object(audit,'ROOT',self.root):
            with self.assertRaises(FileNotFoundError):
                self.run_archive()

    def test_exact_upstream_eof_equivalence_only_no_general_hash_bypass(self):
        for name in self.p['frozen_dependencies_sha256']:
            target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes((audit.ROOT/name).read_bytes())
        target=self.root/'app.py';raw=target.read_bytes()
        upstream=raw[:-1] if audit.digest(raw)==audit.REGISTERED_APP_SHA256 else raw
        self.assertEqual(audit.digest(upstream),audit.UPSTREAM_APP_SHA256)
        self.assertEqual(audit.digest(upstream+b'\n'),audit.REGISTERED_APP_SHA256)
        target.write_bytes(upstream)
        with patch.object(audit,'ROOT',self.root):
            self.assertEqual(self.run_archive()['source_files_verified'],1)
            target.write_bytes(upstream+b'# not the frozen module\n')
            with self.assertRaisesRegex(ValueError,'Frozen dependency changed'):
                self.run_archive()

    def test_contract_payload_changed_even_with_valid_checksum_stops(self):
        x=json.loads(self.candles.read_bytes());x['contract']['contract_name']='CHANGEDUSDC'
        self.candles.write_bytes(audit.canonical(x))
        self.source['manifest']['sources'][0]['sha256']=audit.digest(self.candles.read_bytes())
        with self.assertRaisesRegex(ValueError,'Contract inventory'):
            self.run_archive()

    def test_independent_baseline_entries_must_reproduce(self):
        self.source['signals']['current']=[dict(setup_id='bad',created_ms=1,entry=1,stop=1,target=1)]
        with self.assertRaisesRegex(ValueError,'comparator mismatch'):
            self.run_archive()

    def test_source_coverage_counts_must_reproduce(self):
        self.source['coverage']['valid_points']=1
        with self.assertRaisesRegex(ValueError,'Coverage'):
            self.run_archive()

    def test_invalid_period_index_and_reserved_interval_rejected(self):
        for index in (-1,2,True):
            with self.assertRaisesRegex(ValueError,'Unregistered period'):
                self.run_archive(index)
        p=deepcopy(self.p)
        p['periods'][0]['start_ms']=p['sealed_periods'][0]['start_ms']
        p['periods'][0]['end_ms']=p['sealed_periods'][0]['end_ms']
        self.protocol_path.write_bytes(audit.canonical(p))
        with patch.object(audit,'PINNED_PROTOCOL_SHA256',audit.digest(self.protocol_path.read_bytes())):
            with self.assertRaisesRegex(ValueError,'Reserved evaluation'):
                self.run_archive()


def report(index=0, resolved=50):
    p=audit.protocol();period=p['periods'][index]
    m=dict(resolved=resolved,win_rate=50,avg_net_r=.5,profit_factor=2)
    return dict(role=period['id'],start_ms=period['start_ms'],end_ms=period['end_ms'],
        audit_protocol_sha256=audit.digest(audit.PROTOCOL.read_bytes()),period_complete=True,
        coverage={'failed_markets':0,'valid_points':100},
        metrics={audit.study.MODEL:m},stress_metrics={audit.study.MODEL:deepcopy(m)},
        portfolios={audit.study.MODEL:dict(resolved=20,active=0,uncertain=0,closed_portfolio_roi_pct=1)},
        execution_funnel={'proposal_vs_current':dict(net_filled_count_difference=5,capped_filled_count_difference=2)})


class SelectionTests(unittest.TestCase):
    def test_two_registered_complete_positive_periods_still_require_live_validation(self):
        self.assertEqual(audit.decision([report(),report(1)]),
                         'RETROSPECTIVE_CRITERIA_MET_SEPARATE_LIVE_VALIDATION_REQUIRED')

    def test_duplicate_periods_cannot_increase_samples(self):
        with self.assertRaisesRegex(ValueError,'duplicate'):
            audit.decision([report(),report()])

    def test_forty_plus_forty_cannot_satisfy_fifty_each(self):
        self.assertEqual(audit.decision([report(resolved=40),report(1,resolved=40)]),
                         'CONTINUE_INSUFFICIENT_SAMPLE')

    def test_partial_week_and_missing_other_period_not_qualified(self):
        a=report();a['period_complete']=False
        self.assertEqual(audit.decision([a,report(1)]),'CONTINUE_INSUFFICIENT_SAMPLE')
        a['metrics'][audit.study.MODEL].update(avg_net_r=-.1,profit_factor=.8)
        self.assertEqual(audit.decision([a,report(1)]),'CONTINUE_INSUFFICIENT_SAMPLE')
        self.assertEqual(audit.decision([report()]),'CONTINUE_INSUFFICIENT_SAMPLE')

    def test_large_negative_period_is_rejected_without_changing_live_rules(self):
        a=report(resolved=20);a['metrics'][audit.study.MODEL].update(avg_net_r=-.1,profit_factor=.8)
        self.assertEqual(audit.decision([a]),'REJECTED_HISTORICAL_ROBUSTNESS_NO_LIVE_CHANGE')
        a=report(resolved=19);a['metrics'][audit.study.MODEL].update(avg_net_r=-.1,profit_factor=.8)
        self.assertEqual(audit.decision([a]),'CONTINUE_INSUFFICIENT_SAMPLE')

    def test_positive_means_do_not_override_roi_fills_costs_or_quality(self):
        for kind in ('open','uncertain','roi','capped_fills','pure_fills','stress','win_rate','pf'):
            with self.subTest(kind=kind):
                a=report();account=a['portfolios'][audit.study.MODEL]
                if kind in ('open','uncertain'):account['active' if kind=='open' else 'uncertain']=1
                if kind=='roi':account['closed_portfolio_roi_pct']=None
                if kind=='capped_fills':a['execution_funnel']['proposal_vs_current']['capped_filled_count_difference']=0
                if kind=='pure_fills':a['execution_funnel']['proposal_vs_current']['net_filled_count_difference']=0
                if kind=='stress':a['stress_metrics'][audit.study.MODEL]['avg_net_r']=0
                if kind=='win_rate':a['metrics'][audit.study.MODEL]['win_rate']=39
                if kind=='pf':a['metrics'][audit.study.MODEL]['profit_factor']=1.19
                self.assertEqual(audit.decision([a,report(1)]),'NO_QUALIFIED_REPLACEMENT')
        a=report();a['coverage']['failed_markets']=1
        self.assertEqual(audit.decision([a,report(1)]),'BLOCKED_DATA_QUALITY')


if __name__ == '__main__':
    unittest.main()
