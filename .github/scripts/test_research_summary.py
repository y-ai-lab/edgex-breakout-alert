"""Artifact chronology, uncertain ROI, corruption and partial-cohort regressions."""
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import research_summary as review
from analysis_terminal import pending_entry_replay as study, pending_followup as follow
from analysis_terminal.test_pending_entry_replay import record,candle,evaluate,CONTRACT,START,STEP

ORIGIN=json.loads(study.PROTOCOL.read_text())['prospective_start_ms']
DAY=review.DAY


def fixture(days=1,wins=0,losses=0,uncertain=False):
    rows=[]
    for i,win in enumerate([True]*wins+[False]*losses+[None]*int(uncertain)):
        cs=[candle(START,low=95)]
        cs += [candle(START+STEP,open=110,low=109,high=121,close=120)] if win else [candle(START+STEP,low=89)] if win is False else []
        r=evaluate(record(),cs);r['setup_id']=r['setup_id'].rsplit(':',1)[0]+':'+str(100+i);r['key']=r['model']+':'+r['setup_id']
        shift=ORIGIN-START
        for f in ('created_ms','filled_ms','signal_candle_ms','expires_ms','outcome_ms'):
            if r[f] is not None:r[f]+=shift
        # The identity must retain the original breakout offset after shifting.
        r['setup_id']=f'setup-v1:TESTUSDC:LONG:{ORIGIN-16*STEP}:{100+i}';r['key']=r['model']+':'+r['setup_id']
        rows.append(r)
    policy=json.loads(follow.POLICY.read_text())
    state=study.research_window(ORIGIN+days*DAY,ORIGIN)
    coverage=dict(requested_markets=1,fetched_markets=1,failed_markets=0,valid_points=days*96,expected_points_all_markets=days*96)
    report=dict(dataset='RETROSPECTIVE',role='PROSPECTIVE_PARAMETERS_RETROSPECTIVE_DATA',start_ms=ORIGIN,end_ms=ORIGIN+days*DAY,period_complete=days==7,
                automatic_promotion=False,eligible_for_live_promotion=False,real_orders_enabled=False,
                protocol_sha256=policy['original_entry_protocol_sha256'],engine_sha256=hashlib.sha256(Path(study.__file__).read_bytes()).hexdigest(),
                production_rule_fingerprint=policy['entry_rule_fingerprint'],source_rule_fingerprint=policy['entry_rule_fingerprint'],
                opportunities=dict(additional_filled_setups_vs_current=len(rows),shared_filled_setups=0),
                records=rows,metrics={m:study.metrics([r for r in rows if r['model']==m]) for m in study.MODELS},
                portfolios={m:study.portfolio([r for r in rows if r['model']==m]) for m in study.MODELS},coverage=coverage,source_files_verified=1)
    source=dict(dataset='RETROSPECTIVE',eligible_for_live_promotion=False,start_ms=report['start_ms'],end_ms=report['end_ms'],coverage=coverage,
                manifest=dict(rule_fingerprint=policy['entry_rule_fingerprint'],parameters=json.loads(study.PROTOCOL.read_text())['production_parameters'],
                              universe=[asdict(CONTRACT)],sources=[dict(ticker=CONTRACT.contract_name,file='candles/1.json')],failures=[]))
    return state,report,source


