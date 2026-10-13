"""Frozen source reproduction, corrupt diagnostics and missing evidence checks."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import entry_price_audit as audit
from analysis_terminal import pending_fill_diagnostics as prices
from analysis_terminal.replay import rule_fingerprint, strategy_parameters
from analysis_terminal.test_execution_funnel import report
from analysis_terminal.test_pending_entry_replay import CONTRACT, START, STEP, candle, row
from analysis_terminal.test_pending_fill_diagnostics import expired,narrow_candidate
from analysis_terminal.test_storage import server


def analyze(*args, **kwargs):
    return row()


def narrow_analyze(*args, **kwargs):
    return narrow_candidate('SHORT')[1]


def fixture(root, *, narrow=False):
    source=root/'source';source.mkdir();(root/'review').mkdir()
    r,_=expired()
    frames={'MINUTE_15':[asdict(candle(START+i*STEP)) for i in range(-180,8)],
            'HOUR_4':[asdict(candle(START+i*16*STEP)) | {'interval':'HOUR_4'} for i in range(-180,0)]}
    analyze_fn=analyze
    if narrow:
        from analysis_terminal import pending_entry_replay as study
        r,_=narrow_candidate('SHORT')
        r.update(step_size=.01,min_order_size=.01,max_order_size=1000)
        for c in frames['MINUTE_15']:
            c.update(open=99.9,low=99.8,high=99.95,close=99.9)
        r=study.evaluate(r,[candle(START+i*STEP,open=99.9,low=99.8,high=99.95,close=99.9)
                            for i in range(4)],{START+i*STEP:r['setup_id'] for i in range(4)},
                         end_ms=START+8*STEP)
        analyze_fn=narrow_analyze
    data=source/'candles.json'
    data.write_text(json.dumps(dict(contract=asdict(CONTRACT),**frames)))
    fingerprint=rule_fingerprint(analyze_fn,server.SETTINGS)
    ledger=report([r],end=START+8*STEP)
    ledger.update(source_rule_fingerprint=fingerprint,production_rule_fingerprint=fingerprint)
    manifest=dict(dataset='RETROSPECTIVE',eligible_for_live_promotion=False,
                  start_ms=START,end_ms=START+8*STEP,
                  manifest=dict(rule_fingerprint=fingerprint,parameters=strategy_parameters(server.SETTINGS),
                                sources=[dict(ticker=CONTRACT.contract_name,file='candles.json',
                                              sha256=hashlib.sha256(data.read_bytes()).hexdigest())]))
    (source/'replay-report.json').write_text(json.dumps(manifest))
    ledger_path=root/'review/pending-entry-report.json'
    ledger_path.write_text(json.dumps(ledger))
    saved=prices.build(source,ledger_path,analyze_fn,server.SETTINGS)
    path=root/'review/pending-fill-diagnostics.json'
    path.write_text(json.dumps(saved))
    return path,saved


class EntryPriceAuditTests(unittest.TestCase):
    def test_supported_frozen_candidate_with_blocked_nominal_geometry_is_disclosed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path,saved=fixture(root,narrow=True)
            before={p:p.read_bytes() for p in root.rglob('*.json')}
            result=audit.summarize(root,analyze=narrow_analyze,settings=server.SETTINGS)
            self.assertEqual(result['status'],'VERIFIED_SOURCES_WITH_BLOCKED_PRICE_DIAGNOSTICS')
            self.assertEqual(result['summary']['blocked_price_geometry_candidates'],1)
            self.assertEqual(result['original_candidates_reproduced'],1)
            self.assertIsNone(result['summary']['expired_closest_distance_median_r'])
            self.assertFalse(result['new_samples_added']);self.assertFalse(result['actual_execution_evidence'])
            for p,raw in before.items():self.assertEqual(p.read_bytes(),raw)
            changed=deepcopy(saved);changed['summary']['blocked_price_geometry_candidates']=0
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError,'do not reproduce'):
                audit.summarize(root,analyze=narrow_analyze,settings=server.SETTINGS)

    def test_actual_reproduction_keeps_inputs_unchanged_and_exports_only_aggregates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path,saved=fixture(root)
            before={p:p.read_bytes() for p in root.rglob('*.json')}
            result=audit.summarize(root,analyze=analyze,settings=server.SETTINGS)
            self.assertEqual(result['status'],'VERIFIED_FROM_FROZEN_PUBLIC_SOURCES')
            self.assertEqual(result['summary'],saved['summary'])
            self.assertEqual(result['original_candidates_reproduced'],1)
            self.assertEqual(result['diagnostics_sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertNotIn('records',result)
            self.assertNotIn(CONTRACT.contract_name,json.dumps(result))
            self.assertFalse(result['new_samples_added']);self.assertFalse(result['actual_execution_evidence'])
            for key in ('counterfactual_fills','counterfactual_win_rate','counterfactual_avg_r','counterfactual_portfolio_roi_pct'):
                self.assertIsNone(result['summary'][key])
            for p,raw in before.items():self.assertEqual(p.read_bytes(),raw)

    def test_missing_diagnostics_is_unknown_instead_of_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=audit.summarize(Path(tmp))
            self.assertEqual(result['status'],'NOT_RECORDED')
            self.assertIsNone(result['summary'])

    def test_changed_saved_summary_records_or_provenance_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path,saved=fixture(root)
            for kind in ('summary','duplicate','record','clock','manifest','roi'):
                changed=deepcopy(saved)
                if kind=='summary':changed['summary']['late_price_touches']+=1
                elif kind=='duplicate':changed['records'].append(deepcopy(changed['records'][0]))
                elif kind=='record':changed['records'][0]['target_behind_signal_close']=True
                elif kind=='clock':changed['end_ms']+=STEP
                elif kind=='manifest':changed['source_manifest_sha256']='changed'
                else:changed['summary']['counterfactual_portfolio_roi_pct']=1
                path.write_text(json.dumps(changed))
                with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'do not reproduce'):
                    audit.summarize(root,analyze=analyze,settings=server.SETTINGS)

    def test_source_hash_ledger_geometry_and_live_dataset_cannot_be_substituted(self):
        for kind in ('hash','trigger','live'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);fixture(root)
                if kind=='hash':
                    path=root/'source/candles.json';path.write_text(path.read_text()+' ')
                else:
                    path=root/'review/pending-entry-report.json';ledger=json.loads(path.read_text())
                    if kind=='trigger':ledger['records'][0]['trigger']+=.1
                    else:ledger['dataset']='LIVE_CAPTURE_HYPOTHETICAL'
                    path.write_text(json.dumps(ledger))
                with self.assertRaises(ValueError):audit.summarize(root,analyze=analyze,settings=server.SETTINGS)
