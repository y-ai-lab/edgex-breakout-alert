"""Synthetic fixtures only: do not put captured production data in CI."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import live_capture_integrity as audit


def payload():
    close = 9000000
    row = dict(key='model:setup', model='confirmed_pullback_cost_2r', setup_id='setup',
        signal_candle_ms=close-audit.STEP, created_ms=close+1,
        observed_ms=close+1000, execution_start_ms=close+audit.STEP,
        next_candle_ms=close+audit.STEP, expires_ms=close+4*audit.STEP,
        cohort_start_ms=close, frozen_source={'confirmed':True},
        trigger=100, stop=90, target=120, status='PENDING', filled_ms=None,
        mfe_r=0.0, mae_r=0.0, final_net_r=None, outcome_ms=None)
    return dict(protocol='synthetic', dataset='LIVE_CAPTURE_HYPOTHETICAL',
        real_orders_enabled=False, automatic_promotion=False,
        eligible_for_live_promotion=False, notifications_enabled=False,
        meta=dict(activated_ms=1, capture_start_ms=close, protocol_sha256='p',
            engine_sha256='e', rule_fingerprint='r', last_success_ms=close+1000),
        total_records=1,latest=[row])


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.old=payload();self.new=deepcopy(self.old)

    def reject(self, change, message):
        change(self.new)
        with self.assertRaisesRegex(ValueError,message):
            audit.compare(self.old,self.new)

    def test_forward_expiry_keeps_input_and_adds_no_sample(self):
        before=deepcopy(self.old)
        self.new['latest'][0].update(status='EXPIRED',outcome_ms=12600000,next_candle_ms=12600000)
        result=audit.compare(self.old,self.new)
        self.assertEqual(result['advanced_existing_records'],1)
        self.assertEqual(result['new_samples_added_by_audit'],0)
        self.assertEqual(self.old,before)

    def test_frozen_price_source_and_origin_rewrites_rejected(self):
        for key,value in [('trigger',101),('frozen_source',{'confirmed':False}),('observed_ms',9002000)]:
            with self.subTest(key=key):
                self.new=deepcopy(self.old)
                self.reject(lambda p:p['latest'][0].update({key:value}),'Frozen candidate')
        self.new=deepcopy(self.old)
        self.reject(lambda p:p['meta'].update(capture_start_ms=9900000),'origin')

    def test_all_terminal_outcomes_are_append_only_including_uncertain(self):
        for status in audit.TERMINAL:
            with self.subTest(status=status):
                self.old=payload();self.old['latest'][0]['status']=status
                self.new=deepcopy(self.old)
                self.reject(lambda p:p['latest'][0].update(final_net_r=2),'terminal')

    def test_valid_fill_then_existing_fill_price_cannot_change(self):
        self.new['latest'][0].update(status='OPEN',filled_ms=9900000,entry=100,
            next_candle_ms=10800000,mfe_r=.5,mae_r=.2)
        self.assertEqual(audit.compare(self.old,self.new)['newly_observed_fills'],1)
        self.old=deepcopy(self.new)
        self.reject(lambda p:p['latest'][0].update(entry=101),'fill changed')

    def test_same_bucket_duplicate_missing_and_truncated_views_rejected(self):
        changes=[(lambda p:p.update(total_records=2),'Truncated'),
                 (lambda p:p.update(latest=[],total_records=0),'disappeared'),
                 (lambda p:p['latest'].append(deepcopy(p['latest'][0])),'Truncated')]
        for change,message in changes:
            self.new=deepcopy(self.old);self.reject(change,message)
        self.new=deepcopy(self.old);self.new['latest'].append(deepcopy(self.new['latest'][0]));self.new['total_records']=2
        with self.assertRaisesRegex(ValueError,'Duplicate'):audit.compare(self.old,self.new)

    def test_confirming_and_elapsed_bar_fill_expiry_extension_rejected(self):
        for changes,message in [({'filled_ms':9000000},'fill time'),
            ({'created_ms':9000000},'close chronology'),
            ({'execution_start_ms':9000000},'Elapsed'),
            ({'expires_ms':13500000},'model expiry')]:
            self.new=deepcopy(self.old)
            self.reject(lambda p:p['latest'][0].update(changes),message)

    def test_cursor_excursion_open_reset_and_clock_regressions_rejected(self):
        self.old['latest'][0].update(status='OPEN',filled_ms=9900000,next_candle_ms=10800000,mfe_r=1)
        for changes,message in [({'next_candle_ms':9900000},'backward'),
                                ({'mfe_r':.5},'Excursion'),({'status':'PENDING'},'reset')]:
            self.new=deepcopy(self.old);self.reject(lambda p:p['latest'][0].update(changes),message)
        self.new=deepcopy(self.old)
        self.reject(lambda p:p['meta'].update(last_success_ms=9000000),'Collector clock')

    def test_later_new_capture_allowed_but_backfill_rejected(self):
        row=deepcopy(self.new['latest'][0]);row.update(key='new',setup_id='new')
        self.new['latest'].append(row);self.new['total_records']=2
        with self.assertRaisesRegex(ValueError,'backfilled'):audit.compare(self.old,self.new)
        for k in ('created_ms','signal_candle_ms','observed_ms','execution_start_ms','next_candle_ms','expires_ms'):
            row[k]+=audit.STEP
        self.assertEqual(audit.compare(self.old,self.new)['new_captured_records'],1)

    def test_scope_fingerprint_and_empty_vwap_capture(self):
        self.reject(lambda p:p.update(notifications_enabled=True),'scope')
        self.new=deepcopy(self.old)
        self.reject(lambda p:p['meta'].update(rule_fingerprint='other'),'fingerprint')
        for p in (self.old,self.new):p.update(total_records=0,latest=[]);p['meta']['rule_fingerprint']='r'
        self.assertEqual(audit.compare(self.old,self.new)['current_records'],0)

    def test_original_model_specific_two_or_four_bar_expiry(self):
        for model,bars in audit.WAIT_BARS.items():
            self.old=payload();self.old['latest'][0].update(model=model,expires_ms=9000000+bars*audit.STEP)
            self.new=deepcopy(self.old)
            self.assertEqual(audit.compare(self.old,self.new)['current_records'],1)
            self.reject(lambda p:p['latest'][0].update(expires_ms=9000000+(bars+1)*audit.STEP),
                        'model expiry')

    def test_verified_file_hash_and_time_ordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirs=[Path(tmp)/n for n in ('old','new')]
            for d,ms in zip(dirs,(1,2)):
                d.mkdir();raw=json.dumps(payload()).encode();(d/'pending.json').write_bytes(raw)
                (d/'manifest.json').write_text(json.dumps([dict(endpoint='pending',status=200,
                    fetched_ms=ms,sha256=hashlib.sha256(raw).hexdigest())]))
            self.assertEqual(audit.audit(*dirs,'pending')['status'],'MATCH')
            with self.assertRaisesRegex(ValueError,'ordered'):audit.audit(*reversed(dirs),'pending')
            (dirs[1]/'pending.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):audit.audit(*dirs,'pending')

    def test_cli_never_overwrites_an_existing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'saved.json';out.write_text('original evidence')
            with patch('sys.argv',['audit','--before','a','--after','b','--endpoint',
                                   'pending','--output',str(out)]),patch.object(audit,'audit',return_value={}):
                with self.assertRaises(FileExistsError):audit.main()
            self.assertEqual(out.read_text(),'original evidence')


if __name__=='__main__':unittest.main()
