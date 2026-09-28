"""流式字段 profile（统计画像）。

逐块更新，不保留整份数据。统计口径：

- ``total``           该列出现过的行数
- ``null_count``      空值数（归一化后的 None）
- ``null_rate``       null_count / total，空表为 0
- ``unique_count``    非空去重值个数（1 与 1.0 视为同值）
- ``inferred_type``   int / float / bool / string / object / null / mixed
- ``type_counts``     各类原始值类型计数
- ``min/max/mean``    仅数值列（bool 不计入数值）
- ``common_values``   出现频次最高的前 K 个非空值
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Iterator

from .values import is_number, value_key

PROFILE_VERSION = 1

# 推断类型 -> type_counts 中使用的规范名
_KIND_INT = "int"
_KIND_FLOAT = "float"
_KIND_BOOL = "bool"
_KIND_STR = "string"
_KIND_OTHER = "object"


def _kind_of(value: Any) -> str:
    if isinstance(value, bool):
        return _KIND_BOOL
    if isinstance(value, int):
        return _KIND_INT
    if isinstance(value, float):
        return _KIND_FLOAT
    if isinstance(value, str):
        return _KIND_STR
    return _KIND_OTHER


def _json_default(value: Any) -> Any:
    """profile 中非常规标量（date/dict 等）的 JSON 展示。"""
    return str(value)


class ColumnProfile:
    """单列流式统计。"""

    def __init__(self, name: str, top_k: int = 10):
        self.name = name
        self.top_k = top_k
        self.total = 0
        self.null_count = 0
        self.type_counts: Counter = Counter()
        self._frequencies: Counter = Counter()
        self.numeric_count = 0
        self.numeric_sum: float = 0.0
        self.numeric_min: int | float | None = None
        self.numeric_max: int | float | None = None

    def update(self, values: Iterable[Any]) -> None:
        for value in values:
            self.total += 1
            if value is None:
                self.null_count += 1
                continue
            kind = _kind_of(value)
            self.type_counts[kind] += 1
            self._frequencies[value_key(value)] += 1
            if is_number(value):
                self.numeric_count += 1
                self.numeric_sum += value
                if self.numeric_min is None or value < self.numeric_min:
                    self.numeric_min = value
                if self.numeric_max is None or value > self.numeric_max:
                    self.numeric_max = value

    @property
    def non_null_count(self) -> int:
        return self.total - self.null_count

    @property
    def unique_count(self) -> int:
        return len(self._frequencies)

    @property
    def inferred_type(self) -> str:
        if not self.type_counts:
            return "null"
        kinds = set(self.type_counts)
        if kinds == {_KIND_INT}:
            return "int"
        if kinds <= {_KIND_INT, _KIND_FLOAT} and _KIND_FLOAT in kinds:
            return "float"
        if kinds == {_KIND_BOOL}:
            return "bool"
        if kinds == {_KIND_STR}:
            return "string"
        if kinds <= {_KIND_OTHER}:
            return "object"
        return "mixed"

    @property
    def is_numeric(self) -> bool:
        return self.inferred_type in ("int", "float")

    @property
    def null_rate(self) -> float:
        return self.null_count / self.total if self.total else 0.0

    def _common_values(self) -> list[dict]:
        # 频次降序；同频次按 (类型名, 字符串形式) 排序，保证输出确定
        ordered = sorted(
            self._frequencies.items(),
            key=lambda item: (-item[1], type(item[0]).__name__, str(item[0])),
        )
        return [{"value": key, "count": count} for key, count in ordered[: self.top_k]]

    def to_dict(self) -> dict:
        result: dict[str, Any] = {
            "total": self.total,
            "null_count": self.null_count,
            "null_rate": round(self.null_rate, 6),
            "non_null_count": self.non_null_count,
            "unique_count": self.unique_count,
            "inferred_type": self.inferred_type,
            "type_counts": dict(sorted(self.type_counts.items())),
            "common_values": self._common_values(),
        }
        if self.is_numeric and self.numeric_count:
            result["min"] = self.numeric_min
            result["max"] = self.numeric_max
            result["mean"] = self.numeric_sum / self.numeric_count
        return result

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "ColumnProfile":
        """从基线 JSON 重建（仅含比较所需的汇总字段，频次明细不恢复）。"""
        col = cls(name)
        col.total = int(data.get("total", 0))
        col.null_count = int(data.get("null_count", 0))
        col.type_counts = Counter(data.get("type_counts", {}))
        for item in data.get("common_values", []):
            col._frequencies[item["value"]] = item["count"]
        if "min" in data:
            col.numeric_count = col.non_null_count
            col.numeric_min = data["min"]
            col.numeric_max = data["max"]
            col.numeric_sum = float(data.get("mean", 0.0)) * col.numeric_count
        return col


class Profiler:
    """多列、逐块的 profile 构建器。"""

    def __init__(self, columns: Iterable[str] | None = None, top_k: int = 10):
        self.top_k = top_k
        self.row_count = 0
        self.columns: dict[str, ColumnProfile] = {}
        if columns:
            for name in columns:
                self.columns[name] = ColumnProfile(name, top_k=top_k)

    def _column(self, name: str) -> ColumnProfile:
        col = self.columns.get(name)
        if col is None:
            col = ColumnProfile(name, top_k=self.top_k)
            self.columns[name] = col
        return col

    def update(self, rows: Iterable[dict]) -> "Profiler":
        for row in rows:
            self.row_count += 1
            for name, value in row.items():
                self._column(name).update([value])
        return self

    def update_stream(self, chunks: Iterator[list[dict]]) -> "Profiler":
        for chunk in chunks:
            self.update(chunk)
        return self

    def to_dict(self) -> dict:
        return {
            "profile_version": PROFILE_VERSION,
            "row_count": self.row_count,
            "column_count": len(self.columns),
            "columns": {name: col.to_dict() for name, col in self.columns.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Profiler":
        profiler = cls(top_k=10)
        profiler.row_count = int(data.get("row_count", 0))
        for name, col_data in data.get("columns", {}).items():
            profiler.columns[name] = ColumnProfile.from_dict(name, col_data)
        return profiler


def profile_rows(rows: list[dict], top_k: int = 10) -> dict:
    """对内存中的记录列表直接生成 profile（库 API 便捷函数）。"""
    return Profiler(top_k=top_k).update(rows).to_dict()
