"""UI safety tests run the actual shipped JavaScript against a controlled clock."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class EntryFreshnessTests(unittest.TestCase):
    def test_entry_freshness_and_async_render_regressions(self):
        for case in ("boundaries", "unknown", "ageing", "expired_calculation",
                     "expiry_timer", "superseded_calculation", "heartbeat_failure", "clicks", "visibility"):
            with self.subTest(case=case):
                result = subprocess.run(
                    ["node", str(ROOT / "test_entry_freshness.js"), str(ROOT / "index.html"), case],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
