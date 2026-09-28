"""Tests for the validation engine and every rule type.

Covers nulls, duplicates, non-numeric type errors, range boundaries,
regex, allowed-values, composite unique keys, row positions and the
in-memory API.
"""

import pytest

from dqtool.readers import read_chunks
from dqtool.rules import parse_rules
from dqtool.validation import validate_chunks, validate_records


def _run(records, rules, max_failures=1000):
    return validate_records(parse_rules(rules), records,
                            max_failures=max_failures)


def _fail_rows(report, rule_id):
    return [(f.row, f.reason) for f in report.results[rule_id].failures]


# ---------------------------------------------------------------- not-null

def test_not_null_detects_empty_and_missing():
    report = _run(
        [{"a": "1"}, {"a": ""}, {"a": "3"}, {"a": None}, {}],
        [{"id": "nn", "type": "not-null", "field": "a"}],
    )
    assert not report.passed
    assert report.results["nn"].failure_count == 3
    rows = [f.row for f in report.results["nn"].failures]
    assert rows == [1, 3, 4]
    assert all(f.field == "a" for f in report.results["nn"].failures)
    assert all(f.rule_id == "nn" for f in report.results["nn"].failures)


def test_not_null_accepts_zero_and_false():
    report = _run(
        [{"a": 0}, {"a": False}, {"a": "0"}],
        [{"id": "nn", "type": "not-null", "field": "a"}],
    )
    assert report.passed


# ------------------------------------------------------------------ unique

def test_unique_flags_second_and_later_duplicates():
    report = _run(
        [{"a": "x"}, {"a": "y"}, {"a": "x"}, {"a": "x"}],
        [{"id": "u", "type": "unique", "field": "a"}],
    )
    assert _fail_rows(report, "u") == [
        (2, "duplicate value"),
        (3, "duplicate value"),
    ]


def test_unique_null_modes():
    # skip (default): nulls never collide
    report = _run(
        [{"a": None}, {"a": ""}, {"a": "x"}],
        [{"id": "u", "type": "unique", "field": "a"}],
    )
    assert report.passed

    # distinct: nulls must also be unique
    report = _run(
        [{"a": None}, {"a": ""}, {"a": "x"}],
        [{"id": "u", "type": "unique", "field": "a",
          "params": {"nulls": "distinct"}}],
    )
    assert report.results["u"].failure_count == 1
    assert report.results["u"].failures[0].row == 1

    # fail: every null is a failure
    report = _run(
        [{"a": "x"}, {"a": None}, {"a": "y"}, {"a": ""}],
        [{"id": "u", "type": "unique", "field": "a",
          "params": {"nulls": "fail"}}],
    )
    assert report.results["u"].failure_count == 2


def test_unique_numeric_coercion():
    # "1" and 1 are the same value by default canonicalisation
    report = _run(
        [{"a": "1"}, {"a": 1}, {"a": 1.0}],
        [{"id": "u", "type": "unique", "field": "a"}],
    )
    assert report.results["u"].failure_count == 2


# ------------------------------------------------------------------- range

def test_range_inclusive_and_exclusive_bounds():
    rules = [{"id": "r", "type": "range", "field": "a",
              "params": {"min": 0, "max": 10}}]
    assert _run([{"a": 0}, {"a": 10}], rules).passed

    rules_ex = [{"id": "r", "type": "range", "field": "a",
                 "params": {"min": 0, "max": 10,
                            "min_inclusive": False, "max_inclusive": False}}]
    report = _run([{"a": 0}, {"a": 10}, {"a": 5}], rules_ex)
    assert sorted(f.row for f in report.results["r"].failures) == [0, 1]


def test_range_non_numeric_value_is_type_error():
    report = _run(
        [{"a": "abc"}, {"a": "5"}, {"a": None}],
        [{"id": "r", "type": "range", "field": "a",
          "params": {"min": 0, "max": 10}}],
    )
    failures = report.results["r"].failures
    assert len(failures) == 1
    assert failures[0].row == 0
    assert "not a numeric value" in failures[0].reason


def test_range_nulls_are_skipped():
    report = _run(
        [{"a": ""}, {"a": None}, {"a": "5"}],
        [{"id": "r", "type": "range", "field": "a",
          "params": {"min": 0}}],
    )
    assert report.passed


# ------------------------------------------------------------------- regex

def test_regex_match_and_negate():
    report = _run(
        [{"e": "a@b.com"}, {"e": "nope"}],
        [{"id": "re", "type": "regex", "field": "e",
          "params": {"pattern": r"^[^@]+@[^@]+\.[^@]+$"}}],
    )
    assert report.results["re"].failures[0].row == 1

    neg = _run(
        [{"e": "forbidden"}, {"e": "ok"}],
        [{"id": "re", "type": "regex", "field": "e",
          "params": {"pattern": "^forbidden$", "negate": True}}],
    )
    assert neg.results["re"].failures[0].row == 0


