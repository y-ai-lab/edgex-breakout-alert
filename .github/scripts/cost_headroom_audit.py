"""Descriptive omitted-cost budget for resolved research trades, never a reprice.

A uniform extra total charge is expressed in bps of each frozen entry notional.
This is not Funding history, a fee estimate, a stress portfolio or account ROI.
Caller validates chronology and source provenance before using this audit.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys

from analysis_terminal import pending_entry_replay as study


def budget(numerator, denominator, resolved):
    if (not math.isfinite(numerator) or not math.isfinite(denominator)
            or denominator < 0 or resolved and denominator <= 0):
        raise ValueError('Invalid aggregate cost economics')
    bps = 10000*(numerator/denominator) if resolved and numerator > 0 else None
    if bps is not None and not math.isfinite(bps):
        raise ValueError('Unrepresentable aggregate cost budget')
    return dict(resolved=resolved,
                status='NO_RESOLVED_TRADES' if not resolved else
                       'BASELINE_NONPOSITIVE' if numerator <= 0 else 'POSITIVE_RESOLVED_SURPLUS',
                additional_uniform_charge_break_even_bps=bps)


def summarize(records, expected_metrics, expected_portfolio):
    engine = Path(study.__file__)
    frozen = json.loads(engine.with_name('pending_live_protocol.json').read_text())['frozen_engine_sha256']
    if hashlib.sha256(engine.read_bytes()).hexdigest() != frozen:
        raise ValueError('Cost headroom audit requires the frozen research engine')
    if sys.gettrace() is not None:
        raise ValueError('Do not replace an existing execution tracer')
    if len({r['key'] for r in records}) != len(records):
        raise ValueError('Duplicate cost headroom record')
    before = deepcopy(records)
    resolved = [r for r in records if r['status'] in {'TP', 'SL'}]
    for r in resolved:
        if (any(type(r[k]) is not int or r[k] % study.STEP for k in ('filled_ms','outcome_ms'))
                or r['outcome_ms'] <= r['filled_ms']
                or any(type(r.get(k)) not in (int, float) or not math.isfinite(r[k])
                       for k in ('entry', 'net_risk', 'net_pnl_per_unit', 'final_net_r'))
                or r['entry'] <= 0 or r['net_risk'] <= 0
                or not math.isfinite(r['entry']/r['net_risk'])
                or not math.isclose(r['final_net_r'], r['net_pnl_per_unit']/r['net_risk'],
                                    rel_tol=1e-12, abs_tol=1e-12)):
            raise ValueError('Invalid resolved cost economics or chronology')
    if study.metrics(records) != expected_metrics:
        raise ValueError('Original cost metrics do not reproduce')
    orders = {}

    def trace(frame, event, arg):
        nonlocal orders
        if frame.f_code is not study.portfolio.__code__:
            return None
        if event == 'return':
            orders = deepcopy(frame.f_locals['admitted'])
        return trace

    try:
        sys.settrace(trace)
        observed = study.portfolio(records)
    finally:
        sys.settrace(None)
    if (observed != expected_portfolio or observed != study.portfolio(records)
            or records != before or hashlib.sha256(engine.read_bytes()).hexdigest() != frozen):
        raise ValueError('Original portfolio or frozen cost inputs changed')
    capped = [(r, orders[r['key']]) for r in resolved
              if r['key'] in orders and 'net_pnl_usdc' in orders[r['key']]]
    pnl = sum(o['net_pnl_usdc'] for _, o in capped)
    notional = sum(o['quantity']*r['entry'] for r, o in capped)
    if (len(capped) != observed['resolved'] or
            not math.isclose(pnl, observed['realized_net_pnl_usdc'], rel_tol=1e-12, abs_tol=1e-9)):
        raise ValueError('Resolved portfolio cost accounting mismatch')
    return dict(verified=True, engine_sha256=frozen,
                resolved_r_basket=budget(sum(r['final_net_r'] for r in resolved),
                    sum(r['entry']/r['net_risk'] for r in resolved), len(resolved)),
                capped_resolved_cash_basket=budget(pnl, notional, len(capped)),
                open_records=sum(r['status']=='OPEN' for r in records),
                blocked_records=sum(r['status'] in {'AMBIGUOUS', 'DATA_GAP'} for r in records),
                original_portfolio_roi_pct=observed['closed_portfolio_roi_pct'],
                portfolio_active=observed['active'], portfolio_uncertain=observed['uncertain'],
                extra_charge_basis='TOTAL_ADDITIONAL_ENTRY_NOTIONAL_BPS_PER_RESOLVED_TRADE',
                r_basis='ORIGINAL_FROZEN_NET_STOP_RISK', new_samples_added=False,
                actual_funding_included=False, changes_metrics_or_policy=False,
                limitations=['Only already-resolved trades; OPEN/uncertain costs and P&L are unknown.',
                    'R-basket uses equal original risk units; cash-basket uses original admitted quantities.',
                    'Uniform extra charge is descriptive, not actual Funding, future cost tolerance or ROI.',
                    'No rate, holding-time, side, mark-price, queue or funding-settlement history is inferred.',
                    'Do not change costs, stop risk, allocations, decisions or rules using this budget.'])
