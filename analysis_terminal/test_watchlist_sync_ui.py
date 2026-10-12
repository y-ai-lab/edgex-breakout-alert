from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent


class WatchlistSyncUiTests(unittest.TestCase):
    def run_case(self, case):
        result = subprocess.run(
            ["node", str(ROOT / "test_watchlist_sync_ui.js"), str(ROOT / "index.html"), case],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_same_list_saved_status(self): self.run_case("same_list_saved_status")
    def test_local_edit_during_startup_kept(self): self.run_case("local_edit_during_startup_kept")
    def test_initial_conflict_not_overwritten(self): self.run_case("initial_conflict_not_overwritten")
    def test_existing_empty_is_conflict(self): self.run_case("existing_empty_is_conflict")
    def test_nonexistent_seed_uses_zero_revision(self): self.run_case("nonexistent_seed_uses_zero_revision")
    def test_remote_bootstrap_and_explicit_empty(self): self.run_case("remote_bootstrap_and_explicit_empty")
    def test_dirty_empty_not_recreated(self): self.run_case("dirty_empty_not_recreated")
    def test_missing_import_keeps_local_key(self): self.run_case("missing_import_keeps_local_key")
    def test_edited_during_new_key_import(self): self.run_case("edited_during_new_key_import")
    def test_serialized_latest_changes(self): self.run_case("serialized_latest_changes")
    def test_conflict_reply_preserves_local(self): self.run_case("conflict_reply_preserves_local")
    def test_write_timeout_late_reply_no_false_success(self): self.run_case("write_timeout_late_reply_no_false_success")
    def test_read_timeout_then_retry(self): self.run_case("read_timeout_then_retry")
    def test_invalid_response_not_empty_or_applied(self): self.run_case("invalid_response_not_empty_or_applied")
    def test_malformed_save_reply(self): self.run_case("malformed_save_reply")
    def test_manual_local_choice_still_checks_revision(self): self.run_case("manual_local_choice_still_checks_revision")
    def test_manual_remote_choice_refreshes_actual_data(self): self.run_case("manual_remote_choice_refreshes_actual_data")
    def test_storage_failure_not_claimed_persisted(self): self.run_case("storage_failure_not_claimed_persisted")
    def test_old_key_post_cannot_restore_new_key(self): self.run_case("old_key_post_cannot_restore_new_key")
    def test_key_in_header_and_no_error_echo(self): self.run_case("key_in_header_and_no_error_echo")
    def test_typed_key_does_not_redirect_pending_save(self): self.run_case("typed_key_does_not_redirect_pending_save")
    def test_explicit_bad_key_preserves_everything(self): self.run_case("explicit_bad_key_preserves_everything")
