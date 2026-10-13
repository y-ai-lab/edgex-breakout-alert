"""Deadlines/locks/retry tests on the actual shipped JS, no real account access."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent


class AccountReadLifecycleTests(unittest.TestCase):
    def run_case(self, case):
        result = subprocess.run(['node', str(ROOT/'test_account_read_lifecycle.js'),
                                 str(ROOT/'index.html'), case],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_fetch_deadline(self): self.run_case('fetch_deadline')
    def test_body_deadline(self): self.run_case('body_deadline')
    def test_session_deadline(self): self.run_case('session_deadline')
    def test_delayed_timer_boundary(self): self.run_case('delayed_timer_boundary')
    def test_clock_reverse(self): self.run_case('clock_reverse')
    def test_lock_and_retry_view(self): self.run_case('lock_and_retry_view')
    def test_lock_and_retry_orders(self): self.run_case('lock_and_retry_orders')
    def test_history_timeout_preserves_cursor(self): self.run_case('history_timeout_preserves_cursor')
    def test_session_expires_before_body(self): self.run_case('session_expires_before_body')
    def test_duplicate_load_single_flight(self): self.run_case('duplicate_load_single_flight')
    def test_late_rejection_no_unhandled(self): self.run_case('late_rejection_no_unhandled')
    def test_authorization_bound_to_original_session(self): self.run_case('authorization_bound_to_original_session')
