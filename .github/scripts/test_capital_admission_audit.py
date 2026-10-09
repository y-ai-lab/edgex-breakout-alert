"""Original-event ordering, uncertainty and exact lost-fill accounting."""
from copy import deepcopy
import sys
import unittest
from unittest.mock import patch

import capital_admission_audit as audit
import research_summary as review
import test_research_summary as fixtures
from analysis_terminal import pending_entry_replay as study
from analysis_terminal.test_pending_entry_replay import record, START, STEP


def row(ticker, *, created=START, fill=None, outcome=None, status='PENDING', narrow=False):
    r = record()
    identity = f'setup-v1:{ticker}:LONG:{START-16*STEP}:100'
    r.update(ticker=ticker,setup_id=identity,key=r['model']+':'+identity,
             created_ms=created+1,signal_candle_ms=created-STEP,
             expires_ms=created+4*STEP,trigger=100,stop=99.9 if narrow else 90,target=120,
             filled_ms=fill,outcome_ms=outcome,status=status,final_net_r=None,
             step_size=.01,min_order_size=.01,max_order_size=1000)
    if fill is not None:
        levels=study.cost_levels(r['trigger'],r['stop'],r['target'],r['side'])
        r.update(levels)
        if status in {'TP','SL'}:
            net=levels['net_reward'] if status=='TP' else -levels['net_risk']
            r.update(net_pnl_per_unit=net,final_net_r=net/levels['net_risk'])
    return r


def run(rows):
    before=deepcopy(rows)
    expected=study.portfolio(rows)
    result=audit.summarize(rows,expected)
    assert rows==before
    return result,expected


