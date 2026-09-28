"""基线 profile 比较。

检测五类变化：

1. ``column_missing``  基线有、当前没有的列；
2. ``column_added``    当前新增的列；
3. ``type_change``     推断类型变化（int/float/bool/string/...）；
4. ``null_rate_shift`` 空值率明显变化（绝对差或相对差超阈值）；
5. ``range_anomaly``   数值列的 min/max 明显超出基线范围。

所有阈值可配置，默认值面向常见数据集：

- ``null_rate_abs``     空值率绝对差阈值 0.10（10 个百分点）
- ``null_rate_rel``     空值率相对差阈值 0.50（±50%）
- ``range_rel``         数值边界相对越界阈值 0.20（20%）
- ``range_abs``         基线跨度为 0 时使用的绝对容差
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .profiler import Profiler


@dataclass
class BaselineDiff:
    findings: list[dict] = field(default_factory=list)
    baseline_row_count: int = 0
    current_row_count: int = 0

    @property
    def passed(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "finding_count": len(self.findings),
            "baseline_row_count": self.baseline_row_count,
            "current_row_count": self.current_row_count,
            "findings": self.findings,
        }


def _as_profiles(profile: dict | Profiler) -> dict:
    if isinstance(profile, Profiler):
        return profile.to_dict()
    return profile


def compare_profiles(
    baseline: dict | Profiler,
    current: dict | Profiler,
    *,
    null_rate_abs: float = 0.10,
    null_rate_rel: float = 0.50,
    range_rel: float = 0.20,
    range_abs: float = 0.0,
) -> BaselineDiff:
    """比较两份 profile（``Profiler`` 或其 ``to_dict()`` 结果均可）。"""
    base = _as_profiles(baseline)
    cur = _as_profiles(current)
    base_cols: dict[str, dict] = base.get("columns", {})
    cur_cols: dict[str, dict] = cur.get("columns", {})

    diff = BaselineDiff(
        baseline_row_count=int(base.get("row_count", 0)),
        current_row_count=int(cur.get("row_count", 0)),
    )
    findings: list[dict] = []

    for name in base_cols:
        if name not in cur_cols:
            findings.append(
                {"kind": "column_missing", "column": name,
                 "reason": "基线中存在的列在当前数据中缺失"}
            )
    for name in cur_cols:
        if name not in base_cols:
            findings.append(
                {"kind": "column_added", "column": name,
                 "reason": "当前数据出现基线中没有的新列"}
            )

    for name in sorted(set(base_cols) & set(cur_cols)):
        b = base_cols[name]
        c = cur_cols[name]

        # --- 类型变化 ---
        b_type, c_type = b.get("inferred_type"), c.get("inferred_type")
        # 基线上全空（null）的列不参与类型漂移判断，只看空值率
        if b_type != c_type and b_type != "null":
            findings.append(
                {"kind": "type_change", "column": name,
                 "baseline_type": b_type, "current_type": c_type,
                 "reason": f"推断类型由 {b_type} 变为 {c_type}"}
            )

        # --- 空值率变化（绝对差或相对差任一超阈值即报警） ---
        b_rate = float(b.get("null_rate", 0.0))
        c_rate = float(c.get("null_rate", 0.0))
        delta = c_rate - b_rate
        abs_delta = abs(delta)
        rel_delta = abs_delta / b_rate if b_rate > 0 else (abs_delta if c_rate > 0 else 0.0)
        if abs_delta >= null_rate_abs or (b_rate > 0 and rel_delta >= null_rate_rel):
            findings.append(
                {"kind": "null_rate_shift", "column": name,
                 "baseline_null_rate": b_rate, "current_null_rate": c_rate,
                 "delta": round(delta, 6),
                 "reason": f"空值率由 {b_rate:.2%} 变为 {c_rate:.2%}（变化 {delta:+.2%}）"}
            )

        # --- 数值范围异常 ---
        if "min" in b and "min" in c and b_type in ("int", "float") and c_type in ("int", "float"):
            b_min, b_max = b["min"], b["max"]
            c_min, c_max = c["min"], c["max"]
            span = abs(b_max - b_min)
            tolerance = max(range_abs, span * range_rel)
            if c_min < b_min - tolerance:
                findings.append(
                    {"kind": "range_anomaly", "column": name, "boundary": "min",
                     "baseline_min": b_min, "current_min": c_min,
                     "reason": f"最小值 {c_min} 明显低于基线 {b_min}（容差 {tolerance:g}）"}
                )
            if c_max > b_max + tolerance:
                findings.append(
                    {"kind": "range_anomaly", "column": name, "boundary": "max",
                     "baseline_max": b_max, "current_max": c_max,
                     "reason": f"最大值 {c_max} 明显高于基线 {b_max}（容差 {tolerance:g}）"}
                )

    # 稳定排序：先列名，再问题类别
    kind_order = {"column_missing": 0, "column_added": 1, "type_change": 2,
                  "null_rate_shift": 3, "range_anomaly": 4}
    findings.sort(key=lambda item: (item.get("column", ""), kind_order.get(item["kind"], 9)))
    diff.findings = findings
    return diff
