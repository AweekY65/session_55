"""Baseline profile comparison.

Given a baseline profile (the JSON produced by ``dqtool profile``) and a
candidate profile of a newer dataset, :func:`compare_profiles` reports:

* ``missing_columns``   -- columns present in baseline but absent now
* ``new_columns``       -- columns not seen in baseline
* ``type_changes``      -- inferred type changed
* ``null_rate_changes`` -- absolute null-rate delta exceeds a threshold
* ``range_anomalies``   -- numeric min dropped below / max rose above the
                           baseline range (with optional tolerance)

The function is pure: it only compares profile dictionaries, never
touches data files.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

DEFAULT_NULL_RATE_DELTA = 0.10   # 10 percentage points
DEFAULT_RANGE_TOLERANCE = 0.0    # fraction of baseline range, 0 = exact


def compare_profiles(
    baseline: Dict[str, Any],
    current: Dict[str, Any],
    null_rate_delta: float = DEFAULT_NULL_RATE_DELTA,
    range_tolerance: float = DEFAULT_RANGE_TOLERANCE,
) -> Dict[str, Any]:
    """Compare two profile dicts and return a structured diff report."""
    if not 0.0 <= null_rate_delta <= 1.0:
        raise ValueError("null_rate_delta must be between 0 and 1")
    if range_tolerance < 0:
        raise ValueError("range_tolerance must be >= 0")

    base_cols = baseline.get("columns", {})
    cur_cols = current.get("columns", {})

    missing = [c for c in base_cols if c not in cur_cols]
    new = [c for c in cur_cols if c not in base_cols]

    type_changes: List[Dict[str, Any]] = []
    null_rate_changes: List[Dict[str, Any]] = []
    range_anomalies: List[Dict[str, Any]] = []

    for name in base_cols:
        if name not in cur_cols:
            continue
        b = base_cols[name]
        c = cur_cols[name]

        b_type = b.get("inferred_type")
        c_type = c.get("inferred_type")
        # a fully-null column has no real type yet; don't flag it
        if b_type != c_type and b_type != "null" and c_type != "null":
            type_changes.append(
                {
                    "column": name,
                    "baseline_type": b_type,
                    "current_type": c_type,
                }
            )

        b_rate = b.get("null_rate")
        c_rate = c.get("null_rate")
        if b_rate is not None and c_rate is not None:
            delta = round(c_rate - b_rate, 6)
            if abs(delta) >= null_rate_delta:
                null_rate_changes.append(
                    {
                        "column": name,
                        "baseline_null_rate": b_rate,
                        "current_null_rate": c_rate,
                        "delta": delta,
                        "threshold": null_rate_delta,
                    }
                )

        anomaly = _range_anomaly(name, b, c, range_tolerance)
        if anomaly:
            range_anomalies.append(anomaly)

    anomalies = {
        "missing_columns": sorted(missing),
        "new_columns": sorted(new),
        "type_changes": type_changes,
        "null_rate_changes": null_rate_changes,
        "range_anomalies": range_anomalies,
    }
    anomalies["has_changes"] = any(
        anomalies[k]
        for k in (
            "missing_columns",
            "new_columns",
            "type_changes",
            "null_rate_changes",
            "range_anomalies",
        )
    )
    anomalies["baseline_row_count"] = baseline.get("row_count")
    anomalies["current_row_count"] = current.get("row_count")
    anomalies["thresholds"] = {
        "null_rate_delta": null_rate_delta,
        "range_tolerance": range_tolerance,
    }
    return anomalies


def _range_anomaly(
    name: str, b: Dict[str, Any], c: Dict[str, Any], tolerance: float
) -> Optional[Dict[str, Any]]:
    b_min, b_max = b.get("min_value"), b.get("max_value")
    c_min, c_max = c.get("min_value"), c.get("max_value")
    if b_min is None or b_max is None or c_min is None or c_max is None:
        return None
    span = b_max - b_min
    pad = span * tolerance
    details: Dict[str, Any] = {"column": name, "violations": {}}
    if c_min < b_min - pad:
        details["violations"]["min_below"] = {
            "baseline_min": b_min,
            "current_min": c_min,
        }
    if c_max > b_max + pad:
        details["violations"]["max_above"] = {
            "baseline_max": b_max,
            "current_max": c_max,
        }
    return details if details["violations"] else None
