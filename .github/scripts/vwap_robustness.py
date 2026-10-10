"""Offline extra-period diagnostic of the unchanged registered VWAP model.

No new hypothesis, future-week access, live captures, controls or orders.
"""
import argparse
from dataclasses import fields
import hashlib
import json
from pathlib import Path

import app as scanner
import historical_robustness as baseline
from analysis_terminal import vwap_reclaim_replay as vwap
from analysis_terminal import pending_entry_replay as control

ROOT=Path(__file__).resolve().parents[2]
PROTOCOL=ROOT/'.github/research/vwap_robustness_protocol.json'
PINNED_PROTOCOL_SHA256='cd68eea33d46ba97b9e9b5ac8b1aeddcb56fb0e66d8bf802fe59abc80a062d45'


def digest(raw):return hashlib.sha256(raw).hexdigest()


def protocol():
    raw=PROTOCOL.read_bytes()
    if digest(raw)!=PINNED_PROTOCOL_SHA256:raise ValueError('Registered audit changed')
    p=json.loads(raw)
    for name,expected in p['frozen_dependencies_sha256'].items():
        if digest((ROOT/name).read_bytes())!=expected:raise ValueError('Frozen VWAP dependency changed')
    if digest(baseline.PROTOCOL.read_bytes())!=p['baseline_registration_sha256']:
        raise ValueError('Control registration changed')
    return p


def run(source_dir,control_report,period_index,analyze,settings):
    p=protocol()
    if type(period_index) is not int or not 0<=period_index<len(p['periods']):
        raise ValueError('Unregistered VWAP audit period')
    period=p['periods'][period_index];source_dir=Path(source_dir).resolve()
    raw=(source_dir/'replay-report.json').read_bytes();original=Path(control_report).read_bytes()
    if digest(raw)!=period['source_report_sha256'] or digest(original)!=period['control_report_sha256']:
        raise ValueError('Original source or complete control report changed')
    b=baseline.run(source_dir,period_index,analyze,settings)
    if b!=json.loads(original):raise ValueError('Full original control report does not reproduce')
    manifest=json.loads(raw)['manifest'];records=[];markets=[];hashes=[]
    for item in manifest['sources']:
        path=(source_dir/item['file']).resolve()
        if not path.is_relative_to(source_dir):raise ValueError('Unsafe candle path')
        raw_c=path.read_bytes()
        if digest(raw_c)!=item['sha256']:raise ValueError('Candle checksum changed')
        hashes.append((path,digest(raw_c)));data=json.loads(raw_c)
        contract=scanner.Contract(**{k:v for k,v in data['contract'].items() if k in {f.name for f in fields(scanner.Contract)}})
        result=vwap.replay_market(contract,[scanner.Candle(**c) for c in data['HOUR_4']],
            [scanner.Candle(**c) for c in data['MINUTE_15']],start_ms=b['start_ms'],end_ms=b['end_ms'])
        records.extend(result.pop('records'));markets.append(result)
    if len({r['key'] for r in records})!=len(records):raise ValueError('Duplicate VWAP episode')
    if sum(m['valid_points'] for m in markets)!=b['coverage']['valid_points']:
        raise ValueError('VWAP indicator coverage does not reproduce')
    groups={m:[r for r in b['records'] if r['model']==m] for m in control.MODELS};groups[vwap.MODEL]=records
    metrics={m:control.metrics(rows) for m,rows in groups.items()}
    portfolios={m:control.portfolio(rows) for m,rows in groups.items()}
    current={(r['ticker'],r['created_ms']) for r in groups[control.MODELS[0]] if r['filled_ms'] is not None}
    proposal={(r['ticker'],r['created_ms']) for r in records if r['filled_ms'] is not None}
    comparison=dict(identity_basis='ticker + signal close time: distinct strategy setup families',
        shared_filled_setups=len(current&proposal),proposal_only_filled_setups=len(proposal-current),
        current_filled_missing_in_proposal=len(current-proposal),net_filled_count_difference=len(proposal)-len(current),
        capped_filled_count_difference=portfolios[vwap.MODEL]['filled']-portfolios[control.MODELS[0]]['filled'],
        capped_shared_setup_count=None)
    if digest((source_dir/'replay-report.json').read_bytes())!=digest(raw) or any(digest(path.read_bytes())!=h for path,h in hashes):
        raise ValueError('Original sources mutated')
    return dict(protocol=p['protocol'],dataset='RETROSPECTIVE_ADDITIONAL_DIAGNOSTIC',role=period['id'],
        start_ms=b['start_ms'],end_ms=b['end_ms'],period_complete=True,pristine_holdout=False,
        eligible_for_live_promotion=False,real_orders_enabled=False,automatic_promotion=False,changes_live_rules=False,
        protocol_sha256=PINNED_PROTOCOL_SHA256,original_vwap_protocol_sha256=digest(vwap.PROTOCOL.read_bytes()),
        original_vwap_engine_sha256=digest(Path(vwap.__file__).read_bytes()),
        source_report_sha256=digest(raw),control_report_sha256=digest(original),full_control_report_reproduced=True,
        source_files_verified=len(markets),coverage=b['coverage'],metrics=metrics,portfolios=portfolios,
        stress_metrics={m:vwap.stressed_metrics(rows) for m,rows in groups.items()},comparison=comparison,
        records=b['records']+records,markets=markets,original_future_validation_unchanged=True)


