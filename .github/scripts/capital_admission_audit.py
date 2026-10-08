"""Observe frozen research admission decisions; never change a portfolio policy.

Use only after the source ledger has passed chronology/provenance validation.
Return aggregate diagnostics, without copying ticker/setup/account records.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

from analysis_terminal import pending_entry_replay as study


def summarize(records, expected_portfolio):
    engine = Path(study.__file__)
    frozen = json.loads(engine.with_name('pending_live_protocol.json').read_text())['frozen_engine_sha256']
    if hashlib.sha256(engine.read_bytes()).hexdigest() != frozen:
        raise ValueError('Capital admission audit requires the original frozen engine')
    if sys.gettrace() is not None:
        raise ValueError('Do not replace an existing execution tracer')
    if len({r['key'] for r in records}) != len(records):
        raise ValueError('Duplicate capital admission record')
    before = deepcopy(records)
    lines = engine.read_text().splitlines()
    markers = [i+1 for i,line in enumerate(lines) if line.strip().startswith('if len(active)>=3')]
    if len(markers) != 1:
        raise ValueError('Frozen admission observation point changed')
    previous, contexts, exclusions, admitted = Counter(), {}, [], {}

    def trace(frame, event, arg):
        nonlocal previous, admitted
        if frame.f_code is not study.portfolio.__code__:
            return None
        local = frame.f_locals
        if event == 'line':
            counts = Counter(local.get('excluded', {}))
            for reason,increase in (counts-previous).items():
                if increase != 1 or local['key'] not in contexts:
                    raise ValueError('Unrecognized frozen exclusion event')
                exclusions.append((local['key'],reason,local['kind'],contexts[local['key']]))
            previous = counts
            if frame.f_lineno == markers[0]:
                r,active,cash = local['r'],local['active'],local['cash']
                levels = study.cost_levels(r['trigger'],r['stop'],r['target'],r['side'])
                notional = max(0,cash-sum(o['notional'] for o in active.values()))
                risk = max(0,min(cash*.01,cash*.03-sum(o['risk'] for o in active.values())))
                minimum = r.get('min_order_size')
                contexts[r['key']] = dict(
                    unfilled_reservations=any(not o['filled'] for o in active.values()),
                    filled_reservations=any(o['filled'] for o in active.values()),
                    capacity_full=len(active)>=3,
                    same_ticker=any(o['ticker']==r['ticker'] for o in active.values()),
                    minimum_notional_above_remaining=bool(minimum and minimum*levels['entry']>notional),
                    minimum_risk_above_remaining=bool(minimum and minimum*levels['net_risk']>risk))
        elif event == 'return':
            admitted = deepcopy(local['admitted'])
        return trace

    try:
        sys.settrace(trace)
        observed = study.portfolio(records)
    finally:
        sys.settrace(None)
    # Keep every source field, including null ROI and uncertain reservations.
    if observed != expected_portfolio or observed != study.portfolio(records):
        raise ValueError('Original capital portfolio does not reproduce')
    if records != before or hashlib.sha256(engine.read_bytes()).hexdigest() != frozen:
        raise ValueError('Frozen inputs or engine changed during admission audit')
    if (len(contexts)!=len(records) or len(admitted)!=observed['admitted'] or
            sum(o['filled'] for o in admitted.values())!=observed['filled'] or
            Counter(reason for _,reason,_,_ in exclusions)!=Counter(observed['exclusions']) or
            len(admitted)+sum(kind=='CREATE' for _,_,kind,_ in exclusions)!=len(records)):
        raise ValueError('Admission event accounting mismatch')
    by_key = {r['key']:r for r in records}
    lost = {r['key'] for r in records if r['filled_ms'] is not None and not admitted.get(r['key'],{}).get('filled')}
    if (len(lost)!=sum(r['filled_ms'] is not None for r in records)-observed['filled'] or
            not lost <= {key for key,_,_,_ in exclusions}):
        raise ValueError('Unexplained capital omission of a filled record')
    reasons = {}
    for reason in observed['exclusions']:
        subset = [(key,context) for key,why,_,context in exclusions if why==reason]
        filled = [(key,context) for key,context in subset if by_key[key]['filled_ms'] is not None]
        reasons[reason] = dict(
            excluded_candidates=len(subset),
            excluded_with_uncapped_fill=len(filled),
            excluded_without_uncapped_fill=len(subset)-len(filled),
            lost_filled_with_unfilled_reservations=sum(c['unfilled_reservations'] for _,c in filled),
            lost_filled_with_filled_reservations=sum(c['filled_reservations'] for _,c in filled),
            lost_filled_capacity_full=sum(c['capacity_full'] for _,c in filled),
            lost_filled_same_ticker=sum(c['same_ticker'] for _,c in filled),
            lost_filled_minimum_notional_above_remaining=sum(c['minimum_notional_above_remaining'] for _,c in filled),
            lost_filled_minimum_risk_above_remaining=sum(c['minimum_risk_above_remaining'] for _,c in filled))
    return dict(verified=True,engine_sha256=frozen,
                candidates=len(records),uncapped_filled=sum(r['filled_ms'] is not None for r in records),
                capped_filled=observed['filled'],filled_omitted=len(lost),reasons=reasons,
                retained_filled_statuses=dict(Counter(by_key[k]['status'] for k,o in admitted.items() if o['filled'])),
                capped_shared_setup_count=None,
                new_samples_added=False,actual_execution_evidence=False,
                policy='FROZEN_10000_USDC_1PCT_MAX3_TOTAL3PCT_1X_JST_DAILY3PCT',
                limitations=['Same ledger and original portfolio, not a new strategy or causal experiment.',
                             'Exclusions without an uncapped fill are not lost trades.',
                             'Reservation and affordability diagnostics overlap; do not sum them as extra exclusions.',
                             'This frozen research policy is different from the actual operating account policy.'])
