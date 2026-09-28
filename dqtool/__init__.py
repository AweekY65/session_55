"""dqtool -- local data-quality analysis and validation.

Everything operates on local CSV / JSONL / Parquet files or in-memory
records; no data warehouse, Great Expectations service, cloud platform or
other external service is contacted.
"""

from __future__ import annotations

from .baseline import compare_profiles
from .errors import DQConfigError, DQError, DQInputError
from .profiler import ProfileBuilder, profile_dataset
from .readers import read_chunks, rows_from_records
from .rules import Rule, load_rules, parse_rules
from .validation import (
    Failure,
    ValidationReport,
    validate_chunks,
    validate_records,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "DQError",
    "DQInputError",
    "DQConfigError",
    "Rule",
    "load_rules",
    "parse_rules",
    "read_chunks",
    "rows_from_records",
    "ProfileBuilder",
    "profile_dataset",
    "validate_chunks",
    "validate_records",
    "ValidationReport",
    "Failure",
    "compare_profiles",
]