def decision(reports):
    p=protocol();expected={x['id']:(x['start_ms'],x['end_ms']) for x in p['periods']};seen=set()
    for r in reports:
        if r['role'] not in expected or r['role'] in seen or (r['start_ms'],r['end_ms'])!=expected[r['role']] or r['protocol_sha256']!=PINNED_PROTOCOL_SHA256:
            raise ValueError('Unregistered or repeated audit period')
        seen.add(r['role'])
    if any(not r['period_complete'] or r['coverage']['failed_markets'] or not r['coverage']['valid_points'] for r in reports):
        return 'BLOCKED_DATA_QUALITY'
    for r in reports:
        m=r['metrics'][vwap.MODEL]
        if m['resolved']>=20 and (m['avg_net_r']<=0 or m['profit_factor']!='INF' and m['profit_factor']<=1):
            return 'REJECTED_HISTORICAL_ROBUSTNESS_NO_LIVE_CHANGE'
    if set(expected)!=seen or any(r['metrics'][vwap.MODEL]['resolved']<50 for r in reports):
        return 'CONTINUE_INSUFFICIENT_SAMPLE'
    for r in reports:
        m,s,a,c=r['metrics'][vwap.MODEL],r['stress_metrics'][vwap.MODEL],r['portfolios'][vwap.MODEL],r['comparison']
        if not (m['win_rate']>=40 and m['avg_net_r']>0 and (m['profit_factor']=='INF' or m['profit_factor']>=1.2)
            and s['avg_net_r']>0 and (s['profit_factor']=='INF' or s['profit_factor']>1)
            and c['net_filled_count_difference']>0 and c['capped_filled_count_difference']>0
            and a['resolved']>=20 and not a['active'] and not a['uncertain']
            and a['closed_portfolio_roi_pct'] is not None and a['closed_portfolio_roi_pct']>0):
            return 'NO_QUALIFIED_REPLACEMENT'
    return 'RETROSPECTIVE_DIAGNOSTIC_POSITIVE_ORIGINAL_FUTURE_AND_LIVE_VALIDATION_STILL_REQUIRED'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True);parser.add_argument('--control-report',type=Path,required=True)
    parser.add_argument('--period-index',type=int,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    from analysis_terminal import server
    report=run(args.source,args.control_report,args.period_index,server.analyze_contract,server.SETTINGS)
    args.output.mkdir(parents=True,exist_ok=False)
    summary={k:v for k,v in report.items() if k not in {'records','markets'}};summary['decision']=decision([report])
    for name,value in [('report.json',report),('summary.json',summary)]:
        with (args.output/name).open('x') as f:json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({'period':report['role'],'decision':summary['decision'],'metrics':report['metrics'][vwap.MODEL],
                      'portfolio':report['portfolios'][vwap.MODEL],'comparison':report['comparison']}))


if __name__=='__main__':main()
