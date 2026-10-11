"""Chronological allocation, frozen controls and uncertainty regression tests."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import equal_reservation_replay as allocation
from test_capital_admission_audit import row as fixture_row
from analysis_terminal import pending_entry_replay as engine
from analysis_terminal.test_pending_entry_replay import START, STEP


def row(*args, **kwargs):
    r = fixture_row(*args, **kwargs)
    if r['filled_ms'] is not None:
        r['nominal_fill'] = r['trigger']
    if r['status'] in {'TP', 'SL'}:
        r['exit_price'] = r['target_exit'] if r['status'] == 'TP' else r['stop_exit']
    return r


def run(rows):
    before = deepcopy(rows)
    result, orders, evidence = allocation.compare(rows, engine.portfolio(rows))
    assert rows == before
    return result, orders, evidence


class EqualReservationTests(unittest.TestCase):
    def test_frozen_protocol_and_dependencies(self):
        p = allocation.validate_plan()
        self.assertEqual(p['allocation']['ratio_denominator'], 3)
        self.assertFalse(p['changes_live_rules'])

    def test_original_arm_reproduces_full_original_portfolio(self):
        rows = [row('A', narrow=True), row('B', fill=START, outcome=START+2*STEP, status='TP')]
        result, _, _ = run(rows)
        self.assertEqual(result['original_portfolio'], engine.portfolio(rows))

    def test_ceiling_uses_cash_before_fill_and_leaves_two_other_slots(self):
        rows = [row(t, narrow=True, fill=START, status='OPEN') for t in ('A', 'B', 'C')]
        result, orders, evidence = run(rows)
        self.assertEqual(result['original_portfolio']['filled'], 1)
        self.assertEqual(result['equal_ceiling_portfolio']['filled'], 3)
        for key, event in evidence.items():
            self.assertEqual(event['known_cash'], 10000)
            self.assertLessEqual(orders[key]['notional'], 10000/3)
            self.assertLessEqual(orders[key]['risk'], 100)
        self.assertLessEqual(sum(o['notional'] for o in orders.values()), 10000)

    def test_reservations_maintain_total_risk_and_three_slots(self):
        rows = [row(t, fill=START, status='OPEN') for t in ('A', 'B', 'C', 'D')]
        result, orders, _ = run(rows)
        self.assertEqual(result['equal_ceiling_portfolio']['admitted'], 3)
        self.assertLessEqual(sum(o['risk'] for o in orders.values()), 300)
        self.assertEqual(result['equal_ceiling_portfolio']['exclusions']['CAPACITY_OR_TICKER'], 1)

    def test_same_ticker_stays_blocked_without_future_selection(self):
        first, second = row('A'), row('A', created=START+STEP)
        second['key'] += ':second'
        result, _, _ = run([first, second])
        self.assertEqual(result['equal_ceiling_portfolio']['admitted'], 1)

    def test_uncertain_pending_and_open_money_never_released(self):
        for status in ('AMBIGUOUS', 'DATA_GAP'):
            for filled in (False, True):
                first = row('A', narrow=True, fill=START if filled else None,
                            outcome=START+STEP, status=status)
                others = [row(t, created=START+2*STEP) for t in ('B', 'C', 'D')]
                result, orders, _ = run([first]+others)
                p = result['equal_ceiling_portfolio']
                self.assertEqual(p['uncertain'], 1)
                self.assertEqual(p['active'], 3)
                self.assertIn(first['key'], orders)
                self.assertIsNone(p['closed_portfolio_roi_pct'])
                self.assertIsNone(p['realized_roi_pct'])

    def test_pending_held_even_if_later_candidate_would_fill(self):
        rows = [row(t, narrow=True) for t in ('A', 'B', 'C')]
        rows += [row('D', created=START+STEP, fill=START+2*STEP, status='OPEN')]
        result, orders, _ = run(rows)
        self.assertEqual(result['equal_ceiling_portfolio']['filled'], 0)
        self.assertNotIn(rows[-1]['key'], orders)

    def test_known_expiry_before_same_timestamp_create_releases_only_known_money(self):
        rows = [row('A', outcome=START+STEP, status='EXPIRED'),
                row('B', created=START+STEP, fill=START+STEP, status='OPEN')]
        _, _, evidence = run(rows)
        self.assertEqual(evidence[rows[1]['key']]['reserved_notional'], 0)

    def test_future_profit_does_not_change_earlier_allocation(self):
        rows = [row('A', fill=START, outcome=START+4*STEP, status='TP'),
                row('B', created=START+STEP)]
        _, _, first = run(rows)
        changed = deepcopy(rows)
        changed[0]['net_pnl_per_unit'] *= 10
        changed[0]['final_net_r'] *= 10
        _, _, other = run(changed)
        self.assertEqual(first, other)

    def test_future_loss_or_touch_never_ranks_same_timestamp_candidates(self):
        rows = [row(t, narrow=True) for t in ('A', 'B', 'C')]
        rows += [row('D', narrow=True, fill=START, status='OPEN')]
        _, _, first = run(rows)
        self.assertNotIn(rows[-1]['key'], first)
        changed = deepcopy(rows); changed[-1]['filled_ms'] = None; changed[-1]['status'] = 'PENDING'
        _, _, other = run(changed)
        self.assertEqual(first, other)

    def test_minimum_size_above_ceiling_rejected_even_with_free_cash(self):
        r = row('A', narrow=True)
        r['min_order_size'] = 40
        result, orders, _ = run([r])
        self.assertEqual(result['original_portfolio']['admitted'], 1)
        self.assertFalse(orders)
        self.assertEqual(result['equal_ceiling_portfolio']['exclusions']['SIZE_OR_RISK_LIMIT'], 1)

    def test_step_and_maximum_size_are_preserved(self):
        r = row('A', narrow=True)
        r.update(step_size=2, min_order_size=2, max_order_size=5)
        _, orders, _ = run([r])
        self.assertEqual(orders[r['key']]['quantity'], 4)

    def test_unknown_size_is_not_synthesized(self):
        r = row('A'); r['step_size'] = None
        result, orders, _ = run([r])
        self.assertFalse(orders)
        self.assertEqual(result['equal_ceiling_portfolio']['exclusions']['UNKNOWN_SIZE_RULES'], 1)

    def test_net_risk_and_notional_both_constrain_actual_fill_quantity(self):
        r = row('A', fill=START, status='OPEN', narrow=True)
        r['entry'] *= 2; r['net_risk'] *= 2
        _, orders, evidence = run([r])
        self.assertLess(orders[r['key']]['quantity'], evidence[r['key']]['quantity'])
        self.assertLessEqual(orders[r['key']]['quantity']*r['entry'], orders[r['key']]['notional'])
        self.assertLessEqual(orders[r['key']]['quantity']*r['net_risk'], orders[r['key']]['risk'])

    def test_invalid_actual_fill_size_has_no_phantom_fill_or_profit(self):
        r = row('A', fill=START, outcome=START+2*STEP, status='TP', narrow=True)
        r['entry'] = 1e12
        result, orders, _ = run([r])
        self.assertFalse(orders[r['key']]['filled'])
        self.assertEqual(result['funded_metrics']['resolved'], 0)
        self.assertEqual(result['equal_ceiling_portfolio']['realized_net_pnl_usdc'], 0)

    def test_fees_charged_once_and_closed_roi_requires_zero_active(self):
        r = row('A', fill=START, outcome=START+2*STEP, status='TP')
        result, orders, _ = run([r]); p = result['equal_ceiling_portfolio']
        pnl = orders[r['key']]['quantity']*r['net_pnl_per_unit']
        self.assertAlmostEqual(p['known_cash_usdc'], 10000+pnl)
        self.assertAlmostEqual(p['closed_portfolio_roi_pct'], 100*pnl/10000)
        result, _, _ = run([dict(r, status='OPEN', outcome_ms=None)])
        self.assertIsNone(result['equal_ceiling_portfolio']['closed_portfolio_roi_pct'])

    def test_short_cost_stress_and_funding_unknown_are_separate(self):
        r = row('A'); r.update(side='SHORT', trigger=100, stop=110, target=80, status='TP',
                              filled_ms=START, outcome_ms=START+2*STEP, nominal_fill=100)
        levels = engine.cost_levels(100, 110, 80, 'SHORT'); r.update(levels)
        r.update(exit_price=levels['target_exit'], net_pnl_per_unit=levels['net_reward'],
                 final_net_r=levels['net_rr'])
        result, orders, _ = run([r])
        stress_pnl = (100*(1-.0005)-80*(1+.0005))-.001*(100*(1-.0005)+80*(1+.0005))
        self.assertAlmostEqual(result['fixed_quantity_stress_realized_pnl_usdc'],
                               orders[r['key']]['quantity']*stress_pnl)
        self.assertIsNone(result['stress_final_portfolio_roi_pct'])
        self.assertIsNone(result['funding_inclusive_avg_r'])

    def test_original_mismatch_duplicate_or_protocol_change_stops(self):
        rows = [row('A')]
        with self.assertRaises(ValueError): allocation.compare(rows, {})
        with self.assertRaises(ValueError): run(rows*2)
        with patch.object(allocation, 'PLAN_SHA', 'bad'):
            with self.assertRaises(ValueError): allocation.validate_plan()

    def test_future_window_checked_before_any_market_read(self):
        p = allocation.validate_plan()['future_periods'][0]
        plan = allocation.validate_plan()
        with patch.object(allocation, 'validate_plan', return_value=plan), \
                patch.object(Path, 'read_bytes', side_effect=AssertionError('peeked')):
            with self.assertRaisesRegex(ValueError, 'has not ended'):
                allocation.review(Path('missing'), Path('missing'), role=p['id'], now_ms=p['end_ms']-1)

    def test_original_signal_candle_is_not_used_by_allocator(self):
        r = row('A', fill=START, status='OPEN')
        _, _, evidence = run([r])
        self.assertEqual(evidence[r['key']]['stamp_ms'], r['signal_candle_ms']+STEP)

    def test_empty_input_has_zero_activity_not_profitable_evidence(self):
        result, _, _ = run([])
        self.assertEqual(result['equal_ceiling_portfolio']['filled'], 0)
        self.assertIsNone(result['funded_metrics']['win_rate'])

    def test_future_control_builder_does_not_open_unfinished_prices(self):
        plan = allocation.validate_plan(); p = plan['future_periods'][0]
        with patch.object(allocation, 'validate_plan', return_value=plan), \
                patch.object(Path, 'read_bytes', side_effect=AssertionError('peeked')):
            with self.assertRaisesRegex(ValueError, 'has not ended'):
                allocation.build_registered_controls(Path('missing'), role=p['id'],
                                                       now_ms=p['end_ms']-1, analyze=None, settings=None)

    def test_control_builder_rejects_development_or_unregistered_roles(self):
        for role in ('development', 'other'):
            with self.assertRaises(ValueError):
                allocation.build_registered_controls(Path('missing'), role=role,
                                                       now_ms=START, analyze=None, settings=None)


if __name__ == '__main__':
    unittest.main()
