from pathlib import Path
import subprocess
import unittest
ROOT=Path(__file__).resolve().parent
class PersonalMarketUiTests(unittest.TestCase):
    def run_case(self,case):
        result=subprocess.run(['node',str(ROOT/'test_personal_ui.js'),str(ROOT/'index.html'),case],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    def test_stale_rows_never_keep_ready_or_borrow_global_freshness(self): self.run_case('stale_list')
    def test_stale_global_summary_is_not_an_entry_count(self): self.run_case('stale_summary')
    def test_unchanged_age_tick_does_not_reset_dom(self): self.run_case('unchanged_dom')
    def test_typing_debounces_and_hides_old_filter_results(self): self.run_case('debounced_filter')
    def test_enter_and_selector_changes_work(self): self.run_case('enter_and_change')
    def test_favorite_and_analysis_buttons_use_their_own_symbol(self): self.run_case('favorite_and_analyze')
    def test_reset_returns_from_empty_favorites_to_all_markets(self): self.run_case('reset_favorites')
    def test_public_market_strings_are_escaped_in_both_layouts(self): self.run_case('escaped_rows')
