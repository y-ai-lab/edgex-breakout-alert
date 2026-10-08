"""Run the actual CI guard: unrelated UI/auth changes pass, research drift fails."""
from dataclasses import replace
import json
from pathlib import Path
import re
import textwrap
import unittest
from unittest.mock import patch

from analysis_terminal import server, vwap_reclaim_schedule as schedule

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github/workflows/vwap-reclaim-research.yml'
PROTOCOL = ROOT / 'analysis_terminal/vwap_reclaim_protocol.json'
PROVENANCE = ('published_control_ledger_sha256',
              'control_content_except_engine_provenance_sha256', 'provenance_note')


def guard_source():
    match = re.search(
        r'- name: Registered research rules and comparators remain frozen\n'
        r"\s+run: \|\n\s+python - <<'PY'\n(.*?)\n          PY",
        WORKFLOW.read_text(), re.S)
    if not match:
        raise AssertionError('Workflow guard missing')
    return textwrap.dedent(match[1])


class WorkflowGuardTests(unittest.TestCase):
    def run_guard(self, *, changed_baseline=None, changed_protocol=False, revived=False):
        original = json.loads(PROTOCOL.read_text())
        for key in PROVENANCE:
            original['development'].pop(key)
        if changed_protocol:
            original['validation_periods'][0]['end_ms'] += 900000
        real_read_text = Path.read_text

        def git_show(command):
            self.assertEqual(command[:2], ['git', 'show'])
            ref, name = command[2].split(':', 1)
            if ref == '2782ecccc3be271b7e585e3bee6061493d8b6119':
                self.assertEqual(name, 'analysis_terminal/vwap_reclaim_protocol.json')
                return json.dumps(original).encode()
            self.assertEqual(ref, '4807e869a2563695cc377110edc7e5b90b38aa0a')
            # These already differ from their old web/auth versions. An old
            # whole-service check must fail; the research guard must not read them.
            if name in ('analysis_terminal/server.py', 'analysis_terminal/index.html',
                        'analysis_terminal/live_execution.py', 'analysis_terminal/edgex_orders.py'):
                return b'old web/auth implementation'
            content = (ROOT / name).read_bytes()
            return content + b'changed frozen input' if name == changed_baseline else content

        def read_text(path, *args, **kwargs):
            value = real_read_text(path, *args, **kwargs)
            if revived and path.name == 'break_retest_latest.json':
                record = json.loads(value)
                record['forward_collection_enabled'] = True
                return json.dumps(record)
            return value

        source = guard_source()
        with patch('subprocess.check_output', side_effect=git_show), \
                patch.object(Path, 'read_text', read_text):
            exec(compile(source, str(WORKFLOW), 'exec'), {})

    def test_current_strategy_accepts_updated_web_and_auth_code(self):
        self.run_guard()

    def test_changed_frozen_evaluator_and_research_workflow_fail(self):
        for name in ('analysis_terminal/pending_entry_replay.py',
                     'analysis_terminal/pending_live_protocol.json',
                     '.github/workflows/strategy-research.yml', 'app.py'):
            with self.subTest(name=name), self.assertRaises(AssertionError):
                self.run_guard(changed_baseline=name)

    def test_changed_confirmation_or_rr_parameters_fail(self):
        with patch.object(server, 'SETTINGS', replace(server.SETTINGS, min_rr=1.5)):
            with self.assertRaisesRegex(ValueError, 'Frozen production comparator changed'):
                self.run_guard()

    def test_changed_analyzer_fails_before_archive_download(self):
        with patch.object(server, 'analyze_contract', schedule.first_seal):
            with self.assertRaisesRegex(ValueError, 'Frozen production comparator changed'):
                self.run_guard()

    def test_changed_registered_period_fails(self):
        with self.assertRaisesRegex(AssertionError, 'Registered strategy or periods changed'):
            self.run_guard(changed_protocol=True)

    def test_killed_strategy_cannot_be_reactivated(self):
        with self.assertRaises(AssertionError):
            self.run_guard(revived=True)


if __name__ == '__main__':
    unittest.main()
