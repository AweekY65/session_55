"""Value-level helpers shared by readers, profilers and rules.

Canonicalisation of a cell value defines what "equality" means for the
whole tool:

* ``None`` / ``NaN`` / ``NaT`` (pandas missing values) and the empty
  string (optionally) are treated as null.
* Numbers that compare equal share a canonical form (``1`` and ``"1"``
  are therefore the same value when ``coerce_numbers`` is enabled).
* Everything else is compared as a string after stripping nothing.
"""

from __future__ import annotations

import math
from typing import Any, Optional

# Strings that pandas itself treats as NA ("", "null", "none", ...) plus
# our own lowercased aliases.  Comparison is case-insensitive.
_NULL_STRINGS = frozenset(
    {
        "",
        "null",
        "none",
        "nan",
        "na",
        "n/a",
        "#n/a",
        "nil",
    }
)


def is_null(value: Any, empty_as_null: bool = True) -> bool:
    """Return True if *value* is a missing value.

    ``None``, pandas ``NaN``/``NaT`` and the strings ``""`` (only when
    *empty_as_null* is True), ``null``, ``none``, ``nan``, ``na``,
    ``n/a``, ``#n/a`` and ``nil`` (case-insensitive, whitespace
    trimmed) count as null.
    """
    if value is None:
        return True
    # NaN only equals itself False, so math.isnan is the cheap test.
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        # pandas NaT / NA: duck-typed by class name to avoid importing pandas.
        if type(value).__name__ in ("NaTType", "NAType"):
            return True
    except Exception:
        pass
    if isinstance(value, str):
        text = value.strip().lower()
        if empty_as_null:
            return text in _NULL_STRINGS
        return text in _NULL_STRINGS and text != ""
    return False


def parse_number(value: Any) -> Optional[float]:
    """Parse *value* into a finite int/float, or return None.

    Booleans are deliberately NOT numbers (``True`` != 1 here) so that a
    boolean column is not mis-profiled as numeric.  Strings use Python
    literal conversion; values like ``"1,000"`` are not parsed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def value_type(value: Any, empty_as_null: bool = True) -> str:
    """Infer the logical type of one non-null value.

    Returns one of ``int`` / ``float`` / ``bool`` / ``string`` /
    ``unknown``.  Callers should test :func:`is_null` first.
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float" if math.isfinite(value) else "unknown"
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-+").isdigit():
            return "int"
        if parse_number(text) is not None:
            return "float"
        return "string"
    return "unknown"


def canonical(value: Any, coerce_numbers: bool = True) -> Any:
    """Return a hashable canonical form of *value* for equality checks.

    With *coerce_numbers* (the default) numeric strings collapse onto
    numbers and ``1`` == ``1.0``.  Booleans keep their own identity so
    that ``True`` is not considered equal to ``1``.
    """
    if is_null(value):
        return None
    if isinstance(value, bool):
        return ("bool", value)
    if coerce_numbers:
        number = parse_number(value)
        if number is not None:
            return ("num", number)
    return ("str", str(value))
