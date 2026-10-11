"""Protect funding clock, market coverage and unknown intrabar ordering."""
from copy import deepcopy
import math
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import funding_coverage_audit as audit
from test_capital_admission_audit import row
from analysis_terminal.test_pending_entry_replay import START, STEP


def event(t, rate='0.0001', index='100', interval='60'):
    return dict(contractId='1', fundingTimestamp=str(t), fundingTime=str(t),
                fundingRateIntervalMin=interval, fundingRate=rate, indexPrice=index, isSettlement=True)


def page(rows, cursor=''):
    return dict(code='SUCCESS', data=dict(dataList=rows, nextPageOffsetData=cursor))


class FundingTests(unittest.TestCase):
    def test_registered_plan_cannot_be_replaced_or_silently_reformatted(self):
        p=Path(audit.__file__).resolve().parents[1]/'research/funding_audit_plan.json'
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),audit.PLAN_SHA256)
        with tempfile.TemporaryDirectory() as d:
            changed=Path(d)/'plan.json';changed.write_bytes(p.read_bytes()+b' ')
            with self.assertRaisesRegex(ValueError,'plan changed'):
                audit.run(changed,Path(d)/'missing.json',[])

    def test_complete_hourly_and_four_hour_grids_are_distinct(self):
        for interval in (60, 240):
            step = interval*60000
            rows = [event(t, interval=str(interval)) for t in range(0, 86400000+1, step)]
            result = audit.settlements([page(list(reversed(rows)))], '1', 0, 86400000)
            self.assertEqual(len(result), len(rows))
            self.assertEqual(result[0]['interval_ms'], step)

    def test_missing_internal_first_last_or_cutoff_settlement_blocks(self):
        rows = [event(t) for t in range(0, 14400000+1, 3600000)]
        for index in (0, 1, -1):
            changed = deepcopy(rows); changed.pop(index)
            with self.assertRaises(ValueError):
                audit.settlements([page(changed)], '1', 0, 14400000)

    def test_duplicates_variable_interval_clock_and_phase_block(self):
        original = [event(t) for t in range(0, 14400000+1, 3600000)]
        for field, value in [('fundingTimestamp','3600001'),('fundingTime','1'),
                             ('fundingRateIntervalMin','240'), ('fundingRateIntervalMin','0'),
                             ('fundingRateIntervalMin','1.5')]:
            rows = deepcopy(original); rows[1][field] = value
            with self.assertRaises(ValueError):audit.settlements([page(rows)], '1', 0, 14400000)
        with self.assertRaises(ValueError):audit.settlements([page(original+original)], '1', 0, 14400000)

    def test_forecasts_wrong_market_missing_and_nonfinite_prices_block(self):
        for field, value in [('isSettlement',False),('isSettlement','true'),('contractId','2'),
                             ('indexPrice','NaN'),('indexPrice','0'),('indexPrice',True),
                             ('fundingRate','Infinity'),('fundingRate',False)]:
            r = event(0); r[field] = value
            with self.assertRaises(ValueError):audit.settlements([page([r])], '1', 0, 1)
        with self.assertRaises(ValueError):audit.settlements([page([])], '1', 0, 1)

    def test_pagination_must_exhaust_without_repeated_cursor(self):
        rows = [event(t) for t in range(0, 7200000+1, 3600000)]
        self.assertEqual(len(audit.settlements([page(rows[:1],'next'),page(rows[1:])], '1', 0, 7200000)),3)
        for pages in ([page(rows,'next')], [page(rows),page([])],
                      [page(rows[:1],'x'),page(rows[1:2],'x'),page(rows[2:])]):
            with self.assertRaises(ValueError):audit.settlements(pages, '1', 0, 7200000)

    def test_index_price_is_used_without_oracle_substitution(self):
        r = event(0); r['oraclePrice'] = '1000'
        self.assertEqual(audit.settlements([page([r])], '1', 0, 1)[0]['index'],100)
        del r['indexPrice']
        with self.assertRaises(KeyError):audit.settlements([page([r])], '1', 0, 1)

    def test_long_short_charges_and_negative_funding_receipts(self):
        r = row('A', fill=START, outcome=START+4*STEP, status='TP')
        events = [dict(time_ms=START+2*STEP, index=100, rate=.001)]
        long = audit.exposure(r, events)
        r['side'] = 'SHORT'; short = audit.exposure(r, events)
        self.assertAlmostEqual(long['funding_charge_r'],.1/r['net_risk'])
        self.assertAlmostEqual(long['funding_charge_r'],-short['funding_charge_r'])
        events[0]['rate'] *= -1
        self.assertAlmostEqual(audit.exposure(r, events)['funding_charge_r'],long['funding_charge_r'])

    def test_fill_and_exit_bar_including_endpoints_are_unknown(self):
        r = row('A', fill=START, outcome=START+4*STEP, status='TP')
        for t in (START, START+STEP-1, START+3*STEP, START+4*STEP):
            result = audit.exposure(r,[dict(time_ms=t,index=100,rate=.001)])
            self.assertEqual(result['status'],'TIMING_AMBIGUOUS')
            self.assertIsNone(result['funding_charge_r']);self.assertIsNone(result['adjusted_net_r'])
        for t in (START+STEP,START+3*STEP-1):
            self.assertEqual(audit.exposure(r,[dict(time_ms=t,index=100,rate=.001)])['status'],'VERIFIED_HYPOTHETICAL')

    def test_prior_and_future_settlements_do_not_change_original_outcome(self):
        r = row('A', fill=START, outcome=START+4*STEP, status='SL');before=deepcopy(r)
        events = [dict(time_ms=t,index=100,rate=.9) for t in (START-1,START+4*STEP+1)]
        result = audit.exposure(r, events)
        self.assertEqual(result['funding_charge_r'],0);self.assertEqual(result['adjusted_net_r'],-1)
        self.assertEqual(r,before)

    def test_missing_coverage_blocks_full_basket_never_assumes_zero(self):
        r = row('A', fill=START, outcome=START+4*STEP, status='TP')
        result,_ = audit.summarize([r],{})
        self.assertEqual(result['blocked_funding_resolved'],1)
        self.assertIsNone(result['funding_adjusted_avg_net_r']);self.assertIsNone(result['portfolio_roi_pct'])

    def test_one_unknown_trade_blocks_all_resolved_metrics(self):
        rs = [row('A',fill=START,outcome=START+4*STEP,status='TP'),
              row('B',fill=START,outcome=START+4*STEP,status='SL')]
        result,_=audit.summarize(rs,{'A':[],'B':[dict(time_ms=START,index=100,rate=.001)]})
        self.assertEqual(result['funding_verified_resolved'],1)
        self.assertEqual(result['timing_ambiguous_resolved'],1)
        for field in ('funding_adjusted_avg_net_r','funding_adjusted_profit_factor','funding_adjusted_net_win_rate_pct'):
            self.assertIsNone(result[field])

    def test_complete_resolved_basket_reprices_without_changing_funds_or_samples(self):
        rs=[row('A',fill=START,outcome=START+4*STEP,status='TP'),row('B',fill=START,outcome=START+4*STEP,status='SL')]
        before=deepcopy(rs);events=[dict(time_ms=START+2*STEP,index=100,rate=.001)]
        result, ledger=audit.summarize(rs,{'A':events,'B':events})
        expected=[r['final_net_r']-.1/r['net_risk'] for r in rs]
        self.assertAlmostEqual(result['funding_adjusted_avg_net_r'],sum(expected)/2)
        self.assertAlmostEqual(result['funding_adjusted_profit_factor'],expected[0]/-expected[1])
        self.assertEqual(result['funding_adjusted_net_win_rate_pct'],50)
        self.assertEqual(rs,before);self.assertEqual(result['new_samples_added'],0)
        self.assertIsNone(result['portfolio_roi_pct'])

    def test_zero_rate_still_has_boundary_ownership_ambiguity(self):
        r=row('A',fill=START,outcome=START+4*STEP,status='TP')
        self.assertEqual(audit.exposure(r,[dict(time_ms=START,index=100,rate=0)])['status'],'TIMING_AMBIGUOUS')

    def test_unresolved_records_are_preserved_not_completed_by_funding(self):
        rs=[row('A',status=s) for s in ('OPEN','AMBIGUOUS','DATA_GAP','EXPIRED')]
        for i,r in enumerate(rs):r['key']+=str(i)
        result,ledger=audit.summarize(rs,{})
        self.assertEqual(result['statuses']['UNRESOLVED_OR_UNFILLED'],4)
        self.assertIsNone(result['funding_adjusted_avg_net_r'])
        self.assertTrue(all(x['funding_charge_r'] is None for x in ledger))

    def test_duplicates_invalid_clocks_risk_and_overflow_stop(self):
        r=row('A',fill=START,outcome=START+4*STEP,status='TP')
        with self.assertRaises(ValueError):audit.summarize([r,r],{})
        for field,value in [('filled_ms',START+1),('outcome_ms',START),('side','X'),
                            ('net_risk',0),('net_risk',math.nan),('net_risk',True)]:
            bad=deepcopy(r);bad[field]=value
            with self.assertRaises(ValueError):audit.exposure(bad,[])
        with self.assertRaises(ValueError):
            audit.exposure(r,[dict(time_ms=START+2*STEP,index=1e308,rate=1e308)])

    def test_archive_identity_checksums_metrics_and_cursor_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            def write(name, value):
                p=root/name;p.write_text(json.dumps(value));return hashlib.sha256(p.read_bytes()).hexdigest()
            def fixture():
                r=row('A',fill=START,outcome=START+4*STEP,status='TP')
                r['nominal_fill']=r['trigger']
                r['exit_price']=r['target_exit']
                market_hash=write('market.json',{'contract':{'contract_id':'1'}})
                source_hash=write('replay-report.json',{'manifest':{'sources':[{'ticker':'A','file':'market.json','sha256':market_hash}]}})
                report=dict(start_ms=START,end_ms=START+8*STEP,source_dir=str(root),records=[r],
                    metrics={r['model']:audit.engine.metrics([r])},
                    stress_metrics={r['model']:audit.stress.stressed_metrics([r])},
                    portfolios={r['model']:audit.engine.portfolio([r])})
                report_hash=write('report.json',report)
                p=dict(id='synthetic',start_ms=START,end_ms=START+8*STEP,source_report_sha256=source_hash,
                    sources=[{'id':'test','model':r['model'],'report_sha256':report_hash}])
                plan_hash=write('plan.json',p)
                first=(START+3599999)//3600000*3600000
                page_hash=write('page.json',page([event(t) for t in range(first,START+8*STEP+1,3600000)]))
                m=dict(plan_sha256=plan_hash,start_ms=START,end_ms=START+8*STEP,markets=[dict(ticker='A',contract_id='1',
                    start_ms=START-86400000,end_exclusive_ms=START+8*STEP+1,status='EXHAUSTED',
                    pages=[dict(file='page.json',sha256=page_hash,status=200,requested_offset='')])])
                write('manifest.json',m)
                return plan_hash,m,p,report
            plan_hash,m,p,report=fixture()
            with patch.object(audit,'PLAN_SHA256',plan_hash):
                result,_=audit.run(root/'plan.json',root/'manifest.json',[root/'report.json'])
                self.assertEqual(result['funding_markets'],1)
            for mode in ('page_hash','contract','cursor','extra_market','source_hash','metrics'):
                plan_hash,m,p,report=fixture()
                if mode=='page_hash':(root/'page.json').write_text('{}')
                if mode=='contract':m['markets'][0]['contract_id']='2'
                if mode=='cursor':m['markets'][0]['pages'][0]['requested_offset']='wrong'
                if mode=='extra_market':m['markets'].append(deepcopy(m['markets'][0]))
                if mode=='source_hash':(root/'market.json').write_text('{}')
                if mode=='metrics':
                    report['metrics'][report['records'][0]['model']]['resolved']+=1
                    p['sources'][0]['report_sha256']=write('report.json',report)
                    plan_hash=write('plan.json',p);m['plan_sha256']=plan_hash
                write('manifest.json',m)
                with patch.object(audit,'PLAN_SHA256',plan_hash):
                    with self.assertRaises(ValueError):audit.run(root/'plan.json',root/'manifest.json',[root/'report.json'])


if __name__ == '__main__':
    unittest.main()
