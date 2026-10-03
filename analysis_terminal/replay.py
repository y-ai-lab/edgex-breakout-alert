"""Isolated retrospective replay. No database, notification or promotion writes."""
from bisect import bisect_right
from collections import Counter
import hashlib
import inspect
import json
from typing import Any, Callable

import app as scanner
from analysis_terminal.comparison import strategy_comparison
from analysis_terminal.outcomes import evaluate_paper_signal
from analysis_terminal.setups import setup_identity


PARAMETER_NAMES = ("monitor_interval", "entry_interval", "trend_fast_ema", "trend_slow_ema", "atr_period",
                   "roll_lookback", "roll_max_age", "retest_lookback", "atr_stop_buffer", "atr_target_buffer",
                   "retest_atr_tolerance", "min_rr")


def strategy_parameters(settings) -> dict[str, Any]:
    return {name: getattr(settings, name) for name in PARAMETER_NAMES}


def rule_fingerprint(analyze: Callable, settings) -> str:
    source = inspect.getsource(analyze)
    return hashlib.sha256((source+json.dumps(strategy_parameters(settings), sort_keys=True)).encode()).hexdigest()


def complete_window(candles: list[scanner.Candle], end: int, count: int, step: int, as_of: int) -> list[scanner.Candle] | None:
    window = candles[max(0, end-count):end]
    if len(window) != count or window[-1].time_ms != (as_of//step)*step-step:
        return None
    if any(b.time_ms-a.time_ms != step for a, b in zip(window, window[1:])):
        return None
    return window


def replay_contract(contract: scanner.Contract, monitor: list[scanner.Candle], entries: list[scanner.Candle],
                    analyze: Callable, *, start_ms: int, end_ms: int, monitor_interval: str = "HOUR_4",
                    entry_interval: str = "MINUTE_15", monitor_window: int = 180, entry_window: int = 180,
                    setup_max_age: int = 6, retest_lookback: int = 4) -> dict[str, Any]:
    step, monitor_step = scanner.INTERVAL_MS[entry_interval], scanner.INTERVAL_MS[monitor_interval]
    if start_ms <= 0 or end_ms <= start_ms or start_ms % step or end_ms % step or min(monitor_window, entry_window, setup_max_age, retest_lookback) < 1:
        raise ValueError("Invalid replay configuration")
    for series, interval in ((monitor, monitor_interval), (entries, entry_interval)):
        if any(c.contract_id != contract.contract_id or c.interval != interval for c in series):
            raise ValueError("Replay candle identity mismatch")
        if any(a.time_ms >= b.time_ms for a, b in zip(series, series[1:])):
            raise ValueError("Replay requires ordered unique candles")
    times4 = [c.time_ms+monitor_step for c in monitor]
    times15 = [c.time_ms+step for c in entries]
    # Observe old setups before the reporting window so later confirmations do
    # not masquerade as a first entry after the left boundary.
    scan_start = start_ms-setup_max_age*monitor_step-retest_lookback*step
    seen = {"current": set(), "shadow": set()}
    signals = {"current": [], "shadow": []}
    stages = Counter()
    exclusions = Counter()
    warmup_first_entries = Counter()
    last_ms = (end_ms//step)*step
    expected_points = len(range(start_ms, last_ms, step))
    valid_points = 0
    uncertain_through = 0
    for close_ms in range(scan_start, last_ms, step):
        as_of = close_ms+1
        n4, n15 = bisect_right(times4, as_of), bisect_right(times15, as_of)
        window4 = complete_window(monitor, n4, monitor_window, monitor_step, as_of)
        window15 = complete_window(entries, n15, entry_window, step, as_of)
        report_point = close_ms >= start_ms
        if window4 is None or window15 is None:
            uncertain_through = as_of
            if report_point:
                exclusions["incomplete_indicator_window"] += 1
            continue
        row = analyze(contract, window4, window15, as_of_ms=as_of)
        if report_point:
            valid_points += 1
            stages[str(row.get("stage") or "DATA_WAIT")] += 1
        identity = setup_identity(row)
        if not identity:
            continue
        for model, ready in (("current", row.get("stage") == "READY"), ("shadow", row.get("shadow_v2_ready") is True)):
            if not ready or identity in seen[model]:
                continue
            seen[model].add(identity)
            if int(row["breakout_time_ms"])+monitor_step <= uncertain_through:
                if report_point:
                    exclusions["unknown_first_entry_"+model] += 1
                continue
            if not report_point:
                warmup_first_entries[model] += 1
                continue
            stop = row.get("stop_loss") if model == "current" else row.get("shadow_stop_loss")
            target = row.get("take_profit") if model == "current" else row.get("shadow_v2_target")
            signal = dict(key=f"replay-{model}:{identity}", setup_id=identity, ticker=contract.contract_name,
                          side=row["direction"], breakout_time_ms=row["breakout_time_ms"], breakout_level=row["breakout_level"],
                          entry=row["entry_reference"], stop=stop, target=target,
                          signal_candle_ms=window15[-1].time_ms, created_ms=as_of,
                          dataset="RETROSPECTIVE", model=model)
            if model == "current":
                signal["tp1"] = row.get("tp1_2r")
            else:
                signal["extension_target"] = row.get("shadow_v2_extension_target")
                signal["room_rr"] = row.get("shadow_v2_room_rr")
            signal["result"] = evaluate_paper_signal(signal, entries, interval_ms=step, now_ms=end_ms)
            signals[model].append(signal)
    return dict(ticker=contract.contract_name, expected_points=expected_points, valid_points=valid_points,
                excluded_points=dict(exclusions), stage_counts=dict(stages), warmup_first_entries=dict(warmup_first_entries),
                current=signals["current"], shadow=signals["shadow"])


def replay_report(results: list[dict[str, Any]], *, start_ms: int, end_ms: int, manifest: dict[str, Any]) -> dict[str, Any]:
    current = [s for r in results for s in r["current"]]
    shadow = [s for r in results for s in r["shadow"]]
    return dict(dataset="RETROSPECTIVE", eligible_for_live_promotion=False, start_ms=start_ms, end_ms=end_ms,
                manifest=manifest, comparison=strategy_comparison(current, shadow, limit=50),
                coverage=dict(expected_points=sum(r["expected_points"] for r in results),
                              valid_points=sum(r["valid_points"] for r in results)),
                markets=[{k:v for k,v in r.items() if k not in {"current", "shadow"}} for r in results],
                signals=dict(current=current, shadow=shadow),
                limitations=["Retrospective historical candles may have been revised; not captured live observations.",
                             "Universe is today's tradable contracts: survivorship and listing bias remain.",
                             "Fixed 180-bar indicator windows match the observed public WebSocket seed, not every historical snapshot.",
                             "Full contiguous indicator windows are required; excluded periods and failed markets can bias the sample.",
                             "Mean R excludes fees, slippage, funding and execution feasibility.",
                             "Same setup can enter at different times/prices under the two rules.",
                             "Retrospective samples never count toward the live Shadow promotion gate."])