# ----------------------------------------------------------- allowed-values

def test_allowed_values():
    report = _run(
        [{"s": "a"}, {"s": "b"}, {"s": "c"}, {"s": None}, {"s": "A"}],
        [{"id": "av", "type": "allowed-values", "field": "s",
          "params": {"allowed": ["a", "b"]}}],
    )
    assert [f.row for f in report.results["av"].failures] == [2, 4]


def test_allowed_values_numeric_coercion():
    report = _run(
        [{"s": "1"}, {"s": 1}, {"s": 2}],
        [{"id": "av", "type": "allowed-values", "field": "s",
          "params": {"allowed": [1]}}],
    )
    assert report.results["av"].failure_count == 1
    assert report.results["av"].failures[0].row == 2


# -------------------------------------------------------- composite-unique

def test_composite_unique_key():
    report = _run(
        [
            {"a": 1, "b": "x"},
            {"a": 1, "b": "y"},
            {"a": 2, "b": "x"},
            {"a": 1, "b": "x"},   # duplicate tuple
            {"a": 2, "b": "y"},
        ],
        [{"id": "ck", "type": "composite-unique", "fields": ["a", "b"]}],
    )
    assert report.results["ck"].failure_count == 1
    f = report.results["ck"].failures[0]
    assert f.row == 3
    assert f.fields == ["a", "b"]
    assert "composite" in f.reason


def test_composite_unique_null_policy():
    rules_skip = [{"id": "ck", "type": "composite-unique",
                   "fields": ["a", "b"]}]
    report = _run(
        [{"a": 1, "b": None}, {"a": 1, "b": ""}, {"a": None, "b": 2}],
        rules_skip,
    )
    assert report.passed  # skip mode ignores tuples containing null

    rules_distinct = [{"id": "ck", "type": "composite-unique",
                       "fields": ["a", "b"],
                       "params": {"nulls": "distinct"}}]
    report = _run(
        [{"a": 1, "b": "x"}, {"a": 2, "b": None}, {"a": 3, "b": ""}],
        rules_distinct,
    )
    # rows 1 and 2 share the (non-null, null) shape
    assert report.results["ck"].failure_count == 1
    assert report.results["ck"].failures[0].row == 2

    # fully-null components with different *positions* still collide
    report = _run(
        [{"a": None, "b": 1}, {"a": 1, "b": None}],
        rules_distinct,
    )
    assert report.passed  # null masks differ


# ------------------------------------------------------------------ engine

def test_missing_field_fails_run_without_row_failures():
    report = _run(
        [{"a": "x"}],
        [{"id": "r", "type": "not-null", "field": "ghost"}],
    )
    assert not report.passed
    assert report.missing_fields == {"r": ["ghost"]}
    assert report.total_failures == 0


def test_row_positions_stable_across_chunks(write_csv):
    path = None
    import csv as csv_mod
    # 100 rows, duplicate id on the last row -> row 99 regardless of chunks
    rows = [(i, f"v{i}") for i in range(99)] + [(0, "dup")]
    # build via fixture-like inline writer
    import tempfile, os
    d = tempfile.mkdtemp()
    path = os.path.join(d, "d.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv_mod.writer(f)
        w.writerow(["id", "v"])
        w.writerows(rows)

    rules = parse_rules([{"id": "u", "type": "unique", "field": "id"}])
    report = validate_chunks(rules, read_chunks(path, chunk_size=7))
    assert report.row_count == 100
    failures = report.results["u"].failures
    assert len(failures) == 1
    assert failures[0].row == 99


def test_failure_truncation_keeps_exact_counts():
    records = [{"a": "x"}] * 10
    rules = parse_rules([{"id": "u", "type": "unique", "field": "a"}])
    report = validate_records(rules, records, max_failures=3)
    result = report.results["u"]
    assert result.failure_count == 9          # exact
    assert len(result.failures) == 3          # retained
    assert result.truncated is True


def test_report_serialisation_is_json_clean():
    import json

    report = _run(
        [{"a": object() if False else "x"}],
        [{"id": "nn", "type": "not-null", "field": "a"}],
    )
    payload = report.to_dict(include_values=True)
    json.dumps(payload)  # must not raise
    assert payload["status"] == "passed"
    assert payload["rule_count"] == 1


def test_include_values_flag():
    report = _run(
        [{"a": ""}],
        [{"id": "nn", "type": "not-null", "field": "a"}],
    )
    assert "value" not in report.results["nn"].failures[0].to_dict()
    assert report.results["nn"].failures[0].to_dict(include_value=True)[
        "value"
    ] == ""
