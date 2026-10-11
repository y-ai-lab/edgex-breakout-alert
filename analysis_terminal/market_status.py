"""Read-only scan availability; never changes strategy eligibility or outcomes."""


def required_history(settings):
    return {
        settings.monitor_interval: max(settings.trend_slow_ema,
            settings.roll_lookback + settings.roll_max_age, settings.atr_period + 1),
        settings.entry_interval: max(settings.atr_period + 1, settings.retest_lookback),
    }


def missing_snapshot_contracts(contract_ids, data, intervals):
    # A received 4H stream does not establish that the same market has 15M data.
    return [cid for cid in contract_ids if any(not data.get((cid, i)) for i in intervals)]


def describe_scan(contracts, rows, snapshots, settings, interval_ms, *, now_ms):
    requirements = required_history(settings)
    evaluated = {r['ticker'] for r in rows if r.get('current_price') is not None}
    waiting = []
    for cid, contract in contracts.items():
        if contract.contract_name in evaluated:
            continue
        counts = {i: sum(c.time_ms + interval_ms[i] <= now_ms
                         for c in snapshots.get((cid, i), [])) for i in requirements}
        missing = [i for i in requirements if not snapshots.get((cid, i))]
        short = [i for i, required in requirements.items() if counts[i] < required and i not in missing]
        reasons = (['MISSING_SNAPSHOT'] if missing else []) + (['INSUFFICIENT_HISTORY'] if short else [])
        if not reasons:
            reasons = ['ANALYSIS_UNAVAILABLE']
        waiting.append(dict(ticker=contract.contract_name, reasons=reasons,
                            closed_candles=counts, missing_intervals=missing,
                            insufficient_intervals=short))
    total = len(contracts)
    return dict(version=1, checked=total, universe=total, checked_pct=100.0 if total else 0.0,
                analysis_ready=total-len(waiting), data_wait_count=len(waiting),
                analysis_coverage_pct=round(100*(total-len(waiting))/total, 1) if total else 0.0,
                required_closed_candles=requirements,
                missing_snapshot_markets=sum('MISSING_SNAPSHOT' in r['reasons'] for r in waiting),
                insufficient_history_markets=sum('INSUFFICIENT_HISTORY' in r['reasons'] for r in waiting),
                data_wait=sorted(waiting, key=lambda r: r['ticker']))
