"""Retained results cannot become new samples, repaired exits or remote artifacts."""
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile

import retained_cohort_audit as audit
from analysis_terminal import pending_followup as follow, pending_entry_replay as study
from analysis_terminal.test_pending_followup import fixture
from analysis_terminal.test_pending_entry_replay import candle, STEP


def finished(side='LONG', status='TP', original_status='OPEN'):
    base, source = fixture(status=original_status, side=side)
    start = base['end_ms']
    p = dict(cohort_end_ms=start, retained_end_ms=start+STEP, fixed_cutoff_ms=start+7*follow.DAY)
    if status == 'DATA_GAP':
        cs = {}
    else:
        prices = dict(open=110, low=109, high=130, close=120) if side=='LONG' else dict(open=90, high=91, low=70, close=80)
        if status == 'AMBIGUOUS': prices=dict(open=100, high=140, low=60, close=100)
        if status == 'OPEN': prices=dict(open=100, high=101, low=99, close=100)
        cs = {'TESTUSDC':[candle(start, **prices)]}
    saved = follow.extend(base, source, cs, end_ms=p['retained_end_ms'])
    return base, source, saved, cs, p


def zip_bytes(items):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        for name, raw in items: z.writestr(name, raw)
    return out.getvalue()


class HorizonTests(unittest.TestCase):
    def test_long_and_short_closed_exits_need_no_new_prices_or_money_changes(self):
        for side in ['LONG','SHORT']:
            with self.subTest(side=side):
                base,source,saved,cs,p=finished(side=side)
                original=deepcopy((base,source,saved,cs))
                result=audit.finish(base,source,saved,cs,p,now_ms=p['fixed_cutoff_ms'])
                final=result['fixed_horizon_report']
                self.assertEqual(final['records'],saved['records'])
                self.assertEqual(final['portfolios'],saved['portfolios'])
                self.assertEqual(final['metrics'],saved['metrics'])
                self.assertTrue(final['followup_complete'])
                self.assertFalse(result['github_final_outcome_artifact_available'])
                self.assertFalse(result['latest_missing_artifact_recovered'])
                self.assertFalse(result['new_independent_samples_added'])
                self.assertEqual((base,source,saved,cs),original)

    def test_open_cannot_be_expired_or_assumed_as_a_later_winner(self):
        base,source,saved,cs,p=finished(status='OPEN')
        with self.assertRaisesRegex(ValueError,'OPEN requires'):
            audit.finish(base,source,saved,cs,p,now_ms=p['fixed_cutoff_ms'])
        self.assertEqual(saved['records'][0]['status'],'OPEN')

    def test_pending_is_censored_and_keeps_its_original_reservation(self):
        base,source,saved,cs,p=finished(original_status='PENDING')
        result=audit.finish(base,source,saved,cs,p,now_ms=p['fixed_cutoff_ms'])
        self.assertEqual(result['fixed_horizon_report']['records'],base['records'])
        self.assertEqual(result['fixed_horizon_report']['portfolios'],saved['portfolios'])
        self.assertEqual(result['fixed_horizon_report']['censored_unfilled_pending'],1)
        self.assertIsNone(result['fixed_horizon_report']['portfolios'][study.MODEL]['closed_portfolio_roi_pct'])

    def test_ambiguous_and_missing_bar_never_release_uncertain_money(self):
        for status in ['AMBIGUOUS','DATA_GAP']:
            with self.subTest(status=status):
                base,source,saved,cs,p=finished(status=status)
                result=audit.finish(base,source,saved,cs,p,now_ms=p['fixed_cutoff_ms'])
                self.assertEqual(result['status'],'FIXED_HORIZON_AUDITED_WITH_UNCERTAINTY')
                final=result['fixed_horizon_report']
                self.assertEqual(final['records'][0]['status'],status)
                self.assertEqual(final['portfolios'],saved['portfolios'])
                self.assertEqual(final['portfolios'][study.MODEL]['uncertain'],1)
                self.assertIsNone(final['portfolios'][study.MODEL]['closed_portfolio_roi_pct'])

    def test_before_cutoff_and_later_unregistered_extension_are_rejected(self):
        base,source,saved,cs,p=finished()
        with self.assertRaises(ValueError):audit.finish(base,source,saved,cs,p,now_ms=p['fixed_cutoff_ms']-1)
        p['fixed_cutoff_ms']+=STEP
        with self.assertRaises(ValueError):audit.finish(base,source,saved,cs,p,now_ms=p['fixed_cutoff_ms'])

    def test_changed_terminal_result_and_record_identity_are_rejected(self):
        base,source,saved,cs,p=finished(original_status='SL')
        saved['records'][0]['entry']*=1.01
        with self.assertRaises(ValueError):audit.finish(base,source,saved,cs,p,now_ms=p['fixed_cutoff_ms'])
        base,source,saved,cs,p=finished();saved['records'][0]['key']='replacement'
        with self.assertRaises(ValueError):audit.finish(base,source,saved,cs,p,now_ms=p['fixed_cutoff_ms'])

    def test_changed_metric_or_cash_cannot_be_repaired_to_match_a_good_value(self):
        for key in ['metrics','portfolios']:
            base,source,saved,cs,p=finished()
            if key=='metrics':saved[key][study.MODEL]['avg_net_r']=99
            else:saved[key][study.MODEL]['known_cash_usdc']+=100
            with self.assertRaisesRegex(ValueError,'changed frozen outcomes'):
                audit.finish(base,source,saved,cs,p,now_ms=p['fixed_cutoff_ms'])


