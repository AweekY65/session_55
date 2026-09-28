"""Streaming validation engine.

Rules are evaluated chunk by chunk.  Stateful rules (``unique`` and
``composite-unique``) remember only the canonical keys they have seen,
so the working set is bounded by the number of distinct keys rather
than the number of rows.

Every failure is a :class:`Failure` with the rule id, the offending
field(s), the 0-based row position in the dataset and a short,
machine-readable reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import DQConfigError
from .rules import Rule
from .values import canonical, is_null, parse_number

# Default cap on failures collected into a report (CI logs should stay
# bounded).  Counts in the summary always reflect the true totals.
DEFAULT_MAX_FAILURES = 1000


@dataclass
class Failure:
    rule_id: str
    rule_type: str
    field: Optional[str]
    fields: Optional[List[str]]
    row: int
    reason: str
    value: Any = None
    # True when the failure was caused by the column key being absent
    # from the row (rather than an explicit null value).  The engine
    # suppresses these when the column is absent from the whole dataset.
    missing_key: bool = False

    def to_dict(self, include_value: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "rule_id": self.rule_id,
            "rule_type": self.rule_type,
            "row": self.row,
            "reason": self.reason,
        }
        if self.field is not None:
            out["field"] = self.field
        if self.fields is not None:
            out["fields"] = list(self.fields)
        if include_value:
            out["value"] = _json_safe(self.value)
        return out


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass
class RuleResult:
    rule: Rule
    failure_count: int = 0
    failures: List[Failure] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self, include_values: bool = False) -> Dict[str, Any]:
        return {
            "rule_id": self.rule.id,
            "rule_type": self.rule.type,
            "field": self.rule.field,
            "fields": self.rule.fields,
            "failure_count": self.failure_count,
            "truncated": self.truncated,
            "failures": [f.to_dict(include_values) for f in self.failures],
        }


class _Checker:
    """Base class for per-rule streaming checkers."""

    def __init__(self, rule: Rule) -> None:
        self.rule = rule
        self.failure_count = 0
        self.failures: List[Failure] = []
        self.truncated = False

    def add_failure(
        self,
        row: int,
        reason: str,
        value: Any,
        max_failures: int,
        missing_key: bool = False,
    ) -> None:
        self.failure_count += 1
        if len(self.failures) < max_failures:
            failure = Failure(
                rule_id=self.rule.id,
                rule_type=self.rule.type,
                field=self.rule.field,
                fields=self.rule.fields,
                row=row,
                reason=reason,
                value=value,
            )
            failure.missing_key = missing_key
            self.failures.append(failure)
        else:
            self.truncated = True

    def check_row(self, row: int, data: Dict[str, Any], max_failures: int) -> None:
        raise NotImplementedError

    def finish(self, missing_columns: set[str]) -> None:
        """Evaluate deferred checks.

        The default implementation suppresses failures that were only
        caused by a rule's column being absent from every row; such
        configuration problems are reported once via ``missing_fields``.
        """
        if not missing_columns:
            return
        if all(col in missing_columns for col in self.rule.target_columns):
            kept = [f for f in self.failures if not f.missing_key]
            self.failure_count -= len(self.failures) - len(kept)
            self.failures = kept
            # suppression removes at most retained details; the summary
            # still distinguishes "no detail kept" from genuine truncation
            self.truncated = False


class _NotNullChecker(_Checker):
    def check_row(self, row, data, max_failures):
        key_missing = self.rule.field not in data
        value = data.get(self.rule.field)
        if key_missing or is_null(value):
            self.add_failure(row, "value is null", value, max_failures,
                             missing_key=key_missing)


class _UniqueChecker(_Checker):
    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.nulls_mode = rule.params.get("nulls", "skip")
        self._seen: set[Any] = set()
        self._null_seen = False

    def check_row(self, row, data, max_failures):
        value = data.get(self.rule.field)
        missing = self.rule.field not in data
        if not missing and is_null(value):
            if self.nulls_mode == "fail":
                self.add_failure(row, "value is null", value, max_failures)
            elif self.nulls_mode == "distinct":
                if self._null_seen:
                    self.add_failure(row, "duplicate null value", value,
                                     max_failures)
                self._null_seen = True
            # "skip": nulls never participate
            return
        if missing:
            # missing key == null; same policy applies
            if self.nulls_mode == "fail":
                self.add_failure(row, "value is null", None, max_failures,
                                 missing_key=True)
            elif self.nulls_mode == "distinct":
                if self._null_seen:
                    self.add_failure(row, "duplicate null value", None,
                                     max_failures, missing_key=True)
                self._null_seen = True
            return
        key = canonical(value)
        if key in self._seen:
            self.add_failure(row, "duplicate value", value, max_failures)
        else:
            self._seen.add(key)


class _RangeChecker(_Checker):
    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        p = rule.params
        self.min_v = p["min"]
        self.max_v = p["max"]
        self.min_inc = p["min_inclusive"]
        self.max_inc = p["max_inclusive"]

    def check_row(self, row, data, max_failures):
        value = data.get(self.rule.field)
        if self.rule.field not in data or is_null(value):
            return
        number = parse_number(value)
        if number is None:
            self.add_failure(row, f"not a numeric value: {value!r}", value,
                             max_failures)
            return
        if self.min_v is not None:
            if self.min_inc and number < self.min_v:
                self.add_failure(
                    row, f"value {number} below min {self.min_v}", value,
                    max_failures,
                )
                return
            if not self.min_inc and number <= self.min_v:
                self.add_failure(
                    row, f"value {number} not greater than {self.min_v}", value,
                    max_failures,
                )
                return
        if self.max_v is not None:
            if self.max_inc and number > self.max_v:
                self.add_failure(
                    row, f"value {number} above max {self.max_v}", value,
                    max_failures,
                )
            elif not self.max_inc and number >= self.max_v:
                self.add_failure(
                    row, f"value {number} not less than {self.max_v}", value,
                    max_failures,
                )


class _RegexChecker(_Checker):
    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.pattern: re.Pattern = rule.params["compiled"]
        self.negate = rule.params["negate"]

    def check_row(self, row, data, max_failures):
        value = data.get(self.rule.field)
        if self.rule.field not in data or is_null(value):
            return
        text = value if isinstance(value, str) else str(value)
        matched = self.pattern.search(text) is not None
        if self.negate:
            if matched:
                self.add_failure(
                    row, f"value matches forbidden pattern "
                         f"{self.pattern.pattern!r}",
                    value, max_failures,
                )
        elif not matched:
            self.add_failure(
                row, f"value does not match pattern "
                     f"{self.pattern.pattern!r}",
                value, max_failures,
            )


class _AllowedValuesChecker(_Checker):
    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        coerce = rule.params.get("coerce_numbers", True)
        self.allowed = {canonical(v, coerce_numbers=coerce)
                        for v in rule.params["allowed_set"]}
        self.coerce = coerce

    def check_row(self, row, data, max_failures):
        value = data.get(self.rule.field)
        if self.rule.field not in data or is_null(value):
            return
        key = canonical(value, coerce_numbers=self.coerce)
        if key not in self.allowed:
            self.add_failure(
                row, f"value {value!r} is not in allowed set", value,
                max_failures,
            )


class _CompositeUniqueChecker(_Checker):
    def __init__(self, rule: Rule) -> None:
        super().__init__(rule)
        self.nulls_mode = rule.params.get("nulls", "skip")
        self._seen: set[Tuple[Any, ...]] = set()
        self._null_seen: set[Tuple[bool, ...]] = set()

    def check_row(self, row, data, max_failures):
        fields = self.rule.fields or []
        values = [data.get(f) for f in fields]
        null_mask = tuple(f not in data or is_null(data.get(f)) for f in fields)
        if any(null_mask):
            if self.nulls_mode == "skip":
                return
            # distinct: tuples with the same null shape collide
            if null_mask in self._null_seen:
                self.add_failure(
                    row,
                    "duplicate composite key (null component)",
                    {f: v for f, v in zip(fields, values)},
                    max_failures,
                    missing_key=any(f not in data for f in fields),
                )
            else:
                self._null_seen.add(null_mask)
            return
        key = tuple(canonical(v) for v in values)
        if key in self._seen:
            self.add_failure(
                row,
                "duplicate composite key",
                {f: v for f, v in zip(fields, values)},
                max_failures,
            )
        else:
            self._seen.add(key)


_CHECKERS = {
    "not-null": _NotNullChecker,
    "unique": _UniqueChecker,
    "range": _RangeChecker,
    "regex": _RegexChecker,
    "allowed-values": _AllowedValuesChecker,
    "composite-unique": _CompositeUniqueChecker,
}


@dataclass
class ValidationReport:
    row_count: int
    results: Dict[str, RuleResult]
    missing_fields: Dict[str, List[str]]

    @property
    def passed(self) -> bool:
        return (
            not self.missing_fields
            and all(r.failure_count == 0 for r in self.results.values())
        )

    @property
    def total_failures(self) -> int:
        return sum(r.failure_count for r in self.results.values())

    def to_dict(self, include_values: bool = False) -> Dict[str, Any]:
        return {
            "status": "passed" if self.passed else "failed",
            "row_count": self.row_count,
            "rule_count": len(self.results),
            "total_failures": self.total_failures,
            "missing_fields": self.missing_fields,
            "results": [
                self.results[r].to_dict(include_values)
                for r in self.results
            ],
        }


def validate_chunks(
    rules: List[Rule],
    chunks: Iterable[Tuple[List[str], List[Dict[str, Any]]]],
    max_failures: int = DEFAULT_MAX_FAILURES,
) -> ValidationReport:
    """Run *rules* over streamed *chunks* and return a ValidationReport.

    A rule whose field never appears in the dataset is recorded under
    ``missing_fields`` (it fails the run even without per-row failures).
    """
    if max_failures <= 0:
        raise DQConfigError("max_failures must be a positive integer")
    checkers = [_CHECKERS[r.type](r) for r in rules]
    seen_columns: set[str] = set()
    row_count = 0

    for columns, rows in chunks:
        seen_columns.update(columns)
        for data in rows:
            for checker in checkers:
                checker.check_row(row_count, data, max_failures)
            row_count += 1

    missing_fields: Dict[str, List[str]] = {}
    for checker in checkers:
        missing_for_rule = {
            col for col in checker.rule.target_columns if col not in seen_columns
        }
        if missing_for_rule:
            missing_fields[checker.rule.id] = sorted(missing_for_rule)
        checker.finish(missing_for_rule)

    results = {c.rule.id: _result_from_checker(c) for c in checkers}
    return ValidationReport(
        row_count=row_count, results=results, missing_fields=missing_fields
    )


def validate_records(
    rules: List[Rule],
    records: List[Dict[str, Any]],
    max_failures: int = DEFAULT_MAX_FAILURES,
) -> ValidationReport:
    """Convenience wrapper for in-memory records (no files involved)."""
    from .readers import rows_from_records

    columns, rows = rows_from_records(records)
    return validate_chunks(rules, [(columns, rows)], max_failures=max_failures)


def _result_from_checker(checker: _Checker) -> RuleResult:
    result = RuleResult(rule=checker.rule)
    result.failure_count = checker.failure_count
    result.failures = checker.failures
    result.truncated = checker.truncated
    return result
