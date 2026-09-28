"""Tests for value canonicalisation and null/type inference."""

import math

import pytest

from dqtool.values import canonical, is_null, parse_number, value_type


@pytest.mark.parametrize(
    "value",
    [None, float("nan"), "", "  ", "NULL", "None", "NaN", "na", "N/A",
     " #N/A ", "nil"],
)
def test_null_values_are_detected(value):
    assert is_null(value) is True


def test_empty_string_not_null_when_disabled():
    assert is_null("", empty_as_null=False) is False
    assert is_null("null", empty_as_null=False) is True


@pytest.mark.parametrize("value", [0, "0", "x", False, 0.0])
def test_non_null_values(value):
    assert is_null(value) is False


def test_pandas_nat_is_null():
    pd = pytest.importorskip("pandas")
    assert is_null(pd.NaT) is True
    assert is_null(pd.NA) is True


def test_parse_number_basic():
    assert parse_number("42") == 42.0
    assert parse_number("-3.5") == -3.5
    assert parse_number(10) == 10.0
    assert parse_number(True) is None  # booleans are not numbers
    assert parse_number("abc") is None
    assert parse_number(float("inf")) is None
    assert parse_number("") is None


def test_value_type_inference():
    assert value_type("42") == "int"
    assert value_type("42.0") == "float"
    assert value_type(True) == "bool"
    assert value_type("hello") == "string"
    assert value_type(3) == "int"
    assert value_type(3.2) == "float"


def test_canonical_number_equivalence():
    assert canonical("1") == canonical(1) == canonical(1.0)
    assert canonical("a") == canonical(" a".strip())
    assert canonical(None) is None
    # bool keeps separate identity from 1
    assert canonical(True) != canonical(1)
    assert canonical("true") != canonical(True)  # string "true" is not bool
    assert math.isnan(1.0) is False
