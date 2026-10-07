"""Read-only continuation diagnostics of a frozen, rejected research cohort.

No strategy search, counterfactual entries, server hooks, database or orders.
Fill/terminal candles cannot establish an ordered pre-exit excursion.
"""
from bisect import bisect_right
from collections import Counter
from dataclasses import fields
import hashlib
import json
from pathlib import Path
from statistics import median

import app as scanner
from analysis_terminal import confirmation_zone_replay as zone
from analysis_terminal.replay import complete_window
from analysis_terminal.setups import setup_identity

PROTOCOL = Path(__file__).with_name('continuation_diagnostics_protocol.json')
STEP = zone.STEP
LATENCY = (4, 16, 96)
MFE = (.5, 1.0)


def latency_bucket(bars):
    for boundary in LATENCY:
        if bars <= boundary:
            return f'<=_{boundary}_bars'
    return '>_96_bars'


def mfe_bucket(value):
    return '<_0.5' if value < MFE[0] else '[0.5,1)' if value < MFE[1] else '>=_1'


def signal_features(record, row):
    if (setup_identity(row) != record['setup_id'] or row.get('direction') != record['side']
            or record['created_ms'] != record['signal_candle_ms'] + STEP + 1
            or row.get('entry_reference') != record['trigger']
            or row.get('shadow_stop_loss') != record['stop']):
        raise ValueError('Signal identity or frozen prices mismatch')
    a4, a15 = row['atr_4h'], row['atr_15m']
    if not all(zone.positive(v) for v in (a4, a15)):
        raise ValueError('Invalid signal ATR')
    age = record['created_ms'] - 1 - (row['breakout_time_ms'] + scanner.INTERVAL_MS['HOUR_4'])
    if age < 0:
        raise ValueError('Breakout not closed at signal')
    return dict(breakout_age_hours=age/3600000,
                signal_stop_atr4=abs(record['trigger']-record['stop'])/a4,
                signal_stop_atr15=abs(record['trigger']-record['stop'])/a15,
                confirmation_roll_margin_atr=row['confirmation_roll_margin_atr'],
                confirmation_body_atr=row['confirmation_body_atr'],
                trend_spread_atr4=abs(row['ema20_4h']-row['ema50_4h'])/a4)


def diagnose(record, candles, *, end_ms):
    """Observe a contiguous prefix with neither signal, fill nor terminal extrema."""
    r = record
    out = dict(key=r['key'], setup_id=r['setup_id'], ticker=r['ticker'], side=r['side'],
               original_status=r['status'], filled=r['filled_ms'] is not None,
               elapsed_bars=None, latency_bucket=None, preterminal_bars=0,
               preterminal_mfe_price_r=None, preterminal_mae_price_r=None,
               first_adverse_roll_close_ms=None, first_equal_roll_close_ms=None,
               observation_stop=None, stop_cost_share=None)
    if not out['filled']:
        return out
    filled = r['filled_ms']
    if (r['side'] not in {'LONG','SHORT'} or filled < r['created_ms']-1 or filled % STEP or end_ms % STEP
            or r['created_ms'] != r['signal_candle_ms']+STEP+1):
        raise ValueError('Invalid frozen fill clock')
    risk = abs(r['entry']-r['stop'])
    if not zone.positive(risk) or not zone.positive(r['net_risk']):
        raise ValueError('Invalid fill risk')
    out['stop_cost_share'] = (r['net_risk']-abs(r['nominal_fill']-r['stop']))/r['net_risk']
    terminal = r['outcome_ms']
    if terminal is not None and (terminal % STEP or not filled < terminal <= end_ms):
        raise ValueError('Invalid terminal range')
    if r['status'] in {'TP', 'SL'}:
        if terminal is None or terminal % STEP or terminal <= filled:
            raise ValueError('Invalid terminal clock')
        out['elapsed_bars'] = (terminal-filled)//STEP
        out['latency_bucket'] = latency_bucket(out['elapsed_bars'])
    # DATA_GAP is timestamped at missing-bar close by the original evaluator.
    stop = min(end_ms, terminal-STEP) if terminal is not None else end_ms
    out['observation_stop'] = 'BEFORE_TERMINAL_BAR' if terminal is not None else 'PERIOD_END_CENSORED'
    series = {}
    for c in candles:
        if c.time_ms in series:
            raise ValueError('Duplicate candle time')
        series[c.time_ms] = c
    favorable = adverse = 0.0
    long = r['side'] == 'LONG'
    for stamp in range(filled+STEP, stop, STEP):
        c = series.get(stamp)
        if c is None:
            out['observation_stop'] = 'DATA_GAP'
            break
        if c.time_ms+STEP > end_ms:
            break
        if (not all(zone.positive(v) for v in (c.open,c.high,c.low,c.close))
                or not c.low <= min(c.open,c.close) <= max(c.open,c.close) <= c.high):
            raise ValueError('Invalid diagnostic OHLC')
        favorable = max(favorable, (c.high-r['entry'] if long else r['entry']-c.low)/risk)
        adverse = max(adverse, (r['entry']-c.low if long else c.high-r['entry'])/risk)
        if (c.close < r['roll_level'] if long else c.close > r['roll_level']):
            if out['first_adverse_roll_close_ms'] is None:
                out['first_adverse_roll_close_ms'] = stamp+STEP
        if c.close == r['roll_level'] and out['first_equal_roll_close_ms'] is None:
            out['first_equal_roll_close_ms'] = stamp+STEP
        out['preterminal_bars'] += 1
    if out['preterminal_bars']:
        out['preterminal_mfe_price_r'], out['preterminal_mae_price_r'] = favorable, adverse
    return out


