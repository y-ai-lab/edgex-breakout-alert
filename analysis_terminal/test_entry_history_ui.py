from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent


class EntryHistoryUiTests(unittest.TestCase):
    def run_case(self, case):
        result = subprocess.run(
            ["node", str(ROOT / "test_entry_history_ui.js"), str(ROOT / "index.html"), case],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_search_debounces_and_distinguishes_display_from_total(self): self.run_case("debounced_search")
    def test_composition_and_enter_do_not_submit_partial_text(self): self.run_case("composition_and_enter")
    def test_old_filter_reply_does_not_replace_new_search(self): self.run_case("changed_filter_late_response")
    def test_more_is_single_flight_and_uses_original_cursor(self): self.run_case("pagination_single_flight")
    def test_pages_and_repeated_records_are_deduplicated_by_setup(self): self.run_case("duplicate_setups")
    def test_failed_more_keeps_existing_records_and_retries(self): self.run_case("failed_more_retains_page")
    def test_refresh_failure_keeps_same_filter_previous_records(self): self.run_case("refresh_failure_retains_page")
    def test_timeout_unlocks_retry_without_fake_zero_history(self): self.run_case("timeout_and_retry")
    def test_response_after_deadline_is_not_adopted(self): self.run_case("late_after_timeout")
    def test_invalid_counts_ids_or_cursor_cannot_replace_good_records(self): self.run_case("invalid_counts_or_cursor")
    def test_non_advancing_cursor_does_not_append_or_loop(self): self.run_case("non_advancing_cursor")
    def test_more_after_changed_filter_starts_new_query(self): self.run_case("more_after_filter_change")
    def test_reset_and_history_analysis_are_read_only_and_scoped(self): self.run_case("reset_and_scope")
    def test_proven_empty_history_and_escaped_historical_cards(self): self.run_case("empty_and_escaped_history")
