"""Do not confuse omitted-cost budgets, original risk units and account ROI."""
from copy import deepcopy
import math
import sys
import unittest
from unittest.mock import patch

import cost_headroom_audit as audit
import research_summary as review
import test_research_summary as fixtures
from test_capital_admission_audit import row
from analysis_terminal import pending_entry_replay as study
from analysis_terminal.test_pending_entry_replay import START, STEP


def run(rows):
    before=deepcopy(rows)
    metrics, portfolio=study.metrics(rows),study.portfolio(rows)
    result=audit.summarize(rows,metrics,portfolio)
    assert rows==before and study.metrics(rows)==metrics and study.portfolio(rows)==portfolio
    return result


class CostHeadroomTests(unittest.TestCase):
    def test_uniform_entry_notional_charge_zeroes_original_r_sum(self):
        rows=[row('A',fill=START,outcome=START+STEP,status='TP'),
              row('B',fill=START,outcome=START+STEP,status='SL')]
        result=run(rows);bps=result['resolved_r_basket']['additional_uniform_charge_break_even_bps']
        self.assertAlmostEqual(sum(r['final_net_r']-bps/10000*r['entry']/r['net_risk'] for r in rows),0)
        self.assertGreater(bps,0)
        self.assertEqual(result['extra_charge_basis'],'TOTAL_ADDITIONAL_ENTRY_NOTIONAL_BPS_PER_RESOLVED_TRADE')
        self.assertFalse(result['actual_funding_included'])

    def test_quantity_weighted_cash_budget_differs_from_r_budget(self):
        rows=[row('A',fill=START,outcome=START+STEP,status='TP'),
              row('B',fill=START,outcome=START+STEP,status='SL',narrow=True)]
        result=run(rows)
        a=result['resolved_r_basket']['additional_uniform_charge_break_even_bps']
        b=result['capped_resolved_cash_basket']['additional_uniform_charge_break_even_bps']
        self.assertIsNotNone(a);self.assertIsNotNone(b)
        self.assertNotAlmostEqual(a,b)
        self.assertEqual(result['capped_resolved_cash_basket']['resolved'],2)

    def test_cash_zero_point_uses_original_admitted_quantities(self):
        rows=[row('A',fill=START,outcome=START+STEP,status='TP'),
              row('B',fill=START,outcome=START+STEP,status='SL',narrow=True)]
        for r in rows:r['max_order_size']=1
        result=run(rows)
        bps=result['capped_resolved_cash_basket']['additional_uniform_charge_break_even_bps']
        self.assertAlmostEqual(sum(r['net_pnl_per_unit']-bps/10000*r['entry'] for r in rows),0)
        self.assertFalse(result['changes_metrics_or_policy'])

    def test_losing_and_zero_surplus_have_no_positive_allowance(self):
        r=row('A',fill=START,outcome=START+STEP,status='SL')
        for net in (-r['net_risk'],0):
            changed=deepcopy(r);changed.update(net_pnl_per_unit=net,final_net_r=net/r['net_risk'])
            result=run([changed])
            for group in ('resolved_r_basket','capped_resolved_cash_basket'):
                self.assertEqual(result[group]['status'],'BASELINE_NONPOSITIVE')
                self.assertIsNone(result[group]['additional_uniform_charge_break_even_bps'])

    def test_no_resolved_trades_does_not_mean_zero_funding_or_zero_cost(self):
        for rows in ([],[row('A')]):
            result=run(rows)
            for group in ('resolved_r_basket','capped_resolved_cash_basket'):
                self.assertEqual(result[group]['status'],'NO_RESOLVED_TRADES')
                self.assertIsNone(result[group]['additional_uniform_charge_break_even_bps'])

    def test_open_ambiguous_and_gap_never_supplement_the_resolved_basket(self):
        for status in ('OPEN','AMBIGUOUS','DATA_GAP'):
            uncertain=row('B',fill=START,status=status,
                          outcome=None if status=='OPEN' else START+STEP)
            rows=[row('A',fill=START,outcome=START+STEP,status='TP'),uncertain]
            result=run(rows)
            self.assertEqual(result['resolved_r_basket']['resolved'],1)
            self.assertEqual(result['capped_resolved_cash_basket']['resolved'],1)
            self.assertIsNone(result['original_portfolio_roi_pct'])
            self.assertEqual(result['portfolio_active'],1)
            self.assertEqual(result['blocked_records'],int(status!='OPEN'))

    def test_capital_excluded_resolved_trade_is_not_in_cash_basket(self):
        rows=[row('A',fill=START,outcome=START+STEP,status='TP'),
              row('A',fill=START,outcome=START+STEP,status='SL')]
        rows[1]['key']+=':second'
        result=run(rows)
        self.assertEqual(result['resolved_r_basket']['resolved'],2)
        self.assertEqual(result['capped_resolved_cash_basket']['resolved'],1)

    def test_invalid_economics_chronology_duplicates_or_expected_results_fail(self):
        r=row('A',fill=START,outcome=START+STEP,status='TP')
        for field,value in [('net_risk',0),('entry',math.nan),('final_net_r',math.inf),
                            ('net_pnl_per_unit',r['net_pnl_per_unit']+1),('entry',True),
                            ('filled_ms',None),('outcome_ms',START),('outcome_ms',START+1)]:
            changed=deepcopy(r);changed[field]=value
            with self.assertRaises(ValueError):
                audit.summarize([changed],study.metrics([r]),study.portfolio([r]))
            self.assertIsNone(sys.gettrace())
        with self.assertRaises(ValueError):run([r,r])
        metrics=study.metrics([r]);metrics['resolved']+=1
        with self.assertRaises(ValueError):audit.summarize([r],metrics,study.portfolio([r]))
        portfolio=study.portfolio([r]);portfolio['known_cash_usdc']+=1
        with self.assertRaises(ValueError):audit.summarize([r],study.metrics([r]),portfolio)
        self.assertIsNone(sys.gettrace())

    def test_overflowing_normalized_costs_are_not_reported_as_zero_allowance(self):
        r=row('A',fill=START,outcome=START+STEP,status='TP')
        r.update(entry=1e300,net_risk=1e-300,net_pnl_per_unit=1e-300,final_net_r=1)
        with self.assertRaises(ValueError):
            audit.summarize([r],{}, {})
        for numerator, denominator in ((math.inf,1),(1,math.inf),(1,0),(1,-1)):
            with self.assertRaises(ValueError):audit.budget(numerator,denominator,1)
        self.assertEqual(audit.budget(1e308,1e308,1)['additional_uniform_charge_break_even_bps'],10000)
        with self.assertRaises(ValueError):audit.budget(1e308,1,1)

    def test_unknown_engine_and_existing_tracer_fail_without_leaking_trace(self):
        with patch.object(audit.hashlib,'sha256') as h:
            h.return_value.hexdigest.return_value='changed'
            with self.assertRaises(ValueError):run([])
        marker=lambda frame,event,arg:None
        try:
            sys.settrace(marker)
            with self.assertRaises(ValueError):run([])
            self.assertIs(sys.gettrace(),marker)
        finally:sys.settrace(None)

    def test_original_decision_metrics_portfolio_and_null_roi_are_preserved(self):
        parts=fixtures.fixture(7,losses=20);before=deepcopy(parts)
        result=fixtures.SummaryTests().run_fixture(parts)
        self.assertEqual(parts,before)
        self.assertEqual(result['decision'],'KILL')
        self.assertEqual(result['metrics'],parts[1]['metrics'])
        self.assertEqual(result['portfolios'],parts[1]['portfolios'])
        self.assertFalse(result['eligible_for_live_promotion'])
        text=review.markdown(result)
        self.assertIn('実際のFundingや将来の費用耐性・口座ROIではない',text)
        self.assertIn('net stop-risk',text)

    def test_older_summary_renders_without_added_audit(self):
        result=fixtures.SummaryTests().run_fixture(fixtures.fixture())
        result.pop('cost_headroom_audit');result.pop('cost_headroom_audit_sha256')
        self.assertNotIn('追加費用ゼロ損益bps',review.markdown(result))


if __name__=='__main__':unittest.main()
