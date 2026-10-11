"""Terminal interactions must not resurrect stale prices or trigger orders."""
from pathlib import Path
import subprocess
import unittest
ROOT = Path(__file__).resolve().parent
class ProductUiTests(unittest.TestCase):
    def run_case(self, case):
        result = subprocess.run(['node', str(ROOT/'test_product_ui.js'), str(ROOT/'index.html'), case], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
    def test_newest_requested_symbol_wins(self): self.run_case('out_of_order')
    def test_typing_invalidates_previous_symbol(self): self.run_case('input_change')
    def test_failure_clears_prices_and_retry_recovers(self): self.run_case('failure_recovery')
    def test_malformed_or_stale_analysis_is_not_a_plan(self): self.run_case('invalid_response')
    def test_copy_expires_at_120_seconds(self): self.run_case('expiry_copy')
    def test_candle_age_can_expire_before_view_age(self): self.run_case('candle_age')
    def test_timeout_unlocks_retry(self): self.run_case('timeout')
    def test_delayed_quantity_cannot_repopulate_cleared_analysis(self): self.run_case('late_risk')
    def test_market_context_uses_counts_and_hides_stale_or_unknown(self): self.run_case('market_context')
