"""质量规则引擎（流式）。

支持的规则类型：

================  ==============================  ==================================
类型              关键字段                         语义
================  ==============================  ==================================
not-null          column                          值不得为空
unique            column                          非空值不得重复（重复行均失败，
                                                  原因中给出首次出现行号）
range             column, min?, max?              数值闭区间；min/max 至少给一个；
                                                  非数值记为类型错误
regex             column, pattern                 将标量转为字符串后做整串匹配
allowed-values    column, values                  值必须出现在允许列表中
unique-composite  columns                         多列组合键唯一；键全为空的行豁免
================  ==============================  ==================================

空值（None）对除 not-null 外的规则一律豁免——空值问题交给 not-null 表达，
避免一行因同一个空值被多条规则重复计失败。

每条失败记录包含：rule_id / rule_type / column 或 columns / row_number /
reason，是“逐行”的，而不只是总体计数。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from .errors import RuleError
from .values import is_number, value_key

RULE_TYPES = frozenset(
    {"not-null", "unique", "range", "regex", "allowed-values", "unique-composite"}
)
_SINGLE_COLUMN_TYPES = frozenset({"not-null", "unique", "range", "regex", "allowed-values"})


@dataclass
class ValidationFailure:
    rule_id: str
    rule_type: str
    row_number: int
    reason: str
    column: str | None = None
    columns: list[str] | None = None
    value: Any = None

    def to_dict(self) -> dict:
        result = {
            "rule_id": self.rule_id,
            "rule_type": self.rule_type,
            "row_number": self.row_number,
            "reason": self.reason,
        }
        if self.column is not None:
            result["column"] = self.column
        if self.columns is not None:
            result["columns"] = list(self.columns)
        if self.value is not None or (self.column is not None and self.value is None):
            # 保留实际值便于排查；复杂类型转字符串
            try:
                json.dumps(self.value)
                result["value"] = self.value
            except TypeError:
                result["value"] = str(self.value)
        return result


@dataclass
class RuleStats:
    rule_id: str
    rule_type: str
    failures: int = 0
    passed: int = 0


@dataclass
class ValidationResult:
    total_rows: int = 0
    failures: list[ValidationFailure] = field(default_factory=list)
    stats: dict[str, RuleStats] = field(default_factory=dict)
    truncated: bool = False

    @property
    def passed(self) -> bool:
        return all(stat.failures == 0 for stat in self.stats.values())

    @property
    def failure_count(self) -> int:
        return sum(stat.failures for stat in self.stats.values())

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "total_rows": self.total_rows,
            "failure_count": self.failure_count,
            "stored_failures": len(self.failures),
            "truncated": self.truncated,
            "rules": [
                {
                    "rule_id": stat.rule_id,
                    "rule_type": stat.rule_type,
                    "passed": stat.passed,
                    "failed": stat.failures,
                }
                for stat in self.stats.values()
            ],
            "failures": [item.to_dict() for item in self.failures],
        }


class RuleEngine:
    """流式规则引擎：逐块喂入数据，最后取结果。"""

    def __init__(self, rule_specs: list[dict], max_failures: int | None = None):
        self.rules = [self._validate_spec(dict(spec)) for spec in rule_specs]
        if not self.rules:
            raise RuleError("规则列表为空")
        ids = [rule["id"] for rule in self.rules]
        duplicates = {rid for rid in ids if ids.count(rid) > 1}
        if duplicates:
            raise RuleError(f"规则 ID 重复: {sorted(duplicates)}")
        self.max_failures = max_failures
        self.result = ValidationResult()
        for rule in self.rules:
            self.result.stats[rule["id"]] = RuleStats(rule["id"], rule["type"])
        # 单列 unique / 复合键 -> {键: 首次出现行号}
        self._seen: dict[str, dict[Any, int]] = {rule["id"]: {} for rule in self.rules}
        self._columns_checked: set[str] = set()

    # ---- 规则配置校验 -----------------------------------------------------

    @staticmethod
    def _validate_spec(spec: dict) -> dict:
        rule_id = spec.get("id")
        if not rule_id or not isinstance(rule_id, str):
            raise RuleError(f"规则缺少字符串类型的 id: {spec}")
        rule_type = spec.get("type")
        if rule_type not in RULE_TYPES:
            raise RuleError(f"规则 {rule_id} 的类型不受支持: {rule_type!r}")

        if rule_type in _SINGLE_COLUMN_TYPES:
            column = spec.get("column")
            if not column or not isinstance(column, str):
                raise RuleError(f"规则 {rule_id} 需要字符串字段 column")
        if rule_type == "unique-composite":
            columns = spec.get("columns")
            if not isinstance(columns, list) or not columns or not all(isinstance(c, str) and c for c in columns):
                raise RuleError(f"规则 {rule_id} 需要非空字符串列表 columns")
            if len(set(columns)) != len(columns):
                raise RuleError(f"规则 {rule_id} 的 columns 存在重复列")

        if rule_type == "range":
            minimum = spec.get("min")
            maximum = spec.get("max")
            if minimum is None and maximum is None:
                raise RuleError(f"规则 {rule_id} 至少需要 min 或 max")
            for bound, name in ((minimum, "min"), (maximum, "max")):
                if bound is not None and not is_number(bound):
                    raise RuleError(f"规则 {rule_id} 的 {name} 必须是数值")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise RuleError(f"规则 {rule_id} 的 min({minimum}) 大于 max({maximum})")
            spec["min"] = minimum
            spec["max"] = maximum

        if rule_type == "regex":
            pattern = spec.get("pattern")
            if not isinstance(pattern, str):
                raise RuleError(f"规则 {rule_id} 需要字符串字段 pattern")
            try:
                spec["_compiled"] = re.compile(pattern)
            except re.error as exc:
                raise RuleError(f"规则 {rule_id} 的正则无法编译: {exc}") from exc

        if rule_type == "allowed-values":
            values = spec.get("values")
            if not isinstance(values, list) or not values:
                raise RuleError(f"规则 {rule_id} 需要非空列表 values")
            spec["_value_set"] = {value_key(v) for v in values}
            spec["_value_list"] = list(values)

        return spec

    # ---- 列存在性 ---------------------------------------------------------

    def check_columns(self, known_columns: Iterable[str]) -> None:
        """根据已知列名校验规则引用；缺失直接抛 RuleError。"""
        known = set(known_columns)
        for rule in self.rules:
            refs = [rule["column"]] if "column" in rule else list(rule["columns"])
            missing = [name for name in refs if name not in known]
            if missing:
                raise RuleError(f"规则 {rule['id']} 引用了数据中不存在的列: {missing}")

    # ---- 流式处理 ---------------------------------------------------------

    def process_chunk(self, rows: list[dict], start_row: int = 1) -> None:
        for offset, row in enumerate(rows):
            row_number = start_row + offset
            self.result.total_rows += 1
            for rule in self.rules:
                self._evaluate(rule, row, row_number)

    def process_stream(self, chunks: Iterator[list[dict]], known_columns: Iterable[str] | None = None) -> ValidationResult:
        if known_columns is not None:
            self.check_columns(known_columns)
        start_row = 1
        for chunk in chunks:
            self.process_chunk(chunk, start_row)
            start_row += len(chunk)
        return self.result

    def _add_failure(self, rule: dict, row_number: int, reason: str, value: Any,
                     column: str | None = None, columns: list[str] | None = None) -> None:
        stats = self.result.stats[rule["id"]]
        stats.failures += 1
        if self.max_failures is None or len(self.result.failures) < self.max_failures:
            self.result.failures.append(
                ValidationFailure(
                    rule_id=rule["id"],
                    rule_type=rule["type"],
                    row_number=row_number,
                    reason=reason,
                    value=value,
                    column=column,
                    columns=columns,
                )
            )
        else:
            self.result.truncated = True

    def _evaluate(self, rule: dict, row: dict, row_number: int) -> None:
        stats = self.result.stats[rule["id"]]
        rtype = rule["type"]

        if rtype == "unique-composite":
            names = rule["columns"]
            values = [row.get(name) for name in names]
            if all(value is None for value in values):
                stats.passed += 1
                return
            key = tuple(value_key(v) for v in values)
            seen = self._seen[rule["id"]]
            if key in seen:
                self._add_failure(
                    rule, row_number,
                    f"复合键 {names}={values} 重复，首次出现于第 {seen[key]} 行",
                    value=values, columns=names,
                )
            else:
                seen[key] = row_number
                stats.passed += 1
            return

        name = rule["column"]
        value = row.get(name)

        if rtype == "not-null":
            if value is None:
                self._add_failure(rule, row_number, "字段为空", value=None, column=name)
            else:
                stats.passed += 1
            return

        # 其余单列规则：空值豁免
        if value is None:
            stats.passed += 1
            return

        if rtype == "unique":
            key = value_key(value)
            seen = self._seen[rule["id"]]
            if key in seen:
                self._add_failure(
                    rule, row_number,
                    f"值 {value!r} 重复，首次出现于第 {seen[key]} 行",
                    value=value, column=name,
                )
            else:
                seen[key] = row_number
                stats.passed += 1

        elif rtype == "range":
            if not is_number(value):
                self._add_failure(
                    rule, row_number,
                    f"类型错误：期望数值，实际为 {type(value).__name__}({value!r})",
                    value=value, column=name,
                )
                return
            minimum, maximum = rule["min"], rule["max"]
            if minimum is not None and value < minimum:
                self._add_failure(rule, row_number, f"值 {value} 小于下限 {minimum}", value=value, column=name)
            elif maximum is not None and value > maximum:
                self._add_failure(rule, row_number, f"值 {value} 超过上限 {maximum}", value=value, column=name)
            else:
                stats.passed += 1

        elif rtype == "regex":
            text = value if isinstance(value, str) else str(value)
            if rule["_compiled"].search(text):
                stats.passed += 1
            else:
                self._add_failure(
                    rule, row_number,
                    f"值 {text!r} 不匹配正则 {rule['pattern']!r}",
                    value=value, column=name,
                )

        elif rtype == "allowed-values":
            if value_key(value) in rule["_value_set"]:
                stats.passed += 1
            else:
                self._add_failure(
                    rule, row_number,
                    f"值 {value!r} 不在允许列表 {rule['_value_list']} 中",
                    value=value, column=name,
                )


def load_rules(path: str) -> list[dict]:
    """从本地 JSON 文件加载规则。

    顶层可以是规则列表，也可以是 ``{"rules": [...]}``。
    """
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise RuleError(f"规则文件不是合法 JSON: {exc}") from exc
    if isinstance(data, dict) and "rules" in data:
        data = data["rules"]
    if not isinstance(data, list):
        raise RuleError("规则文件顶层必须是列表或 {'rules': [...]} 对象")
    return data


def validate_rows(
    rows: list[dict],
    rule_specs: list[dict],
    max_failures: int | None = None,
) -> ValidationResult:
    """对内存记录直接执行规则（库 API 便捷函数）。"""
    engine = RuleEngine(rule_specs, max_failures=max_failures)
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return engine.process_stream(iter([rows]), known_columns=columns)