def summarize(records):
    filled = [r for r in records if r['filled']]
    statuses = Counter(r['original_status'] for r in records)
    groups = {}
    for status in sorted(statuses):
        rows = [r for r in filled if r['original_status'] == status]
        path = [r for r in rows if r['preterminal_mfe_price_r'] is not None]
        features = [r['signal_features'] for r in rows]
        groups[status] = dict(filled=len(rows),
            latency_bins={k:sum(r['latency_bucket']==k for r in rows)
                          for k in ('<=_4_bars','<=_16_bars','<=_96_bars','>_96_bars')},
            elapsed_bars_median=median(r['elapsed_bars'] for r in rows) if rows and status in {'TP','SL'} else None,
            with_preterminal_path=len(path), without_preterminal_path=len(rows)-len(path),
            preterminal_mfe_bins={k:sum(mfe_bucket(r['preterminal_mfe_price_r'])==k for r in path)
                                 for k in ('<_0.5','[0.5,1)','>=_1')},
            adverse_roll_close_before_terminal=sum(r['first_adverse_roll_close_ms'] is not None for r in rows),
            equal_roll_close_before_terminal=sum(r['first_equal_roll_close_ms'] is not None for r in rows),
            median_stop_cost_share=median(r['stop_cost_share'] for r in rows) if rows else None,
            median_signal_features={k:median(f[k] for f in features) if features else None for k in
                ('breakout_age_hours','signal_stop_atr4','signal_stop_atr15',
                 'confirmation_roll_margin_atr','confirmation_body_atr','trend_spread_atr4')})
    return dict(candidates=len(records),filled=len(filled),statuses=dict(statuses),groups=groups,
                observation_stops=dict(Counter(r['observation_stop'] for r in filled)),
                unique_tickers=len({r['ticker'] for r in filled}),
                unfilled_excluded_from_path=len(records)-len(filled),
                terminal_candle_excluded=True,fill_candle_excluded=True)


