"""Quality-rule definitions and parsing.

Rules are JSON objects.  A rule file is either ``{"rules": [ ... ]}``
or a bare list of rules::

    {
      "id": "email_format",            # optional; defaults to rule index
      "type": "regex",                 # one of the six rule types
      "field": "email",
      "params": { "pattern": "^[^@]+@[^@]+$" }
    }

Supported rule types
--------------------
not-null
    ``{"field": "id"}`` -- value must not be null.
unique
    ``{"field": "email", "params": {"nulls": "skip"|"distinct"|"fail"}}``
    -- field values must be unique.  ``nulls``: ``skip`` (default)
    ignores nulls, ``distinct`` requires nulls to also be unique,
    ``fail`` makes any null a rule failure in addition to duplicate
    checking.
range
    ``{"field": "age", "params": {"min": 0, "max": 120,
    "min_inclusive": true, "max_inclusive": true}}`` -- numeric range.
    Bounds are optional; non-numeric or non-parseable values fail.
regex
    ``{"field": "phone", "params": {"pattern": "...", "negate": false}}``
    -- full ``re.search`` against the string form.  Nulls are skipped.
allowed-values
    ``{"field": "status", "params": {"allowed": ["a", "b"],
    "coerce_numbers": true}}`` -- value must be in the allowed set.
    Nulls are skipped (pair with not-null to forbid them).
composite-unique
    ``{"fields": ["a", "b"], "params": {"nulls": "skip"|"distinct"}}``
    -- the tuple of field values must be unique.  A row missing any of
    the keys is treated as null for that component.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .errors import DQConfigError

RULE_TYPES = frozenset(
    {
        "not-null",
        "unique",
        "range",
        "regex",
        "allowed-values",
        "composite-unique",
    }
)


@dataclass
class Rule:
    id: str
    type: str
    field: Optional[str]
    fields: Optional[List[str]]
    params: Dict[str, Any] = field(default_factory=dict)

    @property
    def target_columns(self) -> List[str]:
        if self.type == "composite-unique":
            return list(self.fields or [])
        return [self.field] if self.field is not None else []


def load_rules(path: str) -> List[Rule]:
    """Load and validate a JSON rule file."""
    import json

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise DQConfigError(f"rule file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DQConfigError(f"invalid JSON in rule file {path}: {exc}") from exc
    except OSError as exc:
        raise DQConfigError(f"cannot read rule file {path}: {exc}") from exc
    return parse_rules(data)


def parse_rules(data: Any) -> List[Rule]:
    """Validate a decoded rule document and return :class:`Rule` objects."""
    if isinstance(data, dict) and "rules" in data:
        raw_rules = data["rules"]
    else:
        raw_rules = data
    if not isinstance(raw_rules, list):
        raise DQConfigError("rule document must be a list or {'rules': [...]}")

    rules: List[Rule] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise DQConfigError(f"rule #{index} must be an object")
        rtype = raw.get("type")
        if rtype not in RULE_TYPES:
            raise DQConfigError(
                f"rule #{index}: unknown type {rtype!r}; "
                f"expected one of {sorted(RULE_TYPES)}"
            )
        rid = raw.get("id") or f"rule_{index + 1}"
        if not isinstance(rid, str) or not rid:
            raise DQConfigError(f"rule #{index}: id must be a non-empty string")
        if rid in seen_ids:
            raise DQConfigError(f"duplicate rule id: {rid!r}")
        seen_ids.add(rid)
        params = raw.get("params") or {}
        if not isinstance(params, dict):
            raise DQConfigError(f"rule {rid!r}: params must be an object")

        single_field = raw.get("field")
        multi_fields = raw.get("fields")

        if rtype == "composite-unique":
            if not isinstance(multi_fields, list) or not multi_fields:
                raise DQConfigError(
                    f"rule {rid!r}: composite-unique requires non-empty 'fields'"
                )
            if not all(isinstance(f, str) and f for f in multi_fields):
                raise DQConfigError(
                    f"rule {rid!r}: 'fields' must be non-empty strings"
                )
            nulls = params.get("nulls", "skip")
            if nulls not in ("skip", "distinct"):
                raise DQConfigError(
                    f"rule {rid!r}: params.nulls must be 'skip' or 'distinct'"
                )
            rule = Rule(rid, rtype, None, list(multi_fields), {"nulls": nulls})
        else:
            if not isinstance(single_field, str) or not single_field:
                raise DQConfigError(
                    f"rule {rid!r}: rule type {rtype!r} requires string 'field'"
                )
            rule = Rule(rid, rtype, single_field, None, dict(params))
            _validate_params(rule)
        rules.append(rule)
    return rules


def _validate_params(rule: Rule) -> None:
    p = rule.params
    if rule.type == "unique":
        nulls = p.get("nulls", "skip")
        if nulls not in ("skip", "distinct", "fail"):
            raise DQConfigError(
                f"rule {rule.id!r}: params.nulls must be skip|distinct|fail"
            )
        rule.params = {"nulls": nulls}
    elif rule.type == "range":
        min_v, max_v = p.get("min"), p.get("max")
        if min_v is None and max_v is None:
            raise DQConfigError(
                f"rule {rule.id!r}: range requires at least one of min/max"
            )
        min_v = _as_number(min_v) if min_v is not None else None
        max_v = _as_number(max_v) if max_v is not None else None
        if min_v is not None and max_v is not None and min_v > max_v:
            raise DQConfigError(
                f"rule {rule.id!r}: range min {min_v} is greater than max {max_v}"
            )
        rule.params = {
            "min": min_v,
            "max": max_v,
            "min_inclusive": bool(p.get("min_inclusive", True)),
            "max_inclusive": bool(p.get("max_inclusive", True)),
        }
    elif rule.type == "regex":
        pattern = p.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise DQConfigError(
                f"rule {rule.id!r}: regex requires string params.pattern"
            )
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise DQConfigError(
                f"rule {rule.id!r}: invalid regex {pattern!r}: {exc}"
            ) from exc
        rule.params = {"compiled": compiled, "pattern": pattern,
                       "negate": bool(p.get("negate", False))}
    elif rule.type == "allowed-values":
        allowed = p.get("allowed")
        if not isinstance(allowed, list) or not allowed:
            raise DQConfigError(
                f"rule {rule.id!r}: allowed-values requires non-empty "
                f"params.allowed list"
            )
        rule.params = {
            "allowed_set": allowed,
            "coerce_numbers": bool(p.get("coerce_numbers", True)),
        }
    # not-null has no params
    elif rule.type == "not-null":
        rule.params = {}


def _as_number(value: Any) -> float:
    from .values import parse_number

    if isinstance(value, bool):
        raise DQConfigError("boolean bounds are not supported")
    if isinstance(value, (int, float)):
        return float(value)
    number = parse_number(value)
    if number is None:
        raise DQConfigError(f"range bound must be numeric, got {value!r}")
    return number