class SummaryTests(unittest.TestCase):
    def run_fixture(self,parts,*,now=None):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'review').mkdir();(root/'source').mkdir()
            for path,data in zip(('run-status.json','review/pending-entry-report.json','source/replay-report.json'),parts):
                if data is not None:(root/path).write_text(json.dumps(data))
            return review.summarize(root,now_ms=now if now is not None else parts[0]['end_ms']+1)

    def test_waiting_and_missed_collection_do_not_invent_new_results(self):
        state=study.research_window(ORIGIN+1,ORIGIN)
        a=self.run_fixture((state,None,None),now=ORIGIN+1)
        self.assertEqual(a['decision'],'WAITING_FOR_FIRST_COMPLETED_DAY');self.assertNotIn('metrics',a)
        b=self.run_fixture((state,None,None),now=ORIGIN+DAY)
        self.assertEqual(b['collection_status'],'COLLECTION_NOT_CURRENT')

    def test_ready_without_source_or_ledger_is_incomplete_not_zero_signals(self):
        s,r,p=fixture()
        for parts in ((s,None,p),(s,r,None)):
            out=self.run_fixture(parts);self.assertEqual(out['decision'],'NO_VERIFIED_RESULTS');self.assertNotIn('metrics',out)

    def test_partial_and_full_week_share_cohort_and_never_add_independent_counts(self):
        a=self.run_fixture(fixture(1,wins=20));b=self.run_fixture(fixture(7,wins=20))
        self.assertEqual(a['cohort_key'],b['cohort_key']);self.assertTrue(a['decision'].startswith('PROVISIONAL_'))
        self.assertEqual(b['decision'],'CONTINUE_FORWARD_SHADOW_REQUIRED')
        for r in (a,b):self.assertFalse(r['independent_sample_counts_added']);self.assertFalse(r['eligible_for_live_promotion'])

    def test_adequately_sampled_losses_kill_without_modifying_inputs(self):
        parts=fixture(7,losses=20);before=copy.deepcopy(parts)
        r=self.run_fixture(parts);self.assertEqual(r['decision'],'KILL');self.assertEqual(parts,before)

    def test_small_sample_is_labeled_insufficient_even_when_all_are_wins(self):
        r=self.run_fixture(fixture(7,wins=19));self.assertEqual(r['decision'],'CONTINUE_INSUFFICIENT_SAMPLE')
        self.assertIn('INSUFFICIENT SAMPLE',review.markdown(r));self.assertFalse(r['automatic_promotion'])

    def test_missing_price_diagnostics_and_uncapped_funnel_are_explicit(self):
        result=self.run_fixture(fixture(wins=1,losses=1))
        values=result['entry_execution_funnel'][study.MODEL]
        self.assertEqual((values['candidates'],values['filled'],values['unfilled']),(2,2,0))
        self.assertEqual(values['fill_rate_pct'],100)
        self.assertEqual(result['entry_price_audit']['status'],'NOT_RECORDED')
        self.assertIn('未到達・遅い接触を0件と扱わない',review.markdown(result))

    def test_verified_geometry_is_descriptive_and_does_not_change_decision(self):
        from unittest.mock import patch
        from analysis_terminal import pending_fill_diagnostics as prices
        summary=prices.summarize([])
        summary.update(candidates=2,expired=1,trigger_across_roll=2,late_price_touches=1,
                       expired_later_observations={'LATE_PRICE_TOUCH':1})
        price_result=dict(status='VERIFIED_FROM_FROZEN_PUBLIC_SOURCES',summary=summary,
                          original_candidates_reproduced=2)
        with patch.object(review.entry_price_audit,'summarize',return_value=price_result):
            result=self.run_fixture(fixture(wins=1,losses=1))
        self.assertEqual(result['decision'],'PROVISIONAL_CONTINUE_INSUFFICIENT_SAMPLE')
        text=review.markdown(result)
        self.assertIn('期限切れ・期限内未到達: 1',text)
        self.assertIn('遅い接触は追加約定・勝利・ROIではない',text)
        self.assertFalse(result['independent_sample_counts_added'])

    def test_unknown_open_roi_stays_unknown_in_json_and_markdown(self):
        r=self.run_fixture(fixture(7,uncertain=True));p=r['portfolios'][study.MODEL]
        self.assertIsNone(p['closed_portfolio_roi_pct']);self.assertIsNone(p['equity_usdc'])
        self.assertIn('不明',review.markdown(r));self.assertEqual(r['metrics'][study.MODEL]['resolved'],0)

    def test_partial_closed_account_roi_is_not_labeled_final_week_roi(self):
        result=self.run_fixture(fixture(1,wins=1))
        self.assertIsNotNone(result['portfolios'][study.MODEL]['closed_portfolio_roi_pct'])
        before=copy.deepcopy(result)
        text=review.markdown(result)
        self.assertIn('既存取引の口座ROI%',text)
        self.assertIn('途中週で数値が出ても、その週の最終ROIではない',text)
        self.assertNotIn('| 最終ROI% |',text)
        self.assertEqual(result,before)

    def test_net_expectancy_excludes_unresolved_candidates_and_keeps_net_r_basis(self):
        result=self.run_fixture(fixture(1,wins=1,uncertain=True))
        self.assertEqual(result['metrics'][study.MODEL]['candidates'],2)
        self.assertEqual(result['metrics'][study.MODEL]['resolved'],1)
        text=review.markdown(result)
        self.assertIn('決着済み仮約定1件当たり',text)
        self.assertIn('net stop-risk基準',text)
        self.assertIn('候補数・未決済を分母に加えず',text)
        self.assertNotIn('候補単位のexpectancy',text)

    def test_no_lost_fills_does_not_invent_a_capital_shortage(self):
        result=self.run_fixture(fixture())
        text=review.markdown(result)
        self.assertIn('最小数量の建玉金額不足',text)
        self.assertIn('因果効果は未検証',text)
        self.assertNotIn('| SIZE_OR_RISK_LIMIT |',text)

    def test_completed_flag_future_day_and_unanchored_period_fail_closed(self):
        for kind in ('complete','future','anchor','days','role'):
            s,r,p=fixture();now=ORIGIN+DAY+1
            if kind=='complete':r['period_complete']=True
            elif kind=='future':now=ORIGIN+DAY-1
            elif kind=='anchor':s['start_ms']+=DAY
            elif kind=='days':s['days']=True
            else:r['role']='VIEWED_DEVELOPMENT'
            with self.assertRaises(ValueError):self.run_fixture((s,r,p),now=now)

    def test_changed_fingerprint_protocol_metrics_cash_or_identity_is_rejected(self):
        for kind in ('fingerprint','protocol','metrics','cash','duplicate','live','opportunities'):
            s,r,p=fixture(wins=1)
            if kind=='fingerprint':r['production_rule_fingerprint']='changed'
            elif kind=='protocol':r['protocol_sha256']='changed'
            elif kind=='metrics':r['metrics'][study.MODEL]['resolved']+=1
            elif kind=='cash':r['portfolios'][study.MODEL]['known_cash_usdc']+=1
            elif kind=='duplicate':r['records'].append(copy.deepcopy(r['records'][0]))
            elif kind=='opportunities':r['opportunities']['additional_filled_setups_vs_current']+=1
            else:r['automatic_promotion']=True
            with self.assertRaises(ValueError):self.run_fixture((s,r,p))

    def test_universe_duplicate_and_coverage_tampering_are_not_silently_excluded(self):
        for kind in ('sources','coverage','count'):
            s,r,p=fixture()
            if kind=='sources':p['manifest']['sources'].append(copy.deepcopy(p['manifest']['sources'][0]))
            elif kind=='coverage':p['coverage']['valid_points']=-1
            else:r['source_files_verified']=0
            with self.assertRaises(ValueError):self.run_fixture((s,r,p))

    def test_failed_market_blocks_positive_decision_but_keeps_observed_metrics(self):
        s,r,p=fixture(7,wins=20);other=asdict(CONTRACT);other.update(contract_id='2',contract_name='MISSINGUSDC')
        p['manifest']['universe'].append(other);p['manifest']['failures']=[dict(ticker='MISSINGUSDC')]
        r['coverage'].update(requested_markets=2,failed_markets=1,expected_points_all_markets=2*7*96)
        out=self.run_fixture((s,r,p));self.assertEqual(out['decision'],'DATA_QUALITY_BLOCKED');self.assertEqual(out['metrics'][study.MODEL]['resolved'],20)


if __name__=='__main__':unittest.main()