class AdmissionTests(unittest.TestCase):
    def test_capacity_exclusions_distinguish_filled_and_unfilled_candidates(self):
        rows=[row(t) for t in ('A','B','C')]
        rows += [row('D',created=START+STEP,fill=START+2*STEP,status='OPEN'),
                 row('E',created=START+STEP)]
        result,_=run(rows);counts=result['reasons']['CAPACITY_OR_TICKER']
        self.assertEqual((result['uncapped_filled'],result['capped_filled'],result['filled_omitted']),(1,0,1))
        self.assertEqual((counts['excluded_candidates'],counts['excluded_with_uncapped_fill'],counts['excluded_without_uncapped_fill']),(2,1,1))
        self.assertEqual(counts['lost_filled_capacity_full'],1)
        self.assertEqual(counts['lost_filled_with_unfilled_reservations'],1)
        self.assertEqual(counts['lost_filled_with_filled_reservations'],0)

    def test_minimum_notional_shortage_is_not_a_risk_shortage(self):
        rows=[row('A',narrow=True),row('B',created=START+STEP,fill=START+2*STEP,status='OPEN')]
        result,_=run(rows);counts=result['reasons']['SIZE_OR_RISK_LIMIT']
        self.assertEqual(result['filled_omitted'],1)
        self.assertEqual(counts['lost_filled_minimum_notional_above_remaining'],1)
        self.assertEqual(counts['lost_filled_minimum_risk_above_remaining'],0)
        self.assertEqual(counts['lost_filled_with_unfilled_reservations'],1)

    def test_report_distinguishes_notional_risk_and_overlapping_reservations(self):
        rows=[row('A',narrow=True),row('B',created=START+STEP,fill=START+2*STEP,status='OPEN')]
        audit_result,portfolio=run(rows)
        result=fixtures.SummaryTests().run_fixture(fixtures.fixture())
        result['capital_admission_audit'][study.MODEL]=audit_result
        result['portfolios'][study.MODEL]=portfolio
        before=deepcopy(result)
        text=review.markdown(result)
        self.assertIn(f'| {study.MODEL} | SIZE_OR_RISK_LIMIT | 1 | 1 | 0 | 1 |',text)
        self.assertIn('重複し得るため合算しない',text)
        self.assertIn('保有・不確定が残れば不明',text)
        self.assertEqual(result,before)

    def test_same_ticker_conflict_does_not_claim_three_positions(self):
        second=row('A',created=START+STEP,fill=START+2*STEP,status='OPEN')
        second['key']+=':second'
        result,_=run([row('A'),second]);counts=result['reasons']['CAPACITY_OR_TICKER']
        self.assertEqual(counts['lost_filled_same_ticker'],1)
        self.assertEqual(counts['lost_filled_capacity_full'],0)

    def test_same_timestamp_end_releases_reservation_before_create(self):
        rows=[row('A',outcome=START+STEP,status='EXPIRED'),
              row('B',created=START+STEP,fill=START+2*STEP,status='OPEN')]
        result,portfolio=run(rows)
        self.assertEqual(result['capped_filled'],1)
        self.assertEqual(result['reasons'],{})
        self.assertIsNone(portfolio['closed_portfolio_roi_pct'])

    def test_ambiguous_and_gap_reservations_remain_locked_across_later_entries(self):
        for status in ('AMBIGUOUS','DATA_GAP'):
            rows=[row('A',fill=START,outcome=START+STEP,status=status,narrow=True),
                  row('B',created=START+2*STEP,fill=START+3*STEP,status='OPEN')]
            result,portfolio=run(rows)
            self.assertEqual((portfolio['active'],portfolio['uncertain']),(1,1))
            self.assertIsNone(portfolio['closed_portfolio_roi_pct'])
            self.assertIsNone(portfolio['realized_roi_pct'])
            self.assertEqual(result['filled_omitted'],1)
            self.assertEqual(result['retained_filled_statuses'],{status:1})

    def test_future_profit_does_not_size_an_earlier_candidate(self):
        rows=[row('A',fill=START,outcome=START+3*STEP,status='TP',narrow=True),
              row('B',created=START+STEP,fill=START+2*STEP,status='OPEN')]
        result,portfolio=run(rows)
        self.assertGreater(portfolio['realized_net_pnl_usdc'],0)
        self.assertEqual(result['filled_omitted'],1)
        self.assertEqual(result['reasons']['SIZE_OR_RISK_LIMIT']['excluded_with_uncapped_fill'],1)

    def test_empty_zero_fills_do_not_invent_results_or_shared_ids(self):
        result,portfolio=run([])
        self.assertEqual(result['filled_omitted'],0)
        self.assertEqual(result['reasons'],{})
        self.assertIsNone(result['capped_shared_setup_count'])
        self.assertFalse(result['new_samples_added'])
        self.assertFalse(result['actual_execution_evidence'])
        self.assertEqual(portfolio['resolved'],0)

    def test_unknown_size_and_invalid_at_fill_are_accounted_separately(self):
        unknown=row('A');unknown['step_size']=None
        invalid=row('B',fill=START+STEP,status='OPEN');invalid.update(min_order_size=5,net_risk=100)
        result,_=run([unknown,invalid])
        self.assertEqual(result['reasons']['UNKNOWN_SIZE_RULES']['excluded_without_uncapped_fill'],1)
        self.assertEqual(result['reasons']['SIZE_INVALID_AT_FILL']['excluded_with_uncapped_fill'],1)
        self.assertEqual(result['filled_omitted'],1)

    def test_tampered_portfolio_and_duplicate_input_fail_without_tracer_leak(self):
        rows=[row('A')];expected=study.portfolio(rows);expected['known_cash_usdc']+=1
        with self.assertRaises(ValueError):audit.summarize(rows,expected)
        self.assertIsNone(sys.gettrace())
        with self.assertRaises(ValueError):audit.summarize(rows*2,study.portfolio(rows*2))

    def test_frozen_engine_drift_is_rejected(self):
        with patch.object(audit.hashlib,'sha256') as checksum:
            checksum.return_value.hexdigest.return_value='changed'
            with self.assertRaises(ValueError):audit.summarize([],study.portfolio([]))

    def test_existing_tracer_is_not_replaced(self):
        marker=lambda frame,event,arg:None
        try:
            sys.settrace(marker)
            with self.assertRaises(ValueError):audit.summarize([],study.portfolio([]))
            self.assertIs(sys.gettrace(),marker)
        finally:sys.settrace(None)

    def test_daily_research_decision_metrics_roi_and_comparison_are_unchanged(self):
        parts=fixtures.fixture(7,losses=20)
        result=fixtures.SummaryTests().run_fixture(parts)
        self.assertEqual(result['decision'],'KILL')
        self.assertEqual(result['metrics'],parts[1]['metrics'])
        self.assertEqual(result['portfolios'],parts[1]['portfolios'])
        self.assertIsNone(result['comparison']['capped_shared_setup_count'])
        self.assertIn('未約定候補の除外を失われた約定に数えない',review.markdown(result))


if __name__=='__main__':unittest.main()