class ArchiveTests(unittest.TestCase):
    def check(self, raw, expected=None):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'original.zip';path.write_bytes(raw)
            return audit.archive(path,expected or audit.digest(raw))

    def test_cli_imports_repository_from_an_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,str(Path(audit.__file__).resolve()),'--help'],cwd=directory,capture_output=True,text=True,check=True)
            self.assertIn('--frozen',result.stdout)

    def test_archive_hash_is_required_before_reading_members(self):
        raw=zip_bytes([('report.json',b'original')])
        self.assertEqual(self.check(raw),{'report.json':b'original'})
        with self.assertRaisesRegex(ValueError,'checksum'):self.check(raw,'0'*64)

    def test_paths_and_duplicate_members_are_rejected(self):
        for name in ['../escape','/absolute','source/../../escape','source\\escape']:
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'Unsafe'):
                self.check(zip_bytes([(name,b'value')]))
        with self.assertWarns(UserWarning):raw=zip_bytes([('same',b'1'),('same',b'2')])
        with self.assertRaisesRegex(ValueError,'Duplicate archive'):self.check(raw)

    def test_duplicate_json_keys_and_nonfinite_values_are_rejected(self):
        for raw in [b'{"a":1,"a":2}',b'{"a":NaN}',b'{"a":Infinity}']:
            with self.assertRaises(ValueError):audit.decode(raw)

    def test_registered_protocol_and_every_original_dependency_stay_pinned(self):
        p=audit.protocol();self.assertTrue(p['no_network'])
        self.assertEqual(p['fixed_cutoff_ms'],p['cohort_end_ms']+7*follow.DAY)
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'protocol.json';target.write_bytes(audit.PROTOCOL.read_bytes()+b' ')
            with patch.object(audit,'PROTOCOL',target),self.assertRaisesRegex(ValueError,'protocol changed'):
                audit.protocol()

    def test_existing_output_is_never_overwritten_or_read_as_an_input(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory);(out/'proof').write_text('retained')
            with self.assertRaisesRegex(ValueError,'already exists'):
                audit.run('missing','missing','missing',out)
            self.assertEqual((out/'proof').read_text(),'retained')

    def test_retained_followup_hash_mismatch_stops_before_candle_use(self):
        with self.assertRaisesRegex(ValueError,'checksum'):
            audit.retained({'followup/followup-report.json':b'{}'}, {}, {}, {'retained_followup_report_sha256':'0'*64})

    def test_original_seal_mismatch_stops_before_baseline_replay(self):
        files={'report.json':b'{}','source.json':b'{}','seal.json':b'{}'}
        p={'original_report_sha256':'0'*64,'original_source_sha256':'0'*64}
        with patch.object(study,'run') as replay,self.assertRaisesRegex(ValueError,'seal/source'):
            audit.original(files,{},p,None,None)
        replay.assert_not_called()


if __name__=='__main__':unittest.main()
