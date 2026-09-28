"""基线 profile 比较测试：缺列、新增列、类型变化、空值率漂移、范围异常。"""

from dqtool.baseline import compare_profiles
from dqtool.profiler import Profiler, profile_rows
from dqtool.values import normalize_row


def make_profile(rows):
    return profile_rows([normalize_row(r) for r in rows])


def kinds(diff):
    return [(f["kind"], f.get("column")) for f in diff.findings]


def test_identical_profiles_pass():
    rows = [{"id": i, "v": i * 1.0, "name": f"n{i}"} for i in range(20)]
    diff = compare_profiles(make_profile(rows), make_profile(rows))
    assert diff.passed
    assert diff.to_dict()["finding_count"] == 0


def test_missing_and_added_columns():
    baseline = make_profile([{"a": 1, "b": 2}])
    current = make_profile([{"a": 1, "c": 3}])
    diff = compare_profiles(baseline, current)
    by_column = {column: kind for kind, column in kinds(diff)}
    assert by_column.get("b") == "column_missing"
    assert by_column.get("c") == "column_added"


def test_type_change_detected():
    # 未归一化的 profile：基线中 v 是文本（如某列全部含非数字字符），
    # 当前是整数，模拟“同一列类型漂移”
    baseline = profile_rows([{"v": "abc"}, {"v": "def"}])
    current = make_profile([{"v": 1}, {"v": 2}])
    diff = compare_profiles(baseline, current)
    type_findings = [f for f in diff.findings if f["kind"] == "type_change"]
    assert len(type_findings) == 1
    assert type_findings[0]["baseline_type"] == "string"
    assert type_findings[0]["current_type"] == "int"


def test_all_null_baseline_does_not_cause_type_change():
    baseline = make_profile([{"v": None}, {"v": None}])
    current = make_profile([{"v": 1}, {"v": 2}])
    diff = compare_profiles(baseline, current)
    assert not any(f["kind"] == "type_change" for f in diff.findings)


def test_null_rate_shift_absolute_threshold():
    baseline = make_profile([{"v": i} for i in range(10)])  # 0% null
    current = make_profile([{"v": None}] * 2 + [{"v": 1}] * 8)  # 20% null
    diff = compare_profiles(baseline, current)
    shifts = [f for f in diff.findings if f["kind"] == "null_rate_shift"]
    assert len(shifts) == 1
    assert shifts[0]["current_null_rate"] == 0.2


def test_null_rate_small_change_within_threshold_passes():
    baseline = make_profile([{"v": 1}] * 100)
    current = make_profile([{"v": None}] * 5 + [{"v": 1}] * 95)  # +5%
    diff = compare_profiles(baseline, current, null_rate_abs=0.10, null_rate_rel=0.5)
    assert not any(f["kind"] == "null_rate_shift" for f in diff.findings)


def test_null_rate_relative_shift_on_small_base():
    baseline = make_profile([{"v": None}] * 4 + [{"v": 1}] * 96)  # 4%
    current = make_profile([{"v": None}] * 8 + [{"v": 1}] * 92)  # 8%，绝对差不大但翻倍
    diff = compare_profiles(baseline, current, null_rate_abs=0.10, null_rate_rel=0.5)
    shifts = [f for f in diff.findings if f["kind"] == "null_rate_shift"]
    assert len(shifts) == 1


def test_range_anomaly_max_breach():
    baseline = make_profile([{"v": 0}, {"v": 100}])
    current = make_profile([{"v": 0}, {"v": 130}])  # 超出 30% > 默认 20%
    diff = compare_profiles(baseline, current)
    ranges = [f for f in diff.findings if f["kind"] == "range_anomaly"]
    assert len(ranges) == 1
    assert ranges[0]["boundary"] == "max"


def test_range_within_tolerance_passes():
    baseline = make_profile([{"v": 0}, {"v": 100}])
    current = make_profile([{"v": 0}, {"v": 115}])  # 15% < 20%
    diff = compare_profiles(baseline, current)
    assert not any(f["kind"] == "range_anomaly" for f in diff.findings)


def test_range_constant_column_uses_absolute_tolerance():
    baseline = make_profile([{"v": 5}, {"v": 5}])  # 跨度 0
    current = make_profile([{"v": 5}, {"v": 7}])
    no_tol = compare_profiles(baseline, current)
    assert any(f["kind"] == "range_anomaly" for f in no_tol.findings)
    with_tol = compare_profiles(baseline, current, range_abs=3)
    assert not any(f["kind"] == "range_anomaly" for f in with_tol.findings)


def test_range_min_breach_separately_reported():
    baseline = make_profile([{"v": 0}, {"v": 100}])
    current = make_profile([{"v": -40}, {"v": 140}])
    diff = compare_profiles(baseline, current)
    boundaries = {f["boundary"] for f in diff.findings if f["kind"] == "range_anomaly"}
    assert boundaries == {"min", "max"}


def test_profiler_objects_accepted():
    p1 = Profiler()
    p1.update([normalize_row(r) for r in [{"a": 1}, {"a": 2}]])
    p2 = Profiler()
    p2.update([{"a": 1}])
    diff = compare_profiles(p1, p2)
    assert diff.baseline_row_count == 2
    assert diff.current_row_count == 1
