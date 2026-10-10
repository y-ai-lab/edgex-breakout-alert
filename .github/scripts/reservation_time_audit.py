"""Observe capital-time in the frozen portfolio; never allocate or release funds.

Caller validates the source ledger first. Event times are the original research
cash-booking times, including fills booked at bar close, not exchange timestamps.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys

from analysis_terminal import pending_entry_replay as study

GROUPS = ('PENDING', 'FILLED', 'UNCERTAIN')
HOUR = 3600000


def summarize(records, expected_portfolio, *, start_ms, end_ms):
    if (any(type(t) is not int or t % study.STEP for t in (start_ms, end_ms))
            or not start_ms < end_ms):
        raise ValueError('Invalid reservation observation window')
    if len({r['key'] for r in records}) != len(records):
        raise ValueError('Duplicate reservation record')
    for r in records:
        if not start_ms <= r['created_ms']-1 < end_ms:
            raise ValueError('Reservation starts outside the observed period')
        for field in ('filled_ms', 'outcome_ms'):
            stamp = r[field]
            if stamp is not None and (type(stamp) is not int or stamp % study.STEP
                    or not r['created_ms']-1 <= stamp <= end_ms
                    or field == 'filled_ms' and stamp >= end_ms):
                raise ValueError('Reservation event outside the observed period')
    engine = Path(study.__file__)
    frozen = json.loads(engine.with_name('pending_live_protocol.json').read_text())['frozen_engine_sha256']
    if hashlib.sha256(engine.read_bytes()).hexdigest() != frozen:
        raise ValueError('Reservation audit requires the original frozen engine')
    if sys.gettrace() is not None:
        raise ValueError('Do not replace an existing execution tracer')
    marker = [i+1 for i, line in enumerate(engine.read_text().splitlines())
              if line.strip() == 'for stamp,_,key,kind,r in sorted(events):']
    if len(marker) != 1:
        raise ValueError('Frozen reservation event boundary changed')
    before = deepcopy(records)
    snapshots = []

    def trace(frame, event, arg):
        if frame.f_code is not study.portfolio.__code__:
            return None
        local = frame.f_locals
        # The loop head observes the previous event after all branches, including
        # exclusions that continue before the frozen cash/drawdown bookkeeping.
        if event == 'line' and frame.f_lineno == marker[0] and 'stamp' in local:
            values = {group: dict(slots=0, risk=0.0, notional=0.0) for group in GROUPS}
            for key, order in local['active'].items():
                group = 'UNCERTAIN' if key in local['unresolved'] else 'FILLED' if order['filled'] else 'PENDING'
                values[group]['slots'] += 1
                for field in ('risk', 'notional'):
                    amount = order[field]
                    if not math.isfinite(amount) or amount < 0:
                        raise ValueError('Invalid observed reservation')
                    values[group][field] += amount
            snapshots.append((local['stamp'], values))
        return trace

    try:
        sys.settrace(trace)
        observed = study.portfolio(records)
    finally:
        sys.settrace(None)
    if observed != expected_portfolio or observed != study.portfolio(records):
        raise ValueError('Original reservation portfolio does not reproduce')
    if records != before or hashlib.sha256(engine.read_bytes()).hexdigest() != frozen:
        raise ValueError('Frozen reservation inputs or engine changed')
    expected_events = len(records) + sum(r['filled_ms'] is not None for r in records)
    expected_events += sum(r['outcome_ms'] is not None for r in records)
    if len(snapshots) != expected_events:
        raise ValueError('Reservation event accounting mismatch')
    hours = (end_ms-start_ms)/HOUR
    sums = {group: dict(slot_hours=0.0, risk_usdc_hours=0.0, notional_usdc_hours=0.0)
            for group in GROUPS}
    occupied_hours = 0.0
    for i, (stamp, values) in enumerate(snapshots):
        next_stamp = snapshots[i+1][0] if i+1 < len(snapshots) else end_ms
        elapsed = (min(end_ms, next_stamp)-max(start_ms, stamp))/HOUR
        if elapsed < 0:
            raise ValueError('Reservation events moved backward')
        if any(value['slots'] for value in values.values()):
            occupied_hours += elapsed
        for group, value in values.items():
            sums[group]['slot_hours'] += value['slots']*elapsed
            sums[group]['risk_usdc_hours'] += value['risk']*elapsed
            sums[group]['notional_usdc_hours'] += value['notional']*elapsed
    for group, value in sums.items():
        value.update(time_weighted_average_slots=value['slot_hours']/hours,
                     time_weighted_average_risk_usdc=value['risk_usdc_hours']/hours,
                     time_weighted_average_notional_usdc=value['notional_usdc_hours']/hours)
    total_notional = sum(value['notional_usdc_hours'] for value in sums.values())
    return dict(verified=True, engine_sha256=frozen, observation_start_ms=start_ms,
                observation_end_ms=end_ms, observation_hours=hours,
                event_count=len(snapshots), occupied_hours=occupied_hours,
                groups=sums,
                pending_share_of_reserved_notional_time_pct=(100*(sums['PENDING']['notional_usdc_hours']/total_notional)
                                                            if total_notional else None),
                end_counts={group: snapshots[-1][1][group]['slots'] if snapshots else 0 for group in GROUPS},
                time_basis='FROZEN_PORTFOLIO_EVENT_BOOKING',
                new_samples_added=False, actual_execution_evidence=False,
                changes_policy=False, unknown_capital_released=False,
                limitations=['Reservation-time is descriptive, not a causal estimate of additional fills or profit.',
                             'Fill reservations switch at the original bar-close booking time; intrabar order is unknown.',
                             'PENDING, filled and uncertain capital remain reserved exactly as in the original portfolio.',
                             'No policy tuning, alternate portfolio, realized ROI inference or candidate ranking.'])
