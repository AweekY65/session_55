"""Error types for dqtool.

All errors raised by dqtool derive from :class:`DQError` so callers can
catch every tool-level problem with a single except clause.
"""


class DQError(Exception):
    """Base class for all dqtool errors."""


class DQInputError(DQError):
    """The dataset cannot be read or is malformed (missing file, bad JSONL,
    corrupt Parquet, empty file, ...)."""


class DQConfigError(DQError):
    """A rule file / parameter is invalid (unknown rule type, bad regex, ...)."""
