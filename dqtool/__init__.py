"""dqtool — 本地数据质量分析与验证工具。

所有数据均来自本地 CSV / JSONL / Parquet 文件或内存中的 dict 记录，
不依赖任何数据仓库、Great Expectations 服务或云平台。
"""

from .profiler import ColumnProfile, Profiler, profile_rows
from .rules import RuleEngine, RuleError, ValidationFailure, load_rules, validate_rows
from .baseline import BaselineDiff, compare_profiles
from .readers import (
    ChunkReader,
    CorruptDataError,
    infer_format,
    open_dataset,
    stream_file,
)

__version__ = "0.1.0"

__all__ = [
    "ColumnProfile",
    "Profiler",
    "profile_rows",
    "RuleEngine",
    "RuleError",
    "ValidationFailure",
    "load_rules",
    "validate_rows",
    "BaselineDiff",
    "compare_profiles",
    "ChunkReader",
    "CorruptDataError",
    "infer_format",
    "open_dataset",
    "stream_file",
]
