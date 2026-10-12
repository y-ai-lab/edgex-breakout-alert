from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent


class MarketConnectionTests(unittest.TestCase):
    def run_case(self, case):
        result = subprocess.run(
            ["node", str(ROOT / "test_market_connection.js"), str(ROOT / "index.html"), case],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_success_keeps_original_120_second_deadline(self): self.run_case("success_age")
    def test_failure_suppresses_recent_entry_and_analysis(self): self.run_case("failure_suppresses_fresh")
    def test_offline_does_not_request_or_show_zero_candidates(self): self.run_case("offline_no_http")
    def test_late_response_after_disconnect_cannot_restore_ready(self): self.run_case("late_response_never_restores")
    def test_recovered_global_entry_does_not_refresh_old_filtered_list(self): self.run_case("old_filtered_snapshot_stays_stale")
    def test_reconnect_queue_preserves_explicit_force(self): self.run_case("queued_reconnect_preserves_force")
    def test_reconnect_uses_existing_filtered_scanner(self): self.run_case("retry_uses_current_filters")
    def test_timeout_recovers_without_fabricating_snapshot_age(self): self.run_case("timeout_recovery_and_original_age")
    def test_invalid_age_never_means_connected_data(self): self.run_case("invalid_age_not_connected")
    def test_browser_bindings_retry_and_four_tabs(self): self.run_case("bindings_and_scope")
