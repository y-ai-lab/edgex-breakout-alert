"""Signal-time feature causality and complete outcome partitioning."""
from copy import deepcopy
from dataclasses import replace
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import profit_origin_diagnostic as diagnostic
from test_volume_recovery_replay import market, record, fill, evaluate, SIGNAL, STEP


class ProfitOriginTests(unittest.TestCase):
    def test_future_fill_and_exit_cannot_change_entry_known_features(self):
        m,e=market();r=record();before=diagnostic.features(r,m,e)
        r.update(entry=12345,final_net_r=999,mfe_r=999,mae_r=999,filled_ms=SIGNAL+5*STEP)
        future=replace(e[-1],time_ms=SIGNAL+STEP,open=999,close=1000,high=1001,low=998,volume=1000000)
        future4=replace(m[-1],time_ms=m[-1].time_ms+diagnostic.STEP4,open=999,close=1000,high=1001,low=998)
        self.assertEqual(before,diagnostic.features(r,m+[future4],e+[future]))

    def test_mirrored_directional_features_both_sides(self):
        long=diagnostic.features(record(),*market())
        short=diagnostic.features(record('SHORT'),*market('SHORT'))
        for name in ('trend_separation_atr4','entry_extension_atr4','stop_width_atr15','relative_volume','directional_close_location'):
            self.assertAlmostEqual(long[name],short[name])
        self.assertEqual((long['direction'],short['direction']),('LONG','SHORT'))

    def test_signal_clock_or_missing_prior_window_blocks(self):
        m,e=market();r=record()
        with self.assertRaisesRegex(ValueError,'clock'):diagnostic.features(r|dict(created_ms=r['created_ms']-1),m,e)
        with self.assertRaisesRegex(ValueError,'missing'):diagnostic.features(r,m,e[-179:])
        with self.assertRaisesRegex(ValueError,'missing'):diagnostic.features(r,m,[x for x in e if x.time_ms!=SIGNAL-STEP])

    def test_signal_volume_excluded_from_baseline_zero_mean_unknown(self):
        m,e=market();r=record();before=diagnostic.features(r,m,e)
        changed=e[:-1]+[replace(e[-1],volume=3000,value=9000000)]
        self.assertAlmostEqual(diagnostic.features(r,m,changed)['relative_volume'],100*before['relative_volume'])
        zeros=[replace(c,volume=0) for c in e[:-1]]+[e[-1]]
        self.assertIsNone(diagnostic.features(r,m,zeros)['relative_volume'])
        for bad in (None,True,-1,float('nan'),float('inf')):
            changed=list(e);changed[-25]=replace(changed[-25],volume=bad)
            with self.assertRaisesRegex(ValueError,'volume'):diagnostic.features(r,m,changed)

    def test_friction_from_frozen_trigger_not_realized_entry(self):
        m,e=market();r=record();v=diagnostic.features(r,m,e)
        risk=diagnostic.control.cost_levels(r['trigger'],r['stop'],r['target'],r['side'])['net_risk']
        self.assertAlmostEqual(v['friction_share_of_net_stop_risk'],(risk-abs(r['trigger']-r['stop']))/risk)
        self.assertEqual(v,diagnostic.features(r|dict(entry=1,nominal_fill=1),m,e))

    def test_predeclared_bins_exact_edges_and_invalid_not_zero(self):
        s=dict(edges=[.5,1]);self.assertEqual([diagnostic.bucket(v,s) for v in (None,0,.5,1)],['UNKNOWN','lt_0.5','0.5_to_1','ge_1'])
        for bad in (True,float('inf'),float('nan')):
            with self.assertRaises(ValueError):diagnostic.bucket(bad,s)

    def test_partitions_include_open_ambiguous_unfilled_and_all_losses(self):
        r=record();f=fill(r)
        tp=replace(f,time_ms=f.time_ms+STEP,high=r['target']+.01)
        sl=replace(f,time_ms=f.time_ms+STEP,low=r['stop']-.01)
        rows=[evaluate(r,[f,tp]),evaluate(r,[f,sl]),evaluate(r,[f]),evaluate(r,[replace(f,high=r['target']+1,low=r['stop']-1)])]
        rows.append(r|dict(status='EXPIRED'))
        for i,row in enumerate(rows):row['key']=str(i)
        original=deepcopy(rows);vectors=[dict(x=x) for x in (.1,.8,.2,None,.9)]
        bins=diagnostic.summarize(rows,vectors,dict(x=dict(edges=[.5])))['x']['bins']
        self.assertEqual(sum(x['candidates'] for x in bins.values()),5)
        self.assertEqual(sum(x['filled'] for x in bins.values()),4)
        self.assertEqual(sum(x['resolved'] for x in bins.values()),2)
        self.assertEqual(bins['UNKNOWN']['statuses'],{'AMBIGUOUS':1})
        self.assertFalse(bins['lt_0.5']['cohort_complete'])
        self.assertEqual(rows,original)
        self.assertTrue(all(x['portfolio_roi_pct'] is None for x in bins.values()))

    def test_no_resolved_keeps_metrics_null_and_no_median_imputation(self):
        r=record();rs=[r|dict(key='a',status='EXPIRED')]
        x=diagnostic.summarize(rs,[dict(x=None)],dict(x=dict(edges=[1])))['x']
        self.assertIsNone(x['bins']['UNKNOWN']['net_win_rate_pct'])
        self.assertIsNone(x['bins']['UNKNOWN']['avg_net_r'])
        self.assertEqual(x['resolved_outcome_medians']['NET_WIN'],dict(count=0,median=None))
        with self.assertRaisesRegex(ValueError,'Duplicate'):diagnostic.summarize(rs*2,[dict(x=0)]*2,dict(x=dict(edges=[1])))

    def test_tp_touch_with_negative_net_return_is_not_classified_as_win(self):
        r=record();f=fill(r);tp=replace(f,time_ms=f.time_ms+STEP,high=r['target']+.01)
        row=evaluate(r,[f,tp]);row['final_net_r']=-.01
        result=diagnostic.summarize([row],[dict(x=.8)],dict(x=dict(edges=[.5])))['x']
        group=result['bins']['ge_0.5']
        self.assertEqual(group['tp_rate_pct'],100)
        self.assertEqual(group['net_win_rate_pct'],0)
        self.assertEqual(result['resolved_outcome_medians']['NET_WIN']['count'],0)
        self.assertEqual(result['resolved_outcome_medians']['NET_LOSS'],dict(count=1,median=.8))

    def test_original_evidence_and_plan_tampering_stop(self):
        self.assertEqual(len(diagnostic.plan()['sources']),3)
        with patch.object(diagnostic,'PLAN_SHA256','bad'):
            with self.assertRaisesRegex(ValueError,'plan'):diagnostic.plan()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'report.json';p.write_text('{}')
            with self.assertRaisesRegex(ValueError,'ledger changed'):diagnostic.run([p,p,p])


if __name__=='__main__':unittest.main()
