"""Read-only comparisons of recorded first entries; no strategy decisions."""
from __future__ import annotations

import math
from statistics import mean
from typing import Any

from analysis_terminal.outcomes import verified_result
from analysis_terminal.setups import first_per_setup, setup_identity


def finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def resolved_result(signal: dict[str, Any]) -> bool:
    result = signal.get("result") or {}
    r = finite_number(result.get("final_r"))
    return bool(verified_result(result) and r is not None and
                ((result.get("status") == "TP" and r > 0) or
                 (result.get("status") == "SL" and r < 0)))


def cohort(signals: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    identified = [s for s in signals if s.get("setup_id")]
    valid = [s for s in identified
             if setup_identity(dict(s, direction=s.get("side"))) == s["setup_id"]
             and (finite_number(s.get("created_ms")) or 0) > 0]
    # A tie cannot favor a profitable duplicate; use a stable record-key order.
    first = first_per_setup(sorted(valid, key=lambda s: str(s.get("key") or "")))
    return first, dict(records=len(signals), legacy_unidentified=len(signals)-len(identified),
                       invalid_identity=len(identified)-len(valid), duplicates=len(valid)-len(first))


def metrics(signals: list[dict[str, Any]]) -> dict[str, Any]:
    resolved = [s for s in signals if resolved_result(s)]
    rs = [float(s["result"]["final_r"]) for s in resolved]
    tp = sum(s["result"]["status"] == "TP" for s in resolved)
    gross_win = sum(max(r, 0) for r in rs)
    gross_loss = -sum(min(r, 0) for r in rs)
    pf = gross_win / gross_loss if gross_loss else "INF" if gross_win else None
    avg = mean(rs) if rs else None
    excursions = {}
    for name in ("mfe_r", "mae_r"):
        values = [finite_number(s["result"].get(name)) for s in resolved]
        values = [v for v in values if v is not None and v >= 0]
        excursions["avg_" + name] = round(mean(values), 4) if values else None
        excursions[name + "_samples"] = len(values)
    ordered = [s for s in resolved if (finite_number(s["result"].get("outcome_time_ms")) or 0) > 0]
    ordered.sort(key=lambda s: (float(s["result"]["outcome_time_ms"]), int(s["created_ms"]), str(s.get("key") or "")))
    longest = streak = 0
    for signal in ordered:
        streak = streak + 1 if signal["result"]["status"] == "SL" else 0
        longest = max(longest, streak)
    return dict(signals=len(signals), resolved=len(resolved), tp=tp, sl=len(resolved)-tp,
                open=sum((s.get("result") or {}).get("status", "OPEN") in {"OPEN", "TP1"} for s in signals),
                ambiguous=sum((s.get("result") or {}).get("status") == "AMBIGUOUS" for s in signals),
                unverified_results=sum(bool(s.get("result")) and not verified_result(s["result"]) for s in signals),
                invalid_resolved_results=sum(verified_result(s.get("result")) and
                    (s.get("result") or {}).get("status") in {"TP", "SL"} and not resolved_result(s) for s in signals),
                win_rate=round(tp/len(resolved)*100, 2) if resolved else None,
                avg_r=round(avg, 4) if avg is not None else None,
                expectancy_r=round(avg, 4) if avg is not None else None,
                profit_factor=round(pf, 4) if isinstance(pf, float) else pf,
                max_consecutive_losses=longest if ordered else None,
                streak_samples=len(ordered), minimum_resolved=20,
                sample_status="SUFFICIENT SAMPLE" if len(resolved) >= 20 else "INSUFFICIENT SAMPLE",
                **excursions)


def strategy_comparison(current_records: list[dict[str, Any]], shadow_records: list[dict[str, Any]],
                        limit: int = 50) -> dict[str, Any]:
    current, current_excluded = cohort(current_records)
    shadow, shadow_excluded = cohort(shadow_records)
    c = {s["setup_id"]: s for s in current}
    v = {s["setup_id"]: s for s in shadow}
    both = c.keys() & v.keys()
    current_only, shadow_only = c.keys()-v.keys(), v.keys()-c.keys()
    paired = sorted(k for k in both if resolved_result(c[k]) and resolved_result(v[k]))
    deltas = [float(v[k]["result"]["final_r"])-float(c[k]["result"]["final_r"]) for k in paired]

    def brief(signal):
        if signal is None:
            return None
        return {key: signal.get(key) for key in ("key", "created_ms", "entry", "stop", "target", "result")}

    ids = sorted(c.keys() | v.keys(), key=lambda k: (-min(s["created_ms"] for s in (c.get(k), v.get(k)) if s), k))
    return dict(sample_basis="unique_setup_first_recorded_entry_all_history", automatic_promotion=False,
                current=metrics(current), shadow=metrics(shadow),
                exclusions=dict(current=current_excluded, shadow=shadow_excluded),
                groups=dict(current_only=dict(setups=len(current_only), current=metrics([c[k] for k in current_only])),
                            shadow_only=dict(setups=len(shadow_only), shadow=metrics([v[k] for k in shadow_only])),
                            both=dict(setups=len(both), current=metrics([c[k] for k in both]), shadow=metrics([v[k] for k in both]))),
                paired=dict(resolved=len(paired), current=metrics([c[k] for k in paired]), shadow=metrics([v[k] for k in paired]),
                            avg_delta_r=round(mean(deltas), 4) if deltas else None,
                            sample_status="SUFFICIENT SAMPLE" if len(paired) >= 20 else "INSUFFICIENT SAMPLE"),
                notes=["Recorded entries may occur at different times/prices; paired results do not isolate a TP-only effect.",
                       "Resolved metrics exclude AMBIGUOUS, incomplete coverage, legacy evaluation and invalid final R.",
                       "MFE/MAE are mean OHLC bounds through resolution, on verified resolved entries only.",
                       "Expectancy is observed mean final R before fees, slippage and funding.",
                       "Loss streak uses verified TP/SL ordered by outcome bar, then entry time and key; excluded results are skipped."],
                latest=[dict(setup_id=k, ticker=(c.get(k) or v[k])["ticker"], side=(c.get(k) or v[k])["side"],
                             group="both" if k in both else "current_only" if k in current_only else "shadow_only",
                             current=brief(c.get(k)), shadow=brief(v.get(k))) for k in ids[:limit]])
