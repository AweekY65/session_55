"""profile 统计口径测试。"""

import math

from dqtool.profiler import Profiler, profile_rows
from dqtool.values import normalize_row
from dqtool.values import normalize_value


def test_basic_profile_numeric_and_string(sample_rows):
    normalized = [normalize_row(row) for row in sample_rows]
    profile = profile_rows(normalized)

    assert profile["row_count"] == 5
    assert profile["column_count"] == 5

    id_col = profile["columns"]["id"]
    assert id_col["inferred_type"] == "int"
    assert id_col["min"] == 1
    assert id_col["max"] == 5
    assert id_col["mean"] == 3
    assert id_col["null_count"] == 0
    assert id_col["null_rate"] == 0.0
    assert id_col["unique_count"] == 5

    age = profile["columns"]["age"]
    # None + 3 个 int + 1 个无法解析的 "abc" -> mixed
    assert age["inferred_type"] == "mixed"
    assert age["null_count"] == 1
    assert age["null_rate"] == 0.2
    assert "min" not in age  # mixed 列不出数值统计


def test_float_profile_stats():
    rows = [normalize_row({"x": v}) for v in ["1.5", "2.5", "4"]]
    profile = profile_rows(rows)
    col = profile["columns"]["x"]
    assert col["inferred_type"] == "float"
    assert col["min"] == 1.5
    assert col["max"] == 4
    assert math.isclose(col["mean"], 8.0 / 3.0)
    assert col["unique_count"] == 3


def test_bool_and_null_tokens():
    rows = [
        normalize_row({"flag": "true"}),
        normalize_row({"flag": "FALSE"}),
        normalize_row({"flag": "NA"}),
        normalize_row({"flag": "n/a"}),
        normalize_row({"flag": "  "}),
    ]
    profile = profile_rows(rows)
    col = profile["columns"]["flag"]
    assert col["inferred_type"] == "bool"
    assert col["null_count"] == 3
    assert col["non_null_count"] == 2


def test_custom_null_values():
    rows = [
        normalize_row({"x": "\\N"}, null_tokens={"\\n", ""}),
        normalize_row({"x": "NA"}, null_tokens={"\\n", ""}),
    ]
    profile = profile_rows(rows)
    assert profile["columns"]["x"]["null_count"] == 1
    assert profile["columns"]["x"]["inferred_type"] == "string"


def test_all_null_column_type_is_null():
    profile = profile_rows([{"x": None}, {"x": normalize_value("")}])
    assert profile["columns"]["x"]["inferred_type"] == "null"
    assert profile["columns"]["x"]["null_rate"] == 1.0


def test_unique_counts_and_common_values(sample_rows):
    normalized = [normalize_row(row) for row in sample_rows]
    profile = profile_rows(normalized, top_k=2)
    status = profile["columns"]["status"]
    assert status["unique_count"] == 3
    top = status["common_values"]
    assert len(top) == 2
    assert top[0] == {"value": "active", "count": 3}


def test_int_and_float_same_value_dedup():
    # 1 与 1.0 在质量语义上视为同一个值
    profile = profile_rows([{"x": 1}, {"x": 1.0}, {"x": 2}])
    assert profile["columns"]["x"]["unique_count"] == 2


def test_nan_and_infinity_become_null():
    rows = [{"x": normalize_value(float("nan"))},
            {"x": normalize_value(float("inf"))},
            {"x": 1.0}]
    col = profile_rows(rows)["columns"]["x"]
    assert col["null_count"] == 2
    assert col["inferred_type"] == "float"


def test_profiler_update_is_incremental(sample_rows):
    normalized = [normalize_row(row) for row in sample_rows]
    a = Profiler()
    a.update(normalized[:2])
    a.update(normalized[2:])
    b = Profiler()
    b.update(normalized)
    assert a.to_dict() == b.to_dict()
