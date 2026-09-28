"""Tests for streaming profiling statistics."""

import math

from dqtool.profiler import ProfileBuilder, profile_dataset
from dqtool.readers import read_chunks


def _profile(records, top_k=10):
    builder = ProfileBuilder(top_k=top_k)
    columns = []
    for r in records:
        for k in r:
            if k not in columns:
                columns.append(k)
    builder.update(columns, records)
    return builder.to_dict()


def test_basic_numeric_stats():
    p = _profile([{"a": "1"}, {"a": "2"}, {"a": "3"}, {"a": "4"}])
    col = p["columns"]["a"]
    assert p["row_count"] == 4
    assert col["total_count"] == 4
    assert col["null_count"] == 0
    assert col["null_rate"] == 0.0
    assert col["unique_count"] == 4
    assert col["inferred_type"] == "int"
    assert col["min_value"] == 1.0
    assert col["max_value"] == 4.0
    assert col["mean"] == 2.5


def test_null_rate_and_common_values():
    p = _profile(
        [{"a": "x"}, {"a": "x"}, {"a": "x"}, {"a": "y"}, {"a": ""}]
    )
    col = p["columns"]["a"]
    assert col["null_count"] == 1
    assert col["null_rate"] == 0.2
    assert col["unique_count"] == 2  # distinct non-null values: x, y
    common = {c["value"]: c["count"] for c in col["common_values"]}
    assert common["x"] == 3
    assert common["y"] == 1


def test_top_k_limits_common_values():
    records = [{"a": str(i % 4)} for i in range(20)]
    p = _profile(records, top_k=2)
    assert len(p["columns"]["a"]["common_values"]) == 2


def test_type_inference_mixed_and_numeric():
    p = _profile(
        [
            {"a": "1"}, {"a": "2.5"},       # numeric (int+float)
            {"b": "1"}, {"b": "x"},         # mixed
            {"c": "yes"}, {"c": "no"},      # string
            {"d": ""}, {"d": None},         # null
            {"e": True}, {"e": False},      # bool
        ]
    )
    assert p["columns"]["a"]["inferred_type"] == "numeric"
    assert p["columns"]["b"]["inferred_type"] == "mixed"
    assert p["columns"]["c"]["inferred_type"] == "string"
    assert p["columns"]["d"]["inferred_type"] == "null"
    assert p["columns"]["e"]["inferred_type"] == "bool"
    # type_counts detail is exposed
    assert p["columns"]["b"]["type_counts"] == {"int": 1, "string": 1}


def test_mean_accuracy_large_sequence():
    records = [{"a": str(i)} for i in range(1, 10_001)]
    p = _profile(records)
    col = p["columns"]["a"]
    assert col["min_value"] == 1.0
    assert col["max_value"] == 10_000.0
    assert math.isclose(col["mean"], 5000.5, rel_tol=1e-9)
    assert col["unique_count"] == 10_000


def test_profile_chunked_matches_single_pass(write_csv, tmp_path):
    rows = [(i, "x" if i % 2 else "y", "" if i % 7 == 0 else i)
            for i in range(100)]
    path = write_csv("d.csv", ["id", "g", "m"], rows)
    single = profile_dataset(read_chunks(path, chunk_size=10_000))
    chunked = profile_dataset(read_chunks(path, chunk_size=13))
    assert single == chunked


def test_profile_parquet_native_types(write_parquet):
    path = write_parquet(
        "d.parquet",
        {"id": [1, 2, 3, 4], "name": ["a", None, "a", "b"],
         "score": [0.5, 1.5, 2.5, 3.5]},
    )
    p = profile_dataset(read_chunks(path, chunk_size=2))
    assert p["columns"]["id"]["inferred_type"] == "int"
    assert p["columns"]["score"]["mean"] == 2.0
    assert p["columns"]["name"]["null_rate"] == 0.25
