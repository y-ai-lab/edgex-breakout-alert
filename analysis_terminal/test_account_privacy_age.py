"""Private freshness and page lifecycle checks on the shipped JavaScript."""
from pathlib import Path
import subprocess
import unittest

ROOT=Path(__file__).resolve().parent

class AccountPrivacyAgeTests(unittest.TestCase):
    def run_case(self,case):
        r=subprocess.run(['node',str(ROOT/'test_account_privacy_age.js'),str(ROOT/'index.html'),case],capture_output=True,text=True,timeout=15)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertIn(case+': OK',r.stdout,'Async test must complete')

    def test_exact_boundary(self): self.run_case('exact_boundary')

    def test_server_age_remaining(self): self.run_case('server_age_remaining')

    def test_independent_orders_expiry(self): self.run_case('independent_orders_expiry')

    def test_session_deadline_clears_history(self): self.run_case('session_deadline_clears_history')

    def test_lock_cancels_expiry(self): self.run_case('lock_cancels_expiry')

    def test_new_snapshot_reschedules(self): self.run_case('new_snapshot_reschedules')

    def test_invalid_metadata_not_retained(self): self.run_case('invalid_metadata_not_retained')

    def test_delayed_expiry_never_refreshes_age(self): self.run_case('delayed_expiry_never_refreshes_age')

    def test_clock_reverse_locks(self): self.run_case('clock_reverse_locks')

    def test_pagehide_and_cache_restore(self): self.run_case('pagehide_and_cache_restore')

    def test_visible_resume_checks_expiry(self): self.run_case('visible_resume_checks_expiry')

    def test_expiry_cancels_inflight_read(self): self.run_case('expiry_cancels_inflight_read')

    def test_initial_load_arms_timer(self): self.run_case('initial_load_arms_timer')

    def test_order_load_arms_timer(self): self.run_case('order_load_arms_timer')

    def test_zero_polling_when_locked(self): self.run_case('zero_polling_when_locked')

    def test_clock_reverse_during_load_no_history_leak(self): self.run_case('clock_reverse_during_load_no_history_leak')
