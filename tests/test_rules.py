"""规则引擎测试：空值、重复、类型错误、复合唯一键及逐行失败记录。"""

import pytest

from dqtool.errors import RuleError
from dqtool.rules import RuleEngine, validate_rows
from dqtool.values import normalize_row


def run(rows, rules, **kwargs):
    normalized = [normalize_row(row) for row in rows]
    return validate_rows(normalized, rules, **kwargs)


def test_not_null_rule(sample_rows, sample_rules):
    result = run(sample_rows, sample_rules)
    failures = [f for f in result.failures if f.rule_id == "R001"]
    assert len(failures) == 1
    assert failures[0].row_number == 2
    assert failures[0].column == "age"
    assert "空" in failures[0].reason


def test_unique_rule_flags_duplicates_with_first_row():
    rules = [{"id": "U", "type": "unique", "column": "code"}]
    rows = [{"code": "a"}, {"code": "b"}, {"code": "a"}, {"code": "a"}]
    result = run(rows, rules)
    dup = [f for f in result.failures if f.rule_id == "U"]
    assert [f.row_number for f in dup] == [3, 4]
    assert "首次出现于第 1 行" in dup[0].reason
    # 首次出现不算失败
    assert result.stats["U"].passed == 2


def test_unique_ignores_nulls():
    rules = [{"id": "U", "type": "unique", "column": "code"}]
    result = run([{"code": None}, {"code": None}, {"code": ""}], rules)
    assert result.passed


def test_range_rule_out_of_bounds():
    rules = [{"id": "R", "type": "range", "column": "n", "min": 0, "max": 100}]
    result = run([{"n": 0}, {"n": 100}, {"n": -1}, {"n": 101}], rules)
    assert [f.row_number for f in result.failures] == [3, 4]
    assert result.stats["R"].passed == 2


def test_range_type_error_is_reported(sample_rows, sample_rules):
    result = run(sample_rows, sample_rules)
    type_failures = [
        f for f in result.failures
        if f.rule_id == "R003" and "类型错误" in f.reason
    ]
    assert len(type_failures) == 1
    assert type_failures[0].row_number == 5
    assert "str" in type_failures[0].reason


def test_range_bools_are_not_numbers():
    rules = [{"id": "R", "type": "range", "column": "n", "min": 0, "max": 100}]
    result = run([{"n": True}], rules)
    assert len(result.failures) == 1
    assert "类型错误" in result.failures[0].reason


def test_regex_rule():
    rules = [{"id": "RE", "type": "regex", "column": "email",
              "pattern": r"^[^@]+@[^@]+\.[^@]+$"}]
    result = run([{"email": "a@b.com"}, {"email": "bad"}, {"email": None}], rules)
    assert [f.row_number for f in result.failures] == [2]
    assert result.passed is False


def test_allowed_values_rule(sample_rows, sample_rules):
    result = run(sample_rows, sample_rules)
    failures = [f for f in result.failures if f.rule_id == "R005"]
    assert len(failures) == 1
    assert failures[0].row_number == 4
    assert "unknown" in failures[0].reason


def test_composite_unique_key():
    rules = [{"id": "C", "type": "unique-composite", "columns": ["a", "b"]}]
    rows = [
        {"a": 1, "b": "x"},
        {"a": 1, "b": "x"},   # 重复
        {"a": 1, "b": "y"},
        {"a": 2, "b": "x"},
        {"a": None, "b": None},
        {"a": None, "b": None},  # 全空键豁免，不算重复
    ]
    result = run(rows, rules)
    failures = result.failures
    assert len(failures) == 1
    assert failures[0].row_number == 2
    assert failures[0].columns == ["a", "b"]
    assert "首次出现于第 1 行" in failures[0].reason
    assert result.stats["C"].passed == 5


def test_failure_records_have_required_fields(sample_rows, sample_rules):
    result = run(sample_rows, sample_rules)
    assert not result.passed
    for failure in result.failures:
        assert failure.rule_id
        assert failure.rule_type
        assert isinstance(failure.row_number, int)
        assert failure.reason
        assert failure.column or failure.columns


def test_null_values_exempt_from_other_rules():
    rules = [
        {"id": "R1", "type": "range", "column": "n", "min": 0, "max": 1},
        {"id": "R2", "type": "regex", "column": "n", "pattern": "."},
        {"id": "R3", "type": "allowed-values", "column": "n", "values": [0, 1]},
        {"id": "R4", "type": "unique", "column": "n"},
    ]
    result = run([{"n": None}, {"n": None}], rules)
    assert result.passed


def test_max_failures_truncation():
    rules = [{"id": "N", "type": "not-null", "column": "x"}]
    rows = [{"x": None}] * 10
    result = run(rows, rules, max_failures=3)
    assert result.failure_count == 10          # 计数完整
    assert len(result.failures) == 3           # 明细受限
    assert result.truncated


def test_missing_column_reference_raises():
    engine = RuleEngine([{"id": "N", "type": "not-null", "column": "absent"}])
    with pytest.raises(RuleError):
        engine.check_columns(["x"])


def test_invalid_rule_specs():
    bad_specs = [
        [{"id": "A", "type": "unknown", "column": "x"}],
        [{"id": "B", "type": "range", "column": "x"}],
        [{"id": "C", "type": "range", "column": "x", "min": 10, "max": 1}],
        [{"id": "D", "type": "regex", "column": "x", "pattern": "("}],
        [{"id": "E", "type": "allowed-values", "column": "x", "values": []}],
        [{"id": "F", "type": "unique-composite", "columns": ["a", "a"]}],
        [{"id": "G"}],
    ]
    for spec in bad_specs:
        with pytest.raises(RuleError):
            RuleEngine(spec)


def test_duplicate_rule_ids_rejected():
    specs = [
        {"id": "SAME", "type": "not-null", "column": "a"},
        {"id": "SAME", "type": "not-null", "column": "b"},
    ]
    with pytest.raises(RuleError):
        RuleEngine(specs)


def test_engine_row_numbers_continue_across_chunks():
    rules = [{"id": "U", "type": "unique", "column": "k"}]
    engine = RuleEngine(rules)
    engine.process_stream(iter([
        [{"k": "a"}, {"k": "b"}],
        [{"k": "a"}, {"k": "c"}],
    ]))
    assert engine.result.failures[0].row_number == 3
