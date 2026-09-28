"""Tests for rule file parsing / validation."""

import json

import pytest

from dqtool.errors import DQConfigError
from dqtool.rules import load_rules, parse_rules


def test_parse_bare_list_and_wrapped(write_rules):
    rules = [{"type": "not-null", "field": "a"}]
    path = write_rules(rules)
    parsed = load_rules(path)
    assert parsed[0].id == "rule_1"
    assert parsed[0].type == "not-null"
    assert parse_rules(rules)[0].id == "rule_1"


def test_all_rule_types_parse():
    parsed = parse_rules(
        [
            {"id": "r1", "type": "not-null", "field": "a"},
            {"id": "r2", "type": "unique", "field": "a",
             "params": {"nulls": "distinct"}},
            {"id": "r3", "type": "range", "field": "a",
             "params": {"min": 1, "max": 9.5, "max_inclusive": False}},
            {"id": "r4", "type": "regex", "field": "a",
             "params": {"pattern": "^x$", "negate": True}},
            {"id": "r5", "type": "allowed-values", "field": "a",
             "params": {"allowed": ["x", "y"], "coerce_numbers": False}},
            {"id": "r6", "type": "composite-unique", "fields": ["a", "b"]},
        ]
    )
    assert [r.id for r in parsed] == ["r1", "r2", "r3", "r4", "r5", "r6"]
    assert parsed[2].params["min"] == 1.0
    assert parsed[2].params["max_inclusive"] is False
    assert parsed[5].fields == ["a", "b"]
    assert parsed[3].params["compiled"].search("x") is not None


@pytest.mark.parametrize(
    "rule,match",
    [
        ({"type": "unknown", "field": "a"}, "unknown type"),
        ({"type": "not-null"}, "requires string 'field'"),
        ({"id": "x", "type": "range", "field": "a"}, "at least one of"),
        ({"id": "x", "type": "range", "field": "a",
          "params": {"min": 10, "max": 1}}, "greater than max"),
        ({"id": "x", "type": "range", "field": "a",
          "params": {"min": "abc"}}, "numeric"),
        ({"id": "x", "type": "regex", "field": "a",
          "params": {"pattern": "("}}, "invalid regex"),
        ({"id": "x", "type": "regex", "field": "a",
          "params": {}}, "pattern"),
        ({"id": "x", "type": "allowed-values", "field": "a",
          "params": {"allowed": []}}, "non-empty"),
        ({"id": "x", "type": "composite-unique"}, "non-empty 'fields'"),
        ({"id": "x", "type": "composite-unique", "fields": ["a", ""]},
         "non-empty strings"),
        ({"id": "x", "type": "unique", "field": "a",
          "params": {"nulls": "nope"}}, "params.nulls"),
    ],
)
def test_invalid_rules_raise(rule, match):
    with pytest.raises(DQConfigError, match=match):
        parse_rules([rule])


def test_duplicate_ids_rejected():
    with pytest.raises(DQConfigError, match="duplicate rule id"):
        parse_rules(
            [
                {"id": "x", "type": "not-null", "field": "a"},
                {"id": "x", "type": "not-null", "field": "b"},
            ]
        )


def test_rule_document_must_be_list():
    with pytest.raises(DQConfigError):
        parse_rules({"nope": 1})
    with pytest.raises(DQConfigError):
        parse_rules("not-a-rule")


def test_load_rules_missing_file(tmp_path):
    with pytest.raises(DQConfigError, match="not found"):
        load_rules(str(tmp_path / "missing.json"))


def test_load_rules_bad_json(tmp_path):
    p = tmp_path / "r.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(DQConfigError, match="invalid JSON"):
        load_rules(str(p))
