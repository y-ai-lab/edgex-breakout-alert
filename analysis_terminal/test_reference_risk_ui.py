from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent


class ReferenceRiskUiTests(unittest.TestCase):
    def run_case(self, case):
        result = subprocess.run(
            ["node", str(ROOT / "test_reference_risk_ui.js"), str(ROOT / "index.html"), case],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_required_fields(self): self.run_case("required_fields")
    def test_risk_cap(self): self.run_case("risk_cap")
    def test_long_short_targets(self): self.run_case("long_short_targets")
    def test_equal_stop(self): self.run_case("equal_stop")
    def test_optional_fields(self): self.run_case("optional_fields")
    def test_order_limits(self): self.run_case("order_limits")
    def test_enter_and_composition(self): self.run_case("enter_and_composition")
    def test_same_input_single_flight(self): self.run_case("same_input_single_flight")
    def test_input_change_discards_reply(self): self.run_case("input_change_discards_reply")
    def test_programmatic_change_discards_reply(self): self.run_case("programmatic_change_discards_reply")
    def test_old_finally_keeps_new_busy(self): self.run_case("old_finally_keeps_new_busy")
    def test_timeout_retry_late_reply(self): self.run_case("timeout_retry_late_reply")
    def test_failed_request_retry(self): self.run_case("failed_request_retry")
    def test_invalid_results(self): self.run_case("invalid_results")
    def test_source_or_capital_change(self): self.run_case("source_or_capital_change")
    def test_clear_analysis_invalidates_calculation(self): self.run_case("clear_analysis_invalidates_calculation")
    def test_decimal_strings_and_negative_net_target(self): self.run_case("decimal_strings_and_negative_net_target")
    def test_error_cleared_after_edit(self): self.run_case("error_cleared_after_edit")
