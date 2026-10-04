"""UI safety tests run the actual shipped JavaScript against a controlled clock."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class EntryFreshnessTests(unittest.TestCase):
    def test_essential_layout_full_boot_and_api_checks(self):
        result = subprocess.run(
            ["node", str(ROOT / "test_essential_ui.js"), str(ROOT / "index.html")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_foreground_notifications_only_allow_fresh_ready(self):
        result = subprocess.run(
            ["node", str(ROOT / "test_notifications.js"), str(ROOT / "index.html")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_entry_freshness_and_async_render_regressions(self):
        for case in ("boundaries", "unknown", "ageing", "expired_calculation",
                     "expiry_timer", "superseded_calculation", "heartbeat_failure", "clicks", "visibility"):
            with self.subTest(case=case):
                result = subprocess.run(
                    ["node", str(ROOT / "test_entry_freshness.js"), str(ROOT / "index.html"), case],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
