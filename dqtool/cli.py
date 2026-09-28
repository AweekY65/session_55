"""Command-line interface for dqtool.

Exit codes (usable from terminals and CI)
-----------------------------------------
0  validation passed / profiles match
1  validation found rule failures or baseline comparison found changes
2  input error (missing/corrupt/empty dataset, unreadable format)
3  configuration error (bad rule file, bad CLI parameters)
4  unexpected internal error
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .baseline import (
    DEFAULT_NULL_RATE_DELTA,
    DEFAULT_RANGE_TOLERANCE,
    compare_profiles,
)
from .errors import DQConfigError, DQError, DQInputError
from .profiler import DEFAULT_TOP_K, profile_dataset
from .readers import read_chunks
from .report import error_report, write_json_report
from .rules import load_rules
from .validation import DEFAULT_MAX_FAILURES, validate_chunks

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INPUT_ERROR = 2
EXIT_CONFIG_ERROR = 3
EXIT_INTERNAL_ERROR = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dqtool",
        description=(
            "Local data-quality analysis and validation for CSV/JSONL/Parquet "
            "files. No external services."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"dqtool {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # profile ---------------------------------------------------------
    p_profile = sub.add_parser(
        "profile", help="generate a column profile for a dataset"
    )
    _add_data_args(p_profile)
    p_profile.add_argument(
        "-o", "--output", help="write profile JSON to this path (default stdout)"
    )
    p_profile.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help="number of common values per column (default %(default)s)",
    )

    # validate --------------------------------------------------------
    p_validate = sub.add_parser(
        "validate", help="validate a dataset against quality rules"
    )
    _add_data_args(p_validate)
    p_validate.add_argument(
        "-r", "--rules", required=True, help="path to the rules JSON file"
    )
    p_validate.add_argument(
        "-o",
        "--report",
        help="write the JSON quality report to this path (default stdout)",
    )
    p_validate.add_argument(
        "--max-failures",
        type=int,
        default=DEFAULT_MAX_FAILURES,
        help=(
            "max failure details retained per rule (counts always exact; "
            "default %(default)s)"
        ),
    )
    p_validate.add_argument(
        "--include-values",
        action="store_true",
        help="include the offending value in each failure record",
    )

    # compare ---------------------------------------------------------
    p_compare = sub.add_parser(
        "compare", help="compare a dataset profile against a baseline profile"
    )
    p_compare.add_argument("baseline", help="baseline profile JSON path")
    p_compare.add_argument("current", help="current profile JSON path")
    p_compare.add_argument(
        "-o", "--output", help="write diff JSON to this path (default stdout)"
    )
    p_compare.add_argument(
        "--null-rate-delta",
        type=float,
        default=DEFAULT_NULL_RATE_DELTA,
        help=(
            "absolute null-rate change that counts as significant "
            "(default %(default)s)"
        ),
    )
    p_compare.add_argument(
        "--range-tolerance",
        type=float,
        default=DEFAULT_RANGE_TOLERANCE,
        help=(
            "fraction of baseline numeric range tolerated beyond baseline "
            "min/max (default %(default)s)"
        ),
    )
    return parser


def _add_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("data", help="path to the dataset file")
    parser.add_argument(
        "-f",
        "--format",
        choices=["csv", "jsonl", "parquet"],
        help="force input format (default: infer from extension)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=50_000,
        help="rows per streaming chunk (default %(default)s)",
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "profile":
            return _cmd_profile(args)
        if args.command == "validate":
            return _cmd_validate(args)
        if args.command == "compare":
            return _cmd_compare(args)
        parser.error(f"unknown command {args.command!r}")
        return EXIT_INTERNAL_ERROR
    except DQInputError as exc:
        _emit_error(exc, args, "InputError")
        return EXIT_INPUT_ERROR
    except DQConfigError as exc:
        _emit_error(exc, args, "ConfigError")
        return EXIT_CONFIG_ERROR
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        _emit_error(exc, args, type(exc).__name__)
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        _emit_error(exc, args, type(exc).__name__)
        return EXIT_INTERNAL_ERROR


def _emit_error(exc: Exception, args: argparse.Namespace, kind: str) -> None:
    payload = error_report(str(exc), kind)
    out_path = getattr(args, "report", None) or getattr(args, "output", None)
    # Errors go to stderr as JSON so a half-written --report file never
    # looks like a passing report.
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)


def _cmd_profile(args: argparse.Namespace) -> int:
    chunks = read_chunks(
        args.data, fmt=args.format, chunk_size=args.chunk_size
    )
    profile = profile_dataset(chunks, top_k=args.top_k)
    write_json_report(profile, args.output)
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    rules = load_rules(args.rules)
    chunks = read_chunks(
        args.data, fmt=args.format, chunk_size=args.chunk_size
    )
    report = validate_chunks(
        rules, chunks, max_failures=args.max_failures
    ).to_dict(include_values=args.include_values)
    write_json_report(report, args.report)
    return EXIT_OK if report["status"] == "passed" else EXIT_FAILED


def _cmd_compare(args: argparse.Namespace) -> int:
    baseline = _load_profile(args.baseline)
    current = _load_profile(args.current)
    diff = compare_profiles(
        baseline,
        current,
        null_rate_delta=args.null_rate_delta,
        range_tolerance=args.range_tolerance,
    )
    diff["status"] = "changed" if diff["has_changes"] else "matched"
    write_json_report(diff, args.output)
    return EXIT_FAILED if diff["has_changes"] else EXIT_OK


def _load_profile(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise DQConfigError(f"profile file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DQConfigError(f"invalid JSON in profile {path}: {exc}") from exc
    if not isinstance(data, dict) or "columns" not in data:
        raise DQConfigError(
            f"{path} is not a dqtool profile (missing 'columns' section)"
        )
    return data


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
