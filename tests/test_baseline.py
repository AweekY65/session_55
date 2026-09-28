"""Tests for baseline profile comparison."""

import pytest

from dqtool.baseline import compare_profiles


def _col(total=100, nulls=0, ctype="int", min_v=0.0, max_v=10.0):
    return {
        "total_count": total,
        "null_count": nulls,
        "null_rate": round(nulls / total, 6) if total else None,
        "unique_count": total - nulls,
        "inferred_type": ctype,
        "type_counts": {ctype: total - nulls} if ctype != "null" else {},
        "min_value": min_v,
        "max_value": max_v,
        "mean": None,
        "common_values": [],
    }


def _profile(cols, rows=100):
    return {"row_count": rows, "column_count": len(cols), "columns": cols}


def test_identical_profiles_match():
    p = _profile({"a": _col()})
    diff = compare_profiles(p, p)
    assert diff["has_changes"] is False
    assert diff == diff  # smoke
    assert diff["missing_columns"] == []
    assert diff["new_columns"] == []
    assert diff["type_changes"] == []
    assert diff["null_rate_changes"] == []
    assert diff["range_anomalies"] == []


def test_missing_and_new_columns():
    base = _profile({"a": _col(), "b": _col()})
    cur = _profile({"a": _col(), "c": _col()})
    diff = compare_profiles(base, cur)
    assert diff["missing_columns"] == ["b"]
    assert diff["new_columns"] == ["c"]
    assert diff["has_changes"] is True


def test_type_change_detected():
    base = _profile({"a": _col(ctype="int")})
    cur = _profile({"a": _col(ctype="string")})
    diff = compare_profiles(base, cur)
    assert diff["type_changes"] == [
        {"column": "a", "baseline_type": "int", "current_type": "string"}
    ]


def test_all_null_column_not_flagged_as_type_change():
    base = _profile({"a": _col(ctype="int")})
    cur = _profile({"a": _col(total=5, nulls=5, ctype="null",
                              min_v=None, max_v=None)})
    diff = compare_profiles(base, cur)
    assert diff["type_changes"] == []
    # but the null-rate shift is still reported
    assert diff["null_rate_changes"][0]["column"] == "a"


def test_null_rate_threshold_boundary():
    base = _profile({"a": _col(total=100, nulls=0)})
    cur = _profile({"a": _col(total=100, nulls=9)})  # +0.09 < 0.10
    diff = compare_profiles(base, cur)
    assert diff["null_rate_changes"] == []

    cur2 = _profile({"a": _col(total=100, nulls=10)})  # +0.10 == threshold
    diff = compare_profiles(base, cur2)
    assert len(diff["null_rate_changes"]) == 1
    assert diff["null_rate_changes"][0]["delta"] == 0.1

    # custom threshold
    diff = compare_profiles(base, cur, null_rate_delta=0.05)
    assert len(diff["null_rate_changes"]) == 1


def test_range_anomaly_both_sides_and_tolerance():
    base = _profile({"a": _col(min_v=0.0, max_v=100.0)})
    cur_ok = _profile({"a": _col(min_v=0.0, max_v=100.0)})
    assert compare_profiles(base, cur_ok)["range_anomalies"] == []

    cur_bad = _profile({"a": _col(min_v=-5.0, max_v=120.0)})
    anomalies = compare_profiles(base, cur_bad)["range_anomalies"]
    assert len(anomalies) == 1
    v = anomalies[0]["violations"]
    assert v["min_below"]["current_min"] == -5.0
    assert v["max_above"]["current_max"] == 120.0

    # tolerance: 5% of span == 5.0 -> -5 and 105 are just inside
    cur_tol = _profile({"a": _col(min_v=-5.0, max_v=105.0)})
    anomalies = compare_profiles(base, cur_tol, range_tolerance=0.05)[
        "range_anomalies"
    ]
    assert anomalies == []


def test_non_numeric_column_has_no_range_anomaly():
    base = _profile({"a": _col(ctype="string", min_v=None, max_v=None)})
    cur = _profile({"a": _col(ctype="string", min_v=None, max_v=None)})
    assert compare_profiles(base, cur)["range_anomalies"] == []


def test_bad_thresholds_raise():
    p = _profile({"a": _col()})
    with pytest.raises(ValueError):
        compare_profiles(p, p, null_rate_delta=1.5)
    with pytest.raises(ValueError):
        compare_profiles(p, p, range_tolerance=-0.1)


def test_end_to_end_profile_compare(tmp_path):
    """Profile two CSV datasets with the real pipeline and compare."""
    import json

    from dqtool.profiler import profile_dataset
    from dqtool.readers import read_chunks

    d = tmp_path

    def write(name, rows):
        with open(d / name, "w", encoding="utf-8") as f:
            f.write("id,v\n")
            for r in rows:
                f.write(",".join(r) + "\n")

    write("base.csv", [(str(i), str(i)) for i in range(20)])
    # current: v becomes textual for the tail rows, last row's id is 99
    write(
        "cur.csv",
        [(str(i), str(i) if i < 15 else f"v{i}") for i in range(19)]
        + [("99", "500")],
    )
    base = profile_dataset(read_chunks(str(d / "base.csv"), chunk_size=5))
    cur = profile_dataset(read_chunks(str(d / "cur.csv"), chunk_size=5))
    diff = compare_profiles(base, cur)
    # baseline v is all-numeric (int); current v mixes int and string
    assert diff["type_changes"] and diff["type_changes"][0]["column"] == "v"
    assert diff["type_changes"][0]["baseline_type"] == "int"
    assert diff["type_changes"][0]["current_type"] == "mixed"
    rng = {a["column"]: a["violations"] for a in diff["range_anomalies"]}
    assert "id" in rng and rng["id"]["max_above"]["current_max"] == 99.0
    assert "v" in rng  # 500 exceeds baseline max of 19
    json.dumps(diff)  # serialisable
