import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from analysis_terminal import vwap_reclaim_schedule as schedule, vwap_reclaim_replay as study
from analysis_terminal.test_vwap_reclaim_replay import report


class VwapScheduleTests(unittest.TestCase):
    def test_no_future_data_fetched_until_both_registered_weeks_complete(self):
        p = json.loads(study.PROTOCOL.read_text());end = p['validation_periods'][-1]['end_ms']
        with tempfile.TemporaryDirectory() as d, patch.object(schedule, 'gh') as request:
            for now in (p['validation_periods'][0]['end_ms'], end-1):
                result = schedule.scheduled(Path(d), now_ms=now, head_branch='main')
                self.assertEqual(result['status'], 'WAITING_FOR_COMPLETE_REGISTERED_WEEK_PAIR')
                self.assertFalse(result['seal']);request.assert_not_called()

    def test_complete_collection_rejects_non_main_before_any_download(self):
        end = json.loads(study.PROTOCOL.read_text())['validation_periods'][-1]['end_ms']
        with patch.object(schedule, 'gh') as request, self.assertRaises(ValueError):
            schedule.scheduled(Path('/tmp/unused-vwap-output'), now_ms=end, head_branch='feature/test')
        request.assert_not_called()

    def test_first_main_artifact_retained_not_later_favorable_replay(self):
        name='experiment'
        def artifact(id, branch='main'):
            return dict(id=id,name=name,created_at=f'2026-10-{id:02}T00:00:00Z',expired=False,
                        workflow_run=dict(head_branch=branch,head_sha=str(id)))
        listing=dict(total_count=3,artifacts=[artifact(3),artifact(1),artifact(2,'feature/test')])
        self.assertEqual(schedule.first_seal(listing,name)['id'],1)
        listing['total_count']=4
        with self.assertRaises(ValueError):schedule.first_seal(listing,name)

    def test_expired_first_seal_not_recreated_from_later_prices(self):
        p=json.loads(study.PROTOCOL.read_text());a,b=p['validation_periods']
        name=f'edgex-vwap-frozen-pair-{a["start_ms"]}-{b["end_ms"]}'
        first=dict(id=1,name=name,created_at='2026-10-23T01:00:00Z',expired=True,
                   digest='sha256:original',workflow_run=dict(head_branch='main',head_sha='original'))
        with tempfile.TemporaryDirectory() as d, patch.object(schedule,'gh',return_value=dict(total_count=1,artifacts=[first])) as get:
            result=schedule.scheduled(Path(d),now_ms=b['end_ms']+study.DAY,head_branch='main')
            self.assertEqual(result['status'],'FROZEN_RESULT_EXPIRED')
            self.assertFalse(result['seal']);self.assertEqual(get.call_count,1)
            self.assertFalse(list(Path(d).iterdir()))

    def test_existing_first_seal_is_not_a_new_independent_sample(self):
        p=json.loads(study.PROTOCOL.read_text());a,b=p['validation_periods']
        name=f'edgex-vwap-frozen-pair-{a["start_ms"]}-{b["end_ms"]}'
        first=dict(id=1,name=name,created_at='2026-10-23T01:00:00Z',expired=False,
                   digest='sha256:original',workflow_run=dict(head_branch='main',head_sha='original'))
        with patch.object(schedule,'gh',return_value=dict(total_count=1,artifacts=[first])) as get:
            result=schedule.scheduled(Path('/tmp/unused-vwap-output'),now_ms=b['end_ms'],head_branch='main')
            self.assertEqual(result['status'],'FROZEN_RESULT_ALREADY_RECORDED')
            self.assertFalse(result['independent_daily_samples_added']);self.assertEqual(get.call_count,1)

    def test_artifact_hash_and_report_uniqueness_verified_without_extracting_paths(self):
        def archive(names):
            stream=BytesIO()
            with zipfile.ZipFile(stream,'w') as z:
                for name in names:z.writestr(name,'{}')
            raw=stream.getvalue();return raw,dict(digest='sha256:'+hashlib.sha256(raw).hexdigest())
        raw,meta=archive(['development.json']);self.assertEqual(schedule.development_archive(raw,meta),b'{}')
        for names in [['../development.json'],['one/development.json','two/development.json'],['missing.json']]:
            raw,meta=archive(names)
            with self.assertRaises(ValueError):schedule.development_archive(raw,meta)
        raw,meta=archive(['development.json']);meta['digest']='sha256:bad'
        with self.assertRaises(ValueError):schedule.development_archive(raw,meta)

    def test_failed_artifact_download_error_omits_secret_redirect(self):
        class Response:
            returncode=1;stdout=b'';stderr=b'https://artifact/?sig=secret'
        with patch.object(schedule.subprocess,'run',return_value=Response()):
            with self.assertRaisesRegex(RuntimeError,'Public research artifact request failed') as error:
                schedule.gh('repos/test/artifact')
            self.assertNotIn('secret',str(error.exception))

    def experiment(self, *, first_negative=False, first_gap=False, missing_pin=False):
        """Transport fixture: assert cutoffs and never access the second killed week."""
        d=report('development');d['records']=[]
        stream=BytesIO()
        with zipfile.ZipFile(stream,'w') as z:z.writestr('development.json',json.dumps(d))
        raw=stream.getvalue();digest='sha256:'+hashlib.sha256(raw).hexdigest()
        pin=dict(id=7,digest=digest,head_sha='registered')
        meta=dict(pin,expired=missing_pin,workflow_run=dict(head_sha='registered'))
        now=json.loads(study.PROTOCOL.read_text())['validation_periods'][-1]['end_ms']
        calls=[]
        async def public(end, days, output, *, universe):
            calls.append(end);self.assertEqual(days,7);output.mkdir(parents=True)
        def build(source, analyze, settings, *, role, development_path, now_ms):
            return report(role,count=20 if first_negative else 50,avg=-.1 if first_negative else .3,
                          pf=.9 if first_negative else 1.5,failure=1 if first_gap else 0)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);latest=root/'latest.json'
            latest.write_text(json.dumps(dict(pinned_development_artifact=pin,periods=[report('development')])))
            with patch.object(schedule,'LATEST',latest), patch.object(schedule,'gh',side_effect=[dict(total_count=0,artifacts=[]),meta,raw]), \
                 patch.object(study,'validation_gate') as gate, patch('analysis_terminal.run_replay.run',side_effect=public), \
                 patch.object(study,'build',side_effect=build):
                if missing_pin:
                    with self.assertRaisesRegex(ValueError,'expired or changed'):
                        schedule.scheduled(root/'out',now_ms=now,head_branch='main')
                    gate.assert_not_called();self.assertFalse(calls)
                    return None,None,calls
                result=schedule.scheduled(root/'out',now_ms=now,head_branch='main')
                gate.assert_called_once()
                review=json.loads((root/'out/decision-summary.json').read_text())
        return result,review,calls

    def test_expired_development_never_regenerated_or_future_period_opened(self):
        self.experiment(missing_pin=True)

    def test_first_negative_period_kills_and_leaves_second_week_unopened(self):
        state,review,calls=self.experiment(first_negative=True)
        self.assertEqual(state['decision'],'KILL_NO_LIVE_PROMOTION')
        self.assertEqual(len(calls),1);self.assertEqual(review['skipped_registered_periods'],1)
        self.assertTrue(state['seal']);self.assertFalse(state['changes_live_rules'])

    def test_first_quality_blocked_period_does_not_create_second_sample(self):
        state,review,calls=self.experiment(first_gap=True)
        self.assertEqual(state['decision'],'BLOCKED_DATA_QUALITY')
        self.assertEqual(len(calls),1);self.assertEqual(review['skipped_registered_periods'],1)

    def test_two_positive_weeks_only_require_live_shadow_no_aggregate_capital_roi(self):
        state,review,calls=self.experiment()
        self.assertEqual(state['decision'],'LIVE_CAPTURE_SHADOW_REQUIRED')
        self.assertEqual(len(calls),2);self.assertEqual(len(set(calls)),2)
        self.assertIsNone(review['total_portfolio_roi_pct'])
        self.assertFalse(review['eligible_for_live_promotion'])
        self.assertFalse(state['real_orders_enabled'])

    def test_failed_first_collection_sealed_with_safe_error_not_rerun(self):
        now=json.loads(study.PROTOCOL.read_text())['validation_periods'][-1]['end_ms']
        with tempfile.TemporaryDirectory() as directory, patch.object(schedule.time,'time',return_value=now/1000), \
             patch.object(schedule,'scheduled',side_effect=ValueError('private_response=secret')), \
             patch.dict(schedule.os.environ,{'GITHUB_REF_NAME':'main'}), \
             patch('sys.argv',['collector','--output',directory]), patch('sys.stdout'):
            with self.assertRaises(SystemExit):schedule.main()
            raw=(Path(directory)/'run-status.json').read_text();state=json.loads(raw)
            self.assertEqual(state['status'],'FAILED_COLLECTION');self.assertTrue(state['seal'])
            self.assertNotIn('secret',raw)


if __name__=='__main__':unittest.main()
