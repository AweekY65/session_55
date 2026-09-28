"""Streaming column profiling.

A :class:`ProfileBuilder` consumes the ``(columns, rows)`` chunks
produced by :mod:`dqtool.readers` and keeps bounded per-column state:

* row / null counts
* per inferred-type counts (``int`` / ``float`` / ``bool`` /
  ``string`` / ``unknown``)
* numeric min / max / running sum (Welford accumulator is not needed:
  values are already in python floats, a single running sum with
  compensated Kahan summation is used for stability)
* a bounded heavy-hitters sketch for common values

No full column materialisation happens, so profiling a multi-GB file
keeps roughly O(columns * distinct-values-tracked) memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .values import canonical, is_null, parse_number, value_type

DEFAULT_TOP_K = 10
_MAX_HEAVY_HITTERS = 10_000


@dataclass
class ColumnProfile:
    name: str
    total_count: int = 0
    null_count: int = 0
    unique_count: Optional[int] = None
    type_counts: Dict[str, int] = field(default_factory=dict)
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    mean: Optional[float] = None
    common_values: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_count": self.total_count,
            "null_count": self.null_count,
            "null_rate": (
                round(self.null_count / self.total_count, 6)
                if self.total_count
                else None
            ),
            "unique_count": self.unique_count,
            "inferred_type": self.inferred_type,
            "type_counts": dict(self.type_counts),
            "min_value": self.min_value,
            "max_value": self.max_value,
            "mean": self.mean,
            "common_values": self.common_values,
        }

    @property
    def inferred_type(self) -> str:
        """Overall column type.

        * no rows / all null -> ``null``
        * exactly one non-null type -> that type
        * int + float mixed -> ``numeric``
        * anything else -> ``mixed``
        """
        seen = {t for t, c in self.type_counts.items() if c > 0}
        seen.discard("unknown")
        if not seen:
            return "null"
        if len(seen) == 1:
            return next(iter(seen))
        if seen == {"int", "float"}:
            return "numeric"
        return "mixed"


class _ColumnAccumulator:
    __slots__ = (
        "name",
        "total",
        "nulls",
        "type_counts",
        "num_min",
        "num_max",
        "num_sum",
        "num_comp",
        "num_count",
        "exact_values",
        "heavy",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self.total = 0
        self.nulls = 0
        self.type_counts: Dict[str, int] = {}
        self.num_min: Optional[float] = None
        self.num_max: Optional[float] = None
        self.num_sum = 0.0
        self.num_comp = 0.0  # Kahan compensation
        self.num_count = 0
        # Exact set while it is small; once it overflows the heavy
        # hitters sketch is still kept but unique_count becomes an
        # estimate-free lower bound (reported as null).
        self.exact_values: set[Any] = set()
        self.heavy: Dict[Any, int] = {}

    def update(self, value: Any) -> None:
        self.total += 1
        if is_null(value):
            self.nulls += 1
            return
        t = value_type(value)
        self.type_counts[t] = self.type_counts.get(t, 0) + 1
        number = parse_number(value)
        if number is not None:
            self.num_count += 1
            if self.num_min is None or number < self.num_min:
                self.num_min = number
            if self.num_max is None or number > self.num_max:
                self.num_max = number
            # Kahan compensated summation.
            y = number - self.num_comp
            t_sum = self.num_sum + y
            self.num_comp = (t_sum - self.num_sum) - y
            self.num_sum = t_sum
        key = canonical(value)
        if len(self.exact_values) <= _MAX_HEAVY_HITTERS:
            self.exact_values.add(key)
        # Misra–Gries style bounded sketch with generous capacity means
        # the top frequencies are exact for cardinalities under the cap
        # and approximate otherwise.
        if key in self.heavy:
            self.heavy[key] += 1
        elif len(self.heavy) < _MAX_HEAVY_HITTERS:
            self.heavy[key] = 1
        else:
            # decrement every counter; drop zeros
            for k in list(self.heavy):
                self.heavy[k] -= 1
                if self.heavy[k] == 0:
                    del self.heavy[k]

    def finish(self, top_k: int) -> ColumnProfile:
        profile = ColumnProfile(name=self.name)
        profile.total_count = self.total
        profile.null_count = self.nulls
        profile.type_counts = {
            t: self.type_counts.get(t, 0)
            for t in ("int", "float", "bool", "string", "unknown")
            if self.type_counts.get(t, 0)
        }
        if self.num_count:
            profile.min_value = self.num_min
            profile.max_value = self.num_max
            profile.mean = round(self.num_sum / self.num_count, 6)
        profile.unique_count = (
            len(self.exact_values)
            if len(self.exact_values) <= _MAX_HEAVY_HITTERS
            else None
        )
        ranked = sorted(self.heavy.items(), key=lambda kv: (-kv[1], _sort_key(kv[0])))
        common: List[Dict[str, Any]] = []
        for key, count in ranked[:top_k]:
            common.append(
                {"value": _render_key(key), "count": count}
            )
        profile.common_values = common
        return profile


def _sort_key(key: Any) -> str:
    # keys are tuples ("num", x) / ("bool", x) / ("str", x) / None
    return repr(key)


def _render_key(key: Any) -> Any:
    if key is None:
        return None
    kind, value = key
    if kind == "bool":
        return value
    if kind == "num":
        # render whole numbers as ints for readability
        if float(value).is_integer():
            return int(value)
        return value
    return value


class ProfileBuilder:
    """Accumulate per-column profiles over streamed chunks."""

    def __init__(self, top_k: int = DEFAULT_TOP_K) -> None:
        self.top_k = top_k
        self._columns: Dict[str, _ColumnAccumulator] = {}

    def update(self, columns: Iterable[str], rows: Iterable[Dict[str, Any]]) -> None:
        columns = list(columns)
        for name in columns:
            if name not in self._columns:
                self._columns[name] = _ColumnAccumulator(name)
        for row in rows:
            for name in columns:
                self._columns[name].update(row.get(name))

    def finish(self) -> Dict[str, ColumnProfile]:
        return {
            name: acc.finish(self.top_k) for name, acc in self._columns.items()
        }

    def to_dict(self) -> Dict[str, Any]:
        profiles = self.finish()
        total_rows = next(
            (p.total_count for p in profiles.values()), 0
        )
        return {
            "row_count": total_rows,
            "column_count": len(profiles),
            "columns": {name: p.to_dict() for name, p in profiles.items()},
        }


def profile_dataset(
    chunks: Iterable[Any], top_k: int = DEFAULT_TOP_K
) -> Dict[str, Any]:
    """Convenience: consume a reader generator and return a profile dict."""
    builder = ProfileBuilder(top_k=top_k)
    for columns, rows in chunks:
        builder.update(columns, rows)
    result = builder.to_dict()
    if not result["columns"]:
        raise ValueError("cannot profile a dataset with no columns")
    return result
