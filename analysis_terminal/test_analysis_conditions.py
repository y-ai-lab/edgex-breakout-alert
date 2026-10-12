from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent


class AnalysisConditionsTests(unittest.TestCase):
    def run_case(self, case):
        result = subprocess.run(
            ["node", str(ROOT / "test_analysis_conditions.js"), str(ROOT / "index.html"), case],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_long_short_and_unchanged_source(self): self.run_case("long_short")
    def test_confirmation_and_rr_failures_remain_independent(self): self.run_case("independent_failures")
    def test_missing_or_conflicting_confirmation_is_unknown(self): self.run_case("unknown_confirmation")
    def test_invalid_structure_is_visible_before_structure_stage(self): self.run_case("invalid_structure")
    def test_unreached_stages_do_not_infer_success(self): self.run_case("early_stages")
    def test_current_minimum_and_finite_numbers_only(self): self.run_case("minimum_and_numbers")
    def test_condition_count_does_not_promote_and_expires(self): self.run_case("render_and_expiry")
    def test_public_data_is_escaped(self): self.run_case("escaping")
    def test_favorite_only_uses_fresh_analyzed_symbol(self): self.run_case("favorite_scope")
