"""Descriptive daily READY funnels; observations are not new trading signals."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from analysis_terminal.comparison import cohort, metrics

STEP = 900_000
JST = ZoneInfo("Asia/Tokyo")
STAGES = ("DATA_WAIT", "TREND_WAIT", "BREAKOUT_WAIT", "RETEST_WAIT",
          "CONFIRMATION_WAIT", "STRUCTURE_WAIT", "RR_WAIT", "READY")


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def percent(n, d):
    return round(n / d * 100, 2) if d else None


def shadow_observation(rows):
    """Additive snapshot fields only; use existing decisions without re-evaluating them."""
    ready = [r for r in rows if r.get("shadow_v2_ready") is True]
    return dict(version=1, ready=len(ready),
                current_structure_blocked=sum(r.get("stage") == "STRUCTURE_WAIT" for r in ready),
                current_rr_blocked=sum(r.get("stage") == "RR_WAIT" for r in ready))


def funnel(stages):
    passed = dict(scanned=sum(stages.values()), data=sum(stages[s] for s in STAGES[1:]),
                  trend=sum(stages[s] for s in STAGES[2:]),
                  breakout=sum(stages[s] for s in STAGES[3:]),
                  retest=sum(stages[s] for s in STAGES[4:]),
                  confirmation=sum(stages[s] for s in STAGES[5:]),
                  structure=stages["RR_WAIT"] + stages["READY"], ready=stages["READY"])
    return dict(passed=passed, dropped={s: stages[s] for s in STAGES[:-1]},
                confirmation_pass_pct=percent(passed["confirmation"], passed["retest"]),
                post_confirmation_blocked_pct=percent(stages["STRUCTURE_WAIT"] + stages["RR_WAIT"], passed["confirmation"]))


def summarize(observations):
    stages = Counter({s: 0 for s in STAGES})
    valid = []
    for x in observations:
        raw = x.get("stages")
        if (not isinstance(raw, dict) or set(raw) - set(STAGES)
                or any(count(v) is None for v in raw.values())
                or count(x.get("scanned")) is None or sum(raw.values()) != x["scanned"]):
            continue
        valid.append(x)
        stages.update(raw)
    shadows = []
    for x in valid:
        s = x.get("readiness_shadow")
        if (not isinstance(s, dict) or s.get("version") != 1
                or any(count(s.get(k)) is None for k in ("ready", "current_structure_blocked", "current_rr_blocked"))):
            continue
        p = funnel(Counter({**{k: 0 for k in STAGES}, **x["stages"]}))["passed"]
        if (s["ready"] > p["confirmation"]
                or s["current_structure_blocked"] > x["stages"].get("STRUCTURE_WAIT", 0)
                or s["current_rr_blocked"] > x["stages"].get("RR_WAIT", 0)
                or s["current_structure_blocked"] + s["current_rr_blocked"] > s["ready"]):
            continue
        shadows.append(s)
    coverage = [x for x in observations if count(x.get("universe")) is not None
                and count(x.get("scanned")) is not None and x["scanned"] <= x["universe"]]
    result = funnel(stages)
    result.update(observations=len(observations), funnel_observations=len(valid),
                  missing_funnel_observations=len(observations)-len(valid),
                  observed_ready_buckets=sum(x["stages"].get("READY", 0) > 0 for x in valid),
                  universe_exposures=sum(x["universe"] for x in coverage),
                  scanned_exposures=sum(x["scanned"] for x in coverage),
                  scan_coverage_pct=percent(sum(x["scanned"] for x in coverage), sum(x["universe"] for x in coverage)),
                  shadow_observations=len(shadows), missing_shadow_observations=len(observations)-len(shadows),
                  shadow_ready_buckets=sum(s["ready"] > 0 for s in shadows) if shadows else None,
                  shadow_ready_exposures=sum(s["ready"] for s in shadows) if shadows else None,
                  shadow_structure_blocked_exposures=sum(s["current_structure_blocked"] for s in shadows) if shadows else None,
                  shadow_rr_blocked_exposures=sum(s["current_rr_blocked"] for s in shadows) if shadows else None)
    return result


def daily_readiness(snapshots, current_records, shadow_records, *, now_ms, days=7):
    if not 1 <= days <= 30:
        raise ValueError("days must be 1..30")
    today = datetime.fromtimestamp(now_ms / 1000, JST).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int((today - timedelta(days=days-1)).timestamp() * 1000)
    # A 15M bucket is one observation, including a collector restart in that bucket.
    indexed = {}
    ignored = 0
    for x in snapshots:
        stamp = count(x.get("time_ms"))
        if stamp is None or stamp % STEP:
            ignored += 1
        elif start_ms <= stamp <= now_ms:
            indexed[stamp] = x
    models, exclusions = {}, {}
    for name, records in (("current", current_records), ("shadow", shadow_records)):
        first, exclusions[name] = cohort(records)
        # Deduplicate all history BEFORE selecting the reporting window.
        models[name] = [s for s in first if start_ms <= s["created_ms"] <= now_ms]
    daily = []
    for i in range(days):
        begin = start_ms + i * 96 * STEP
        end = min(begin + 96 * STEP, now_ms + 1)
        obs = [x for t, x in sorted(indexed.items()) if begin <= t < end]
        row = summarize(obs)
        expected = (end - 1 - begin) // STEP + 1
        row.update(date=datetime.fromtimestamp(begin/1000, JST).date().isoformat(),
                   start_ms=begin, end_ms=end, partial=begin == int(today.timestamp()*1000),
                   expected_buckets=expected, missing_buckets=expected-len(obs),
                   observation_coverage_pct=percent(len(obs), expected),
                   signals={name: metrics([s for s in items if begin <= s["created_ms"] < end])
                            for name, items in models.items()})
        daily.append(row)
    summary = summarize(list(indexed.values()))
    summary.update(expected_buckets=sum(x["expected_buckets"] for x in daily),
                   missing_buckets=sum(x["missing_buckets"] for x in daily),
                   signals={name: metrics(items) for name, items in models.items()})
    summary["observation_coverage_pct"] = percent(summary["observations"], summary["expected_buckets"])
    return dict(dataset="RECORDED_LIVE_OBSERVATIONS", time_ms=now_ms, timezone="Asia/Tokyo", days=days,
                interval_ms=STEP, start_ms=start_ms, end_ms=now_ms+1,
                observation_basis="ticker_per_recorded_15m_bucket", signal_basis="unique_setup_first_recorded_entry_all_history",
                automatic_promotion=False, changes_live_rules=False,
                excluded_records=exclusions, invalid_snapshot_times=ignored,
                summary=summary, daily=list(reversed(daily)))