def build(source_dir, report_path, analyze, settings, *, baseline_path=None):
    protocol_bytes = PROTOCOL.read_bytes()
    protocol = json.loads(protocol_bytes)
    original = report_path.read_bytes()
    ledger = json.loads(original)
    registered = protocol['periods'].get(ledger['role'])
    if (not registered or hashlib.sha256(original).hexdigest()!=registered['report_sha256']
            or (ledger['start_ms'],ledger['end_ms'])!=(registered['start_ms'],registered['end_ms'])
            or tuple(protocol['diagnostic_rules']['elapsed_bars_boundaries'])!=LATENCY
            or tuple(protocol['diagnostic_rules']['preterminal_mfe_price_r_boundaries'])!=MFE):
        raise ValueError('Unregistered cohort or diagnostic definitions')
    reproduced = zone.build(source_dir,analyze,settings,role=ledger['role'],baseline_path=baseline_path)
    if reproduced != ledger or report_path.read_bytes()!=original:
        raise ValueError('Original research ledger did not reproduce exactly')
    source = json.loads((source_dir/'replay-report.json').read_text())
    grouped = {}
    for r in ledger['records']:
        if r['model'] == zone.MODEL:
            grouped.setdefault(r['ticker'],[]).append(r)
    records = []
    for item in source['manifest']['sources']:
        if item['ticker'] not in grouped:
            continue
        raw = (source_dir/item['file']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:
            raise ValueError('Source changed after replay verification')
        data = json.loads(raw)
        allowed = {f.name for f in fields(scanner.Contract)}
        contract = scanner.Contract(**{k:v for k,v in data['contract'].items() if k in allowed})
        frames = {i:[scanner.Candle(**c) for c in data[i]] for i in ('HOUR_4','MINUTE_15')}
        for r in grouped[item['ticker']]:
            windows = {i:complete_window(cs,bisect_right([c.time_ms+scanner.INTERVAL_MS[i] for c in cs],r['created_ms']),
                       180,scanner.INTERVAL_MS[i],r['created_ms']) for i,cs in frames.items()}
            if any(w is None for w in windows.values()):
                raise ValueError('Incomplete signal window')
            row = analyze(contract,windows['HOUR_4'],windows['MINUTE_15'],as_of_ms=r['created_ms'])
            observation = diagnose(r,frames['MINUTE_15'],end_ms=ledger['end_ms'])
            observation['signal_features'] = signal_features(r,row)
            records.append(observation)
    if len(records)!=ledger['metrics'][zone.MODEL]['candidates']:
        raise ValueError('Missing cohort diagnostic')
    return dict(protocol=protocol['protocol'],dataset='DESCRIPTIVE_VIEWED_FROZEN_COHORT',role=ledger['role'],
                start_ms=ledger['start_ms'],end_ms=ledger['end_ms'],
                protocol_sha256=hashlib.sha256(protocol_bytes).hexdigest(),
                engine_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                source_report_sha256=hashlib.sha256(original).hexdigest(),
                source_files_verified=reproduced['source_files_verified'],
                original_metrics=ledger['metrics'][zone.MODEL],original_portfolio=ledger['portfolios'][zone.MODEL],
                summary=summarize(records),records=records,decision=protocol['decision'],
                changes_live_rules=False,real_orders_enabled=False,automatic_promotion=False,eligible_for_live_promotion=False,
                limitations=['Preterminal excursions are observable lower bounds, not full ordered MFE before exit.',
                            'Fill and terminal candles excluded; absent path is unknown, not zero excursion.',
                            'OPEN is right-censored and its diagnostics are not resolved profitability.',
                            'Previously viewed periods and correlated markets; no causal or untouched validation claim.',
                            'No selected subgroups, new entries, parameter tuning, counterfactual profits or ROI.'])


def main():
    import argparse
    from analysis_terminal import server
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source','report','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--baseline',type=Path)
    args = parser.parse_args()
    if args.output.resolve() in {args.report.resolve(),(args.source/'replay-report.json').resolve()} or (
            args.baseline and args.output.resolve()==args.baseline.resolve()):
        raise ValueError('Do not overwrite source ledgers')
    result = build(args.source,args.report,server.analyze_contract,server.SETTINGS,baseline_path=args.baseline)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'role':result['role'],'summary':result['summary']}))


if __name__ == '__main__':
    main()
