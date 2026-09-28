"""值归一化：把 CSV/JSONL/Parquet 读出的原始值统一成 Python 标量。

CSV 中所有值天然是字符串；JSONL 可以带 bool/int/float/None；
Parquet 通过 pyarrow 得到带类型的 Python 对象。统一在这里完成
字符串 -> 类型（int/float/bool/None）的推断，使规则引擎与 profile
无需关心数据来自哪种格式。
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

# 整数：可选符号 + 纯数字（不接受 "1.0"、"1_000"、" 1 " 之外的空白由调用方 strip）
_INT_RE = re.compile(r"^[+-]?\d+$")
# 浮点：必须含小数点或指数，且 float() 能解析且结果有限
_FLOAT_CANDIDATE_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")

_TRUE_TOKENS = frozenset({"true", "True", "TRUE"})
_FALSE_TOKENS = frozenset({"false", "False", "FALSE"})


def make_null_tokens(tokens: Iterable[str] | None) -> frozenset[str]:
    """构造“空值词表”。结果始终包含空字符串。

    None 时使用默认词表：空串、NA、N/A、NULL（大小写不敏感，
    所以内部统一存小写形式，空串也存 ""）。
    """
    if tokens is None:
        normalized = {"", "na", "n/a", "null"}
    else:
        normalized = {""}
        for token in tokens:
            if token is not None:
                normalized.add(str(token).strip().lower())
    return frozenset(normalized)


def _coerce_scalar(value: Any, null_tokens: frozenset[str]) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # NaN 与无穷大不是有效质量数据，统一记为空，同时保证 JSON 可序列化
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in null_tokens:
            return None
        if stripped in _TRUE_TOKENS:
            return True
        if stripped in _FALSE_TOKENS:
            return False
        if _INT_RE.match(stripped):
            try:
                return int(stripped)
            except ValueError:  # pragma: no cover - 正则已保证
                return value
        if _FLOAT_CANDIDATE_RE.match(stripped) and ("." in stripped or "e" in stripped or "E" in stripped):
            try:
                number = float(stripped)
            except ValueError:  # pragma: no cover
                return value
            return number if math.isfinite(number) else None
        return value
    # date/time/dict/list 等复杂类型原样保留
    return value


def normalize_value(value: Any, null_tokens: Iterable[str] | None = None) -> Any:
    """归一化单个值。"""
    tokens = make_null_tokens(null_tokens) if not isinstance(null_tokens, frozenset) else null_tokens
    return _coerce_scalar(value, tokens)


def normalize_row(row: dict, null_tokens: Iterable[str] | frozenset | None = None) -> dict:
    """归一化一行记录中的每个值。"""
    tokens = null_tokens if isinstance(null_tokens, frozenset) else make_null_tokens(null_tokens)
    return {key: _coerce_scalar(value, tokens) for key, value in row.items()}


def value_key(value: Any) -> Any:
    """用于唯一性 / 频次统计的可哈希键。

    - bool 与 int 天然可哈希且 True == 1，保持 Python 语义；
    - float 与 int 相等时（1 与 1.0）归为同一键，符合数据质量直觉；
    - 不可哈希值（dict/list）退化为类型标记 + JSON 文本。
    """
    try:
        hash(value)
        return value
    except TypeError:
        import json

        try:
            return ("__complex__", type(value).__name__, json.dumps(value, sort_keys=True, default=str))
        except (TypeError, ValueError):
            return ("__complex__", type(value).__name__, repr(value))


def is_number(value: Any) -> bool:
    """是否为数值（bool 不算数值，避免 True 被当作 1）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)
