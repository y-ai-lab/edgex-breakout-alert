from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent


class AnalysisExportUiTests(unittest.TestCase):
    def run_case(self, case):
        result = subprocess.run(
            ["node", str(ROOT / "test_analysis_export_ui.js"), str(ROOT / "index.html"), case],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_snapshot_stamp_and_ready_warning(self): self.run_case("snapshot_stamp_and_ready_warning")
    def test_nonready_and_invalid_first_target(self): self.run_case("nonready_and_invalid_first_target")
    def test_canonical_link_and_private_data(self): self.run_case("canonical_link_and_private_data")
    def test_invalid_links_not_shared(self): self.run_case("invalid_links_not_shared")
    def test_copy_success_preserves_children(self): self.run_case("copy_success_preserves_children")
    def test_denied_clipboard_selected_fallback(self): self.run_case("denied_clipboard_selected_fallback")
    def test_missing_clipboard_and_literal_text(self): self.run_case("missing_clipboard_and_literal_text")
    def test_clipboard_deadline_retry_late_completion(self): self.run_case("clipboard_deadline_retry_late_completion")
    def test_changed_symbol_no_late_fallback(self): self.run_case("changed_symbol_no_late_fallback")
    def test_stale_before_copy_no_platform_action(self): self.run_case("stale_before_copy_no_platform_action")
    def test_expiry_during_copy_no_success_or_fallback(self): self.run_case("expiry_during_copy_no_success_or_fallback")
    def test_disconnect_and_close_clear_selected_text(self): self.run_case("disconnect_and_close_clear_selected_text")
    def test_duplicate_copy_and_share_single_flight(self): self.run_case("duplicate_copy_and_share_single_flight")
    def test_native_share_snapshot_and_clean_url(self): self.run_case("native_share_snapshot_and_clean_url")
    def test_share_cancel_no_copy(self): self.run_case("share_cancel_no_copy")
    def test_unavailable_share_copies_full_link_once(self): self.run_case("unavailable_share_copies_full_link_once")
    def test_failed_share_manual_copy_fallback(self): self.run_case("failed_share_manual_copy_fallback")
    def test_failed_share_after_expiry_no_copy(self): self.run_case("failed_share_after_expiry_no_copy")
    def test_old_share_cannot_finish_new_copy(self): self.run_case("old_share_cannot_finish_new_copy")
    def test_numeric_tile_copy_preserves_value(self): self.run_case("numeric_tile_copy_preserves_value")
    def test_manual_selection_cannot_extend_freshness(self): self.run_case("manual_selection_cannot_extend_freshness")
