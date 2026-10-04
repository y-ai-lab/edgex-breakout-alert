"""Price feasibility diagnostics only. Never decides READY or creates orders."""
from __future__ import annotations

import math
from collections import Counter

from analysis_terminal.setups import setup_identity

STATUSES = ("COMPATIBLE", "NO_OVERLAP", "INVALID_STRUCTURE", "INVALID_INPUT")


def positive(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def diagnose(direction, stop, target, roll, min_rr):
    """Continuous price bands: strict confirmation, inclusive minimum RR.

    With frozen S/T, RR boundary B = S + (T-S)/(1+r).
    LONG: S < E <= B and E > roll. SHORT: B <= E < S and E < roll.
    Candle colour, retest, trend, freshness and tick sizes are separate gates.
    """
    result = dict(version=1, status="INVALID_INPUT", direction=direction,
                  min_rr=min_rr if positive(min_rr) else None,
                  structural_stop=stop if positive(stop) else None,
                  structural_target=target if positive(target) else None,
                  confirmation_level=roll if positive(roll) else None,
                  rr_boundary=None, rr_entry_band=None, compatible_entry_band=None)
    if not isinstance(direction, str) or direction not in {"LONG", "SHORT"} or not all(positive(v) for v in (stop, target, roll, min_rr)):
        return result
    if not (stop < target if direction == "LONG" else target < stop):
        result["status"] = "INVALID_STRUCTURE"
        return result
    boundary = stop + (target - stop) / (1 + min_rr)
    result["rr_boundary"] = boundary
    if direction == "LONG":
        rr_band = dict(lower=stop, upper=boundary, lower_inclusive=False, upper_inclusive=True)
        combined = dict(rr_band, lower=max(stop, roll))
    else:
        rr_band = dict(lower=boundary, upper=stop, lower_inclusive=True, upper_inclusive=False)
        combined = dict(rr_band, upper=min(stop, roll))
    result["rr_entry_band"] = rr_band
    compatible = combined["lower"] < combined["upper"]
    result["status"] = "COMPATIBLE" if compatible else "NO_OVERLAP"
    result["compatible_entry_band"] = combined if compatible else None
    return result


def observation(rows, *, observed_ms):
    """Persist every identified diagnosed setup, including ones not confirmed yet."""
    items = {}
    unidentified = 0
    for row in rows:
        band = row.get("entry_band")
        if not isinstance(band, dict) or band.get("version") != 1:
            continue
        identity = setup_identity(row)
        if not identity or identity != row.get("setup_id"):
            unidentified += 1
            continue
        items[identity] = {k: row.get(k) for k in (
            "setup_id", "ticker", "direction", "breakout_time_ms", "breakout_level",
            "latest_4h_time_ms", "latest_15m_time_ms", "stage", "confirmed")}
        items[identity]["entry_band"] = band
    counts = Counter(item["entry_band"]["status"] for item in items.values())
    return dict(version=1, observed_ms=observed_ms, basis="identified_setup_per_recorded_bucket",
                evaluated=len(items), counts={k: counts[k] for k in STATUSES},
                unidentified=unidentified, items=list(items.values()))


def valid_observation(value):
    """Legacy/malformed observations remain missing, never fabricated as zeros."""
    def count(v):
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1
            or not count(value.get("observed_ms")) or not count(value.get("evaluated"))
            or not count(value.get("unidentified")) or not isinstance(value.get("items"), list)
            or not isinstance(value.get("counts"), dict) or set(value["counts"]) != set(STATUSES)
            or not all(count(v) for v in value["counts"].values())
            or sum(value["counts"].values()) != value["evaluated"]
            or len(value["items"]) != value["evaluated"]):
        return False
    identities, actual = set(), Counter()
    for item in value["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("direction"), str):
            return False
        identity = setup_identity(item)
        band = item.get("entry_band")
        if (not identity or identity != item.get("setup_id") or identity in identities
                or not isinstance(band, dict) or band.get("direction") != item.get("direction")
                or band != diagnose(band.get("direction"), band.get("structural_stop"),
                                    band.get("structural_target"), band.get("confirmation_level"), band.get("min_rr"))):
            return False
        identities.add(identity)
        actual[band["status"]] += 1
    return all(actual[k] == value["counts"][k] for k in STATUSES)


def history_summary(snapshots):
    values = [x["entry_bands"] for x in snapshots if valid_observation(x.get("entry_bands"))]
    counts = {k: sum(v["counts"][k] for v in values) for k in STATUSES}
    geometric = counts["COMPATIBLE"] + counts["NO_OVERLAP"]
    return dict(version=1, observation_basis="setup_per_recorded_15m_bucket",
                observations=len(values), missing_observations=len(snapshots)-len(values),
                evaluated_exposures=sum(v["evaluated"] for v in values) if values else None,
                counts=counts if values else None,
                no_overlap_pct=round(100*counts["NO_OVERLAP"]/geometric, 2) if geometric else None)
