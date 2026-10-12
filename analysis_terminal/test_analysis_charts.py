from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent


class AnalysisChartTests(unittest.TestCase):
    def run_case(self, case):
        result = subprocess.run(
            ["node", str(ROOT / "test_analysis_charts.js"), str(ROOT / "index.html"), case],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_resize_and_pixel_density_keep_source_and_deadline(self): self.run_case("resize_dpr")
    def test_hidden_chart_is_rendered_when_visible(self): self.run_case("hidden_then_visible")
    def test_resize_bursts_share_one_animation_frame(self): self.run_case("coalesced_frames")
    def test_expired_data_never_returns_on_resize(self): self.run_case("expiry_never_redraws")
    def test_new_symbol_or_disconnect_discards_old_chart(self): self.run_case("superseded_and_disconnect")
    def test_tap_reads_original_ohlc_and_latest_button_resets(self): self.run_case("selection_and_latest")
    def test_keyboard_selection_and_visibility_are_scoped(self): self.run_case("keyboard_and_scope")
    def test_invalid_ohlc_and_time_are_not_drawn(self): self.run_case("invalid_data_fail_closed")
    def test_time_gap_is_not_filled_or_compressed(self): self.run_case("time_gap_kept")
    def test_invalid_levels_and_flat_prices_do_not_make_nan_chart(self): self.run_case("invalid_levels_and_flat_prices")
    def test_readout_escapes_display_strings(self): self.run_case("escaped_readout")
    def test_clearing_prevents_delayed_frame_from_resurrecting_chart(self): self.run_case("clear_queued_frame")

    def test_crowded_labels_are_separated_without_moving_price_levels(self): self.run_case("label_spacing")

    def test_pixel_density_change_rebinds_and_redraws_without_fetch(self): self.run_case("dpr_listener")
