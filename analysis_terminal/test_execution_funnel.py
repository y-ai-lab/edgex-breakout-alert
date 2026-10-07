"""Accounting regressions; no new entries or parameter selection."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from analysis_terminal import execution_funnel as funnel,pending_entry_replay as study,pending_followup as follow
from analysis_terminal.test_pending_entry_replay import record,candle,evaluate,START,STEP
from analysis_terminal.test_pending_followup import fixture


def rename(r,level):
    r=copy.deepcopy(r)
    r["setup_id"]=r["setup_id"].rsplit(":",1)[0]+":"+str(level)
    r["key"]=r["model"]+":"+r["setup_id"]
    return r


def report(rows,*,end=START+5*STEP):
    return dict(dataset="RETROSPECTIVE",role="VIEWED_TEST_DATA",start_ms=START,end_ms=end,period_complete=False,
                protocol_sha256=hashlib.sha256(study.PROTOCOL.read_bytes()).hexdigest(),
                automatic_promotion=False,eligible_for_live_promotion=False,real_orders_enabled=False,records=rows,
                metrics={m:study.metrics([r for r in rows if r["model"]==m]) for m in study.MODELS},
                portfolios={m:study.portfolio([r for r in rows if r["model"]==m]) for m in study.MODELS})


def current():
    return evaluate(record(model="current_next_open"),[candle(START,open=100,low=99),
                    candle(START+STEP,open=110,low=109,high=126,close=125)])


def proposal():
    return evaluate(record(),[candle(START,low=95),candle(START+STEP,open=95,high=96,low=89,close=90)])


class FunnelTests(unittest.TestCase):
    def test_proposal_only_fills_are_not_net_increase_and_lost_current_is_shown(self):
        rows=[current(),rename(proposal(),101),rename(proposal(),102)]
        out=funnel.summarize(report(rows));c=out["proposal_vs_current"]
        self.assertEqual((c["proposal_only_filled_setups"],c["current_filled_missing_in_proposal"],c["net_filled_count_difference"]),(2,1,1))
        self.assertEqual(c["shared_filled_setups"],0)
        self.assertIsNone(c["capped_shared_setup_count"])

    def test_shared_setup_uses_identity_across_models_without_double_counting(self):
        c=funnel.summarize(report([current(),proposal()]))["proposal_vs_current"]
        self.assertEqual((c["shared_filled_setups"],c["proposal_only_filled_setups"],c["current_filled_missing_in_proposal"]),(1,0,0))

    def test_expiry_and_ambiguous_fill_use_distinct_denominators(self):
        expired=evaluate(record(),[candle(START+i*STEP) for i in range(4)])
        ambiguous=evaluate(record(),[candle(START,low=89)])
        r=report([proposal(),rename(expired,101),rename(ambiguous,102)]);before=copy.deepcopy(r)
        m=funnel.summarize(r)["models"][study.MODEL];u=m["uncapped"]
        self.assertEqual((u["candidates"],u["filled"],u["unfilled"]),(3,2,1))
        self.assertEqual(u["unfilled_status_counts"],{"EXPIRED":1})
        self.assertEqual(u["filled_status_counts"],{"SL":1,"AMBIGUOUS":1})
        self.assertEqual(u["observed_metrics"]["resolved"],1)
        self.assertEqual(u["observed_metrics"]["win_rate"],0)
        self.assertEqual(u["observed_metrics"]["sample_status"],"INSUFFICIENT SAMPLE")
        # An uncapped ambiguous candidate may be rejected by ticker capacity.
        # Only an admitted uncertain position must block the capped account ROI.
        self.assertEqual(m["capped"],r["portfolios"][study.MODEL])
        uncertain=funnel.summarize(report([ambiguous]))["models"][study.MODEL]["capped"]
        self.assertIsNone(uncertain["closed_portfolio_roi_pct"])
        self.assertEqual(r,before)

    def test_empty_and_censored_pending_are_not_zero_win_rate_or_known_roi(self):
        empty=funnel.summarize(report([]))["models"][study.MODEL]
        self.assertIsNone(empty["uncapped"]["fill_rate_pct"])
        pending=evaluate(record(),[candle(START)])
        out=funnel.summarize(report([pending],end=START+STEP));u=out["models"][study.MODEL]
        self.assertEqual(u["uncapped"]["unfilled_status_counts"],{"PENDING":1})
        self.assertIsNone(u["uncapped"]["observed_metrics"]["win_rate"])
        self.assertIsNone(u["capped"]["closed_portfolio_roi_pct"])

    def test_last_allowed_fill_is_fourth_bar_not_signal_bar(self):
        cs=[candle(START+i*STEP) for i in range(3)]+[candle(START+3*STEP,low=95),candle(START+4*STEP,low=89)]
        u=funnel.summarize(report([evaluate(record(),cs)]))["models"][study.MODEL]["uncapped"]
        self.assertEqual(u["fill_wait_bars"],{"4":1})

    def test_duplicate_identity_or_pre_signal_fill_fails_closed(self):
        for mode in ("duplicate","early_fill","identity"):
            r=report([proposal()])
            if mode=="duplicate":r["records"].append(copy.deepcopy(r["records"][0]))
            if mode=="early_fill":r["records"][0]["filled_ms"]=START-STEP
            if mode=="identity":r["records"][0]["ticker"]="OTHERUSDC"
            with self.assertRaises(ValueError):funnel.summarize(r)

    def test_changed_metrics_or_cash_is_not_silently_repaired(self):
        for field,key in (("metrics","filled"),("portfolios","known_cash_usdc")):
            r=report([proposal()]);r[field][study.MODEL][key]+=1
            with self.assertRaises(ValueError):funnel.summarize(r)

    def test_live_flags_bad_protocol_or_nonfinite_values_are_rejected(self):
        for change in ("automatic_promotion","protocol_sha256","numeric","outcome"):
            r=report([proposal()])
            if change=="automatic_promotion":r[change]=True
            if change=="protocol_sha256":r[change]="changed"
            if change=="numeric":r["records"][0]["mfe_r"]=float('nan')
            if change=="outcome":r["records"][0]["outcome_ms"]=None
            with self.assertRaises(ValueError):funnel.summarize(r)

    def test_followup_is_same_cohort_and_preserves_fill_count(self):
        base,source=fixture();end=base["end_ms"]
        r=follow.extend(base,source,{"TESTUSDC":[candle(end,open=110,high=121,low=109,close=120)]},end_ms=end+STEP)
        out=funnel.summarize(r)
        self.assertTrue(out["same_cohort_update"])
        self.assertEqual(out["cohort_end_ms"],end)
        self.assertEqual(out["models"][study.MODEL]["uncapped"]["filled"],1)
        self.assertFalse(out["eligible_for_live_promotion"])

    def test_file_checksum_provenance_and_input_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/"report.json";raw=json.dumps(report([proposal()])).encode();source.write_bytes(raw)
            digest=hashlib.sha256(raw).hexdigest()
            result=funnel.write_report(source,root/"out/funnel.json",digest)
            self.assertEqual(result["source_report_sha256"],digest)
            self.assertEqual(source.read_bytes(),raw)
            with self.assertRaises(ValueError):funnel.write_report(source,root/"bad.json","wrong")
            self.assertFalse((root/"bad.json").exists())


if __name__=="__main__":
    unittest.main()
