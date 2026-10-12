from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent


class PushReadinessUiTests(unittest.TestCase):
    def run_case(self, case):
        result = subprocess.run(
            ["node", str(ROOT / "test_push_readiness_ui.js"), str(ROOT / "index.html"), case],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fully_ready_not_delivery_proof(self): self.run_case("fully_ready_not_delivery_proof")
    def test_ios_install_and_unsupported(self): self.run_case("ios_install_and_unsupported")
    def test_permissions_and_revocation(self): self.run_case("permissions_and_revocation")
    def test_no_local_subscription(self): self.run_case("no_local_subscription")
    def test_server_disabled(self): self.run_case("server_disabled")
    def test_preferences_off(self): self.run_case("preferences_off")
    def test_snooze_expiry(self): self.run_case("snooze_expiry")
    def test_quiet_overnight_boundaries(self): self.run_case("quiet_overnight_boundaries")
    def test_quiet_day_timezone(self): self.run_case("quiet_day_timezone")
    def test_quiet_disabled_and_partial(self): self.run_case("quiet_disabled_and_partial")
    def test_invalid_quiet_unknown(self): self.run_case("invalid_quiet_unknown")
    def test_invalid_preferences(self): self.run_case("invalid_preferences")
    def test_freshness_and_clock_reversal(self): self.run_case("freshness_and_clock_reversal")
    def test_unknown_preferences_not_off(self): self.run_case("unknown_preferences_not_off")
    def test_worker_timeout_and_late_subscription(self): self.run_case("worker_timeout_and_late_subscription")
    def test_preference_timeout_and_retry(self): self.run_case("preference_timeout_and_retry")
    def test_superseded_response(self): self.run_case("superseded_response")
    def test_offline_no_requests(self): self.run_case("offline_no_requests")
    def test_read_failure_and_invalid_config(self): self.run_case("read_failure_and_invalid_config")
    def test_settings_invalidation(self): self.run_case("settings_invalidation")
    def test_privacy_and_escaping(self): self.run_case("privacy_and_escaping")
    def test_bindings_and_scope(self): self.run_case("bindings_and_scope")
    def test_unsaved_quiet_draft_preserved(self): self.run_case("unsaved_quiet_draft_preserved")
    def test_quiet_edit_during_check(self): self.run_case("quiet_edit_during_check")
