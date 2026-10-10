"""Observe original event order, censored reservations and locked uncertainty."""
from copy import deepcopy
import sys
import unittest
from unittest.mock import patch

import reservation_time_audit as audit
import research_summary as review
import test_research_summary as fixtures
from test_capital_admission_audit import row
from analysis_terminal import pending_entry_replay as study
from analysis_terminal.test_pending_entry_replay import START, STEP


def run(rows, *, end=START+8*STEP):
    original=deepcopy(rows)
    portfolio=study.portfolio(rows)
    result=audit.summarize(rows,portfolio,start_ms=START,end_ms=end)
    assert rows==original
    assert study.portfolio(rows)==portfolio
    return result,portfolio


class ReservationTests(unittest.TestCase):
    def test_expired_pending_uses_original_reservation_until_end_event(self):
        r=row('A',outcome=START+4*STEP,status='EXPIRED')
        result,_=run([r]);g=result['groups']
        self.assertEqual(result['occupied_hours'],1)
        self.assertEqual(g['PENDING']['slot_hours'],1)
        levels=study.cost_levels(r['trigger'],r['stop'],r['target'],r['side'])
        quantity=study.floor_size(min(100/levels['net_risk'],10000/levels['entry']),r['step_size'])
        self.assertAlmostEqual(g['PENDING']['notional_usdc_hours'],quantity*levels['entry'])
        self.assertAlmostEqual(g['PENDING']['time_weighted_average_notional_usdc'],quantity*levels['entry']/2)
        self.assertEqual(result['pending_share_of_reserved_notional_time_pct'],100)
        self.assertEqual(result['event_count'],2)

    def test_fill_switches_at_bar_close_not_intrabar_open(self):
        r=row('A',fill=START,status='OPEN')
        result,portfolio=run([r],end=START+4*STEP)
        self.assertEqual(result['groups']['PENDING']['slot_hours'],.25)
        self.assertEqual(result['groups']['FILLED']['slot_hours'],.75)
        self.assertEqual(result['end_counts'],dict(PENDING=0,FILLED=1,UNCERTAIN=0))
        self.assertIsNone(portfolio['closed_portfolio_roi_pct'])

    def test_same_timestamp_exit_before_creation_has_no_double_reservation(self):
        rows=[row('A',outcome=START+4*STEP,status='EXPIRED'),
              row('B',created=START+4*STEP)]
        result,_=run(rows)
        self.assertEqual(result['groups']['PENDING']['slot_hours'],2)
        self.assertEqual(result['occupied_hours'],2)
        self.assertEqual(result['end_counts']['PENDING'],1)

    def test_unfilled_ambiguous_and_gap_funds_remain_locked_to_cutoff(self):
        for status in ('AMBIGUOUS','DATA_GAP'):
            result,portfolio=run([row('A',outcome=START+STEP,status=status)])
            self.assertEqual(result['groups']['PENDING']['slot_hours'],.25)
            self.assertEqual(result['groups']['UNCERTAIN']['slot_hours'],1.75)
            self.assertEqual(result['end_counts']['UNCERTAIN'],1)
            self.assertIsNone(portfolio['closed_portfolio_roi_pct'])
            self.assertFalse(result['unknown_capital_released'])

    def test_filled_ambiguous_becomes_uncertain_without_unlocking(self):
        result,_=run([row('A',fill=START,outcome=START+2*STEP,status='AMBIGUOUS')])
        self.assertEqual(result['groups']['PENDING']['slot_hours'],.25)
        self.assertEqual(result['groups']['FILLED']['slot_hours'],.25)
        self.assertEqual(result['groups']['UNCERTAIN']['slot_hours'],1.5)

    def test_excluded_candidate_is_not_counted_as_a_reservation(self):
        rows=[row(t) for t in ('A','B','C')]+[row('D',created=START+STEP)]
        result,p=run(rows)
        self.assertEqual(p['admitted'],3)
        self.assertEqual(result['groups']['PENDING']['slot_hours'],6)
        self.assertEqual(result['event_count'],4)

    def test_pending_is_right_censored_not_expired_from_its_future_deadline(self):
        r=row('A')
        result,_=run([r],end=START+2*STEP)
        self.assertEqual(result['groups']['PENDING']['slot_hours'],.5)
        self.assertEqual(result['end_counts']['PENDING'],1)
        self.assertEqual(r['status'],'PENDING')

    def test_initial_empty_time_is_in_denominator_and_zero_capital_has_null_share(self):
        result,_=run([row('A',created=START+4*STEP)])
        self.assertEqual(result['groups']['PENDING']['time_weighted_average_slots'],.5)
        empty,_=run([])
        self.assertEqual(empty['occupied_hours'],0)
        self.assertIsNone(empty['pending_share_of_reserved_notional_time_pct'])

    def test_future_profit_keeps_earlier_reservation_observation_unchanged(self):
        rows=[row('A',fill=START,outcome=START+4*STEP,status='TP'),row('B',created=START+STEP)]
        result,_=run(rows)
        mutated=deepcopy(rows);mutated[0]['net_pnl_per_unit']*=2
        other,_=run(mutated)
        self.assertEqual(result['groups'],other['groups'])

    def test_bad_window_duplicate_event_and_cash_fail_without_tracer_leak(self):
        rows=[row('A')];expected=study.portfolio(rows)
        for start,end in ((True,START+STEP),(START+1,START+STEP),(START,START),(START+STEP,START+2*STEP)):
            with self.assertRaises(ValueError):audit.summarize(rows,expected,start_ms=start,end_ms=end)
        with self.assertRaises(ValueError):run(rows*2)
        invalid=row('A',outcome=START+9*STEP,status='EXPIRED')
        with self.assertRaises(ValueError):run([invalid])
        with self.assertRaises(ValueError):run([row('A',fill=START+8*STEP,status='OPEN')])
        expected['known_cash_usdc']+=1
        with self.assertRaises(ValueError):audit.summarize(rows,expected,start_ms=START,end_ms=START+8*STEP)
        self.assertIsNone(sys.gettrace())

    def test_engine_drift_and_existing_tracer_fail_without_replacing_it(self):
        with patch.object(audit.hashlib,'sha256') as digest:
            digest.return_value.hexdigest.return_value='changed'
            with self.assertRaises(ValueError):run([])
        marker=lambda frame,event,arg:None
        try:
            sys.settrace(marker)
            with self.assertRaises(ValueError):run([])
            self.assertIs(sys.gettrace(),marker)
        finally:sys.settrace(None)

    def test_summary_keeps_decision_metrics_portfolio_and_null_shared_ids(self):
        parts=fixtures.fixture(7,losses=20);original=deepcopy(parts)
        result=fixtures.SummaryTests().run_fixture(parts)
        self.assertEqual(parts,original)
        self.assertEqual(result['decision'],'KILL')
        self.assertEqual(result['metrics'],parts[1]['metrics'])
        self.assertEqual(result['portfolios'],parts[1]['portfolios'])
        self.assertIsNone(result['comparison']['capped_shared_setup_count'])
        self.assertTrue(result['reservation_time_audit'][study.MODEL]['verified'])
        text=review.markdown(result)
        self.assertIn('未約定の平均予約USDC',text)
        self.assertIn('因果効果は未検証',text)
        self.assertFalse(result['independent_sample_counts_added'])

    def test_older_saved_summary_can_still_render_without_time_audit(self):
        result=fixtures.SummaryTests().run_fixture(fixtures.fixture())
        result.pop('reservation_time_audit');result.pop('reservation_time_audit_sha256')
        self.assertNotIn('未約定の平均予約USDC',review.markdown(result))


if __name__=='__main__':unittest.main()
