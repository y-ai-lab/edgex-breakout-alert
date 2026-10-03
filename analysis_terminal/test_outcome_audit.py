from copy import deepcopy
from dataclasses import replace
import unittest

from analysis_terminal import test_replay as fixture
from analysis_terminal.outcome_audit import audit_signal, audit_summary
from analysis_terminal.outcomes import evaluate_paper_signal
from analysis_terminal.setups import setup_identity

STEP, START, CONTRACT = fixture.STEP, fixture.START, fixture.CONTRACT


def signal():
    s = dict(key="test", ticker=CONTRACT.contract_name, side="LONG", entry=100, stop=90, target=120,
             breakout_time_ms=START-2*fixture.MONITOR, breakout_level=99,
             signal_candle_ms=START-STEP, created_ms=START+1)
    s["setup_id"] = setup_identity(dict(s, direction=s["side"]))
    return s


def evaluated(rows):
    s = signal()
    s["result"] = evaluate_paper_signal(s, rows, interval_ms=STEP, now_ms=START+3*STEP)
    return s


class OutcomeAuditTests(unittest.TestCase):
    def test_full_terminal_recalculation_does_not_trust_or_mutate_saved_result(self):
        rows = [fixture.c(START, high=121)]
        s = evaluated(rows);before = deepcopy(s)
        self.assertEqual(audit_signal(s, rows, CONTRACT)["status"], "MATCH")
        s["result"]["status"] = "SL";s["result"]["final_r"] = -1
        audited = audit_signal(s, rows, CONTRACT)
        self.assertEqual(audited["status"], "MISMATCH")
        self.assertEqual(audited["recomputed"]["status"], "TP")
        self.assertIn("status", audited["differences"])
        self.assertEqual(s["result"]["status"], "SL")
        self.assertEqual(before["entry"], s["entry"])

    def test_open_is_a_valid_audited_state_not_a_resolved_sample(self):
        rows = [fixture.c(START), fixture.c(START+STEP)]
        audited = audit_signal(evaluated(rows), rows, CONTRACT)
        self.assertEqual((audited["status"], audited["recomputed"]["status"]), ("MATCH", "OPEN"))
        self.assertIsNone(audited["recomputed"]["final_r"])

    def test_signal_prior_and_future_candles_cannot_affect_comparison(self):
        rows = [fixture.c(START), fixture.c(START+STEP)]
        s = evaluated(rows)
        excluded = [fixture.c(START-STEP, high=130, low=80), fixture.c(START+2*STEP, high=130, low=80)]
        self.assertEqual(audit_signal(s, rows+excluded, CONTRACT)["status"], "MATCH")
        self.assertEqual(audit_signal(s, rows+excluded, CONTRACT)["recomputed"]["candles_checked"], 2)

    def test_history_gap_never_resolves_on_a_later_tp(self):
        rows = [fixture.c(START), fixture.c(START+STEP), fixture.c(START+2*STEP)]
        s = evaluated(rows)
        audited = audit_signal(s, [rows[0], replace(rows[2], high=125)], CONTRACT)
        self.assertEqual(audited["status"], "HISTORY_GAP")
        self.assertEqual(audited["first_gap_ms"], START+STEP)
        self.assertEqual(audited["recomputed"]["status"], "OPEN")

    def test_same_bar_ambiguity_and_excursion_differences_are_visible(self):
        rows = [fixture.c(START, high=125, low=85)]
        s = evaluated(rows)
        audited = audit_signal(s, rows, CONTRACT)
        self.assertEqual(audited["status"], "MATCH")
        self.assertEqual(audited["recomputed"]["status"], "AMBIGUOUS")
        s["result"]["mfe_r"] = 0
        self.assertIn("mfe_r", audit_signal(s, rows, CONTRACT)["differences"])

    def test_no_legacy_identity_inference_or_partial_entry_rounding(self):
        rows = [fixture.c(START)]
        s = evaluated(rows)
        self.assertEqual(audit_signal(dict(s, setup_id=None), rows, CONTRACT)["status"], "INVALID_IDENTITY")
        self.assertEqual(audit_signal(dict(s, created_ms=START+1000), rows, CONTRACT)["status"], "INVALID_CHRONOLOGY")
        self.assertEqual(audit_signal(dict(s, result={}), rows, CONTRACT)["status"], "NO_SAVED_HISTORY")
        s["result"]["coverage_complete"] = False
        self.assertEqual(audit_signal(s, rows, CONTRACT)["status"], "UNVERIFIED_SAVED_RESULT")

    def test_wrong_contract_ohlc_grid_and_conflicting_revisions_are_invalid(self):
        rows = [fixture.c(START), fixture.c(START+STEP)]
        s = evaluated(rows)
        for bad in (replace(rows[0], contract_id="2"), replace(rows[0], high=99),
                    replace(rows[0], time_ms=START+1), replace(rows[0], low=float("nan"))):
            self.assertEqual(audit_signal(s, [bad], CONTRACT)["status"], "INVALID_CANDLES")
        self.assertEqual(audit_signal(s, rows+[replace(rows[0], close=101)], CONTRACT)["status"], "INVALID_CANDLES")

    def test_summary_keeps_mismatch_and_incomplete_separate(self):
        summary = audit_summary([dict(status=x) for x in ("MATCH", "MISMATCH", "HISTORY_GAP", "FETCH_ERROR")])
        self.assertEqual((summary["records"], summary["matched"], summary["mismatches"], summary["incomplete_or_invalid"]), (4,1,1,2))
