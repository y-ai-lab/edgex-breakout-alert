"""Private history pagination; shipped JS with synthetic identities only."""
from pathlib import Path
import subprocess
import unittest

ROOT=Path(__file__).resolve().parent

class AccountHistoryUiTests(unittest.TestCase):
    def run_case(self,case):
        r=subprocess.run(['node',str(ROOT/'test_account_history_ui.js'),str(ROOT/'index.html'),case],capture_output=True,text=True,timeout=15)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertIn(case+': OK',r.stdout,'Async test must reach its final assertions')

    def test_valid_append(self): self.run_case('valid_append')

    def test_empty_final(self): self.run_case('empty_final')

    def test_empty_partial(self): self.run_case('empty_partial')

    def test_invalid_flags(self): self.run_case('invalid_flags')

    def test_invalid_window(self): self.run_case('invalid_window')

    def test_duplicate_within_page(self): self.run_case('duplicate_within_page')

    def test_duplicate_across_pages(self): self.run_case('duplicate_across_pages')

    def test_malformed_key_or_time(self): self.run_case('malformed_key_or_time')

    def test_stale_observation(self): self.run_case('stale_observation')

    def test_oversized_page(self): self.run_case('oversized_page')

    def test_stagnant_cursor(self): self.run_case('stagnant_cursor')

    def test_revisited_cursor(self): self.run_case('revisited_cursor')

    def test_replaced_history_ignored(self): self.run_case('replaced_history_ignored')

    def test_auth_failure_locks(self): self.run_case('auth_failure_locks')

    def test_timeout_preserves_cursor(self): self.run_case('timeout_preserves_cursor')

    def test_failure_notice_survives_balance_refresh(self): self.run_case('failure_notice_survives_balance_refresh')

    def test_initial_invalid_not_zero(self): self.run_case('initial_invalid_not_zero')

    def test_initial_load_button_released(self): self.run_case('initial_load_button_released')

    def test_duplicate_clicks(self): self.run_case('duplicate_clicks')

    def test_cumulative_cap_not_claimed_complete(self): self.run_case('cumulative_cap_not_claimed_complete')

    def test_unexpected_fields_not_copied(self): self.run_case('unexpected_fields_not_copied')

    def test_expired_cursor_reloads_overview(self): self.run_case('expired_cursor_reloads_overview')

    def test_server_failure_retries_same_cursor(self): self.run_case('server_failure_retries_same_cursor')
