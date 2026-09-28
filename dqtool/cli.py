"""命令行入口。

退出码：
    0  质量检查通过 / 无基线差异
    1  发现规则失败或基线差异
    2  用法、文件、规则或输入数据本身的错误（含损坏文件）

用法示例见 README。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from . import __version__
from .baseline import compare_profiles
from .errors import CorruptDataError, DQError, RuleError
from .profiler import Profiler
from .readers import infer_format, open_dataset
from .rules import RuleEngine, load_rules
from .values import make_null_tokens, normalize_row

EXIT_OK = 0
EXIT_QUALITY_FAILED = 1
EXIT_USAGE_OR_INPUT_ERROR = 2


class _ReportEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, (set, frozenset)):
            return sorted(obj, key=str)
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return str(obj)


def _dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, cls=_ReportEncoder, allow_nan=False)


def _parse_null_values(raw: str | None) -> frozenset[str] | None:
    if raw is None:
        return None
    # 逗号分隔；空项表示空字符串。如 "NA,," 表示 {"", "na"}
    return make_null_tokens([item for item in raw.split(",")])


def _resolve_format(path: str, fmt: str | None) -> str:
    if fmt:
        return fmt.lower()
    try:
        return infer_format(path)
    except ValueError:
        raise


def _write_output(text: str, path: str | None) -> None:
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
    else:
        print(text)


# ---- profile ----------------------------------------------------------------

def cmd_profile(args: argparse.Namespace) -> int:
    fmt = _resolve_format(args.data, args.format)
    null_tokens = _parse_null_values(args.null_values)
    profiler = Profiler(top_k=args.top_k)

    started = time.time()
    with open_dataset(args.data, fmt=fmt, chunksize=args.chunk_size,
                      strict_csv=not args.lenient_csv) as reader:
        for chunk in reader:
            normalized = [normalize_row(row, null_tokens) for row in chunk]
            profiler.update(normalized)
    elapsed = time.time() - started

    report = {
        "tool": "dqtool",
        "version": __version__,
        "command": "profile",
        "status": "ok",
        "data_source": os.fspath(args.data),
        "format": fmt,
        "elapsed_seconds": round(elapsed, 4),
        "profile": profiler.to_dict(),
    }
    _write_output(_dump_json(report), args.output)
    print(f"[dqtool] profile 完成：{profiler.row_count} 行，{len(profiler.columns)} 列",
          file=sys.stderr)
    return EXIT_OK


# ---- validate ---------------------------------------------------------------

def cmd_validate(args: argparse.Namespace) -> int:
    fmt = _resolve_format(args.data, args.format)
    try:
        rule_specs = load_rules(args.rules)
    except FileNotFoundError:
        print(f"[dqtool] 错误：规则文件不存在: {args.rules}", file=sys.stderr)
        return EXIT_USAGE_OR_INPUT_ERROR
    except RuleError as exc:
        print(f"[dqtool] 规则错误：{exc}", file=sys.stderr)
        return EXIT_USAGE_OR_INPUT_ERROR

    try:
        engine = RuleEngine(rule_specs, max_failures=args.max_failures)
    except RuleError as exc:
        print(f"[dqtool] 规则错误：{exc}", file=sys.stderr)
        return EXIT_USAGE_OR_INPUT_ERROR

    null_tokens = _parse_null_values(args.null_values)
    started = time.time()
    columns_verified = False
    with open_dataset(args.data, fmt=fmt, chunksize=args.chunk_size,
                      strict_csv=not args.lenient_csv) as reader:
        start_row = 1
        for chunk in reader:
            if not columns_verified:
                engine.check_columns(reader.columns)
                columns_verified = True
            normalized = [normalize_row(row, null_tokens) for row in chunk]
            engine.process_chunk(normalized, start_row)
            start_row += len(chunk)
    elapsed = time.time() - started

    result = engine.result
    status = "passed" if result.passed else "failed"
    report = {
        "tool": "dqtool",
        "version": __version__,
        "command": "validate",
        "status": status,
        "data_source": os.fspath(args.data),
        "format": fmt,
        "rules_source": os.fspath(args.rules),
        "elapsed_seconds": round(elapsed, 4),
        "result": result.to_dict(),
    }
    _write_output(_dump_json(report), args.output)

    print(
        f"[dqtool] 校验{'通过' if result.passed else '失败'}："
        f"{result.total_rows} 行，{result.failure_count} 条失败"
        + ("（失败明细已截断）" if result.truncated else ""),
        file=sys.stderr,
    )
    if not result.passed:
        for failure in result.failures[: args.show_failures]:
            target = failure.column or ",".join(failure.columns or [])
            print(f"  第 {failure.row_number} 行 [{failure.rule_id}] {target}: {failure.reason}",
                  file=sys.stderr)
        stored = len(result.failures)
        if stored > args.show_failures:
            print(f"  …另有 {stored - args.show_failures} 条失败未在摘要中展示（详见 JSON 报告）",
                  file=sys.stderr)
        if result.truncated:
            print(f"  注意：失败明细已按 --max-failures 截断，实际失败 {result.failure_count} 条",
                  file=sys.stderr)
    return EXIT_OK if result.passed else EXIT_QUALITY_FAILED


# ---- baseline ---------------------------------------------------------------

def _load_profile_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    # 既支持裸 profile，也支持 `dqtool profile -o` 的包装报告
    if isinstance(data, dict) and "profile" in data and isinstance(data["profile"], dict):
        return data["profile"]
    if not isinstance(data, dict) or "columns" not in data:
        raise RuleError(f"{path} 不是有效的 profile 文件（缺少 columns）")
    return data


def cmd_baseline(args: argparse.Namespace) -> int:
    if args.current_profile:
        # 两份 profile 文件直接比较
        try:
            baseline_profile = _load_profile_json(args.baseline)
            current_profile = _load_profile_json(args.current_profile)
        except FileNotFoundError as exc:
            print(f"[dqtool] 错误：文件不存在: {exc.filename}", file=sys.stderr)
            return EXIT_USAGE_OR_INPUT_ERROR
        except (json.JSONDecodeError, RuleError) as exc:
            print(f"[dqtool] profile 错误：{exc}", file=sys.stderr)
            return EXIT_USAGE_OR_INPUT_ERROR
        source_desc = os.fspath(args.current_profile)
    else:
        # 对数据现场生成 profile，再与基线比较
        fmt = _resolve_format(args.data, args.format)
        null_tokens = _parse_null_values(args.null_values)
        try:
            baseline_profile = _load_profile_json(args.baseline)
        except FileNotFoundError:
            print(f"[dqtool] 错误：基线文件不存在: {args.baseline}", file=sys.stderr)
            return EXIT_USAGE_OR_INPUT_ERROR
        except (json.JSONDecodeError, RuleError) as exc:
            print(f"[dqtool] 基线错误：{exc}", file=sys.stderr)
            return EXIT_USAGE_OR_INPUT_ERROR

        profiler = Profiler(top_k=args.top_k)
        with open_dataset(args.data, fmt=fmt, chunksize=args.chunk_size,
                          strict_csv=not args.lenient_csv) as reader:
            for chunk in reader:
                normalized = [normalize_row(row, null_tokens) for row in chunk]
                profiler.update(normalized)
        current_profile = profiler.to_dict()
        source_desc = os.fspath(args.data)

    diff = compare_profiles(
        baseline_profile,
        current_profile,
        null_rate_abs=args.null_rate_abs,
        null_rate_rel=args.null_rate_rel,
        range_rel=args.range_rel,
        range_abs=args.range_abs,
    )
    status = "passed" if diff.passed else "drift_detected"
    report = {
        "tool": "dqtool",
        "version": __version__,
        "command": "baseline",
        "status": status,
        "data_source": source_desc,
        "baseline_source": os.fspath(args.baseline),
        "thresholds": {
            "null_rate_abs": args.null_rate_abs,
            "null_rate_rel": args.null_rate_rel,
            "range_rel": args.range_rel,
            "range_abs": args.range_abs,
        },
        "diff": diff.to_dict(),
    }
    _write_output(_dump_json(report), args.output)

    if diff.passed:
        print("[dqtool] 基线比较通过：未发现明显变化", file=sys.stderr)
        return EXIT_OK
    print(f"[dqtool] 基线比较发现 {len(diff.findings)} 处变化：", file=sys.stderr)
    for finding in diff.findings:
        print(f"  [{finding['kind']}] {finding.get('column', '-')}: {finding['reason']}",
              file=sys.stderr)
    return EXIT_QUALITY_FAILED


# ---- argparse ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dqtool",
        description="本地数据质量分析与验证工具（CSV / JSONL / Parquet，纯本地）",
    )
    parser.add_argument("--version", action="version", version=f"dqtool {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common_io(p: argparse.ArgumentParser, *, null_values: bool = True) -> None:
        p.add_argument("--format", choices=["csv", "jsonl", "parquet"],
                       help="强制指定数据格式，默认按扩展名推断")
        p.add_argument("--chunk-size", type=int, default=10_000,
                       help="分块大小（行数），默认 10000；工具始终流式处理")
        p.add_argument("--lenient-csv", action="store_true",
                       help="CSV 行列数不一致时不报错（截断/补空），默认严格报错")
        p.add_argument("-o", "--output", help="JSON 报告输出路径；不指定则打印到标准输出")
        if null_values:
            p.add_argument("--null-values",
                           help="逗号分隔的空值词表（大小写不敏感），如 'NA,NULL,\\\\N'；"
                                "空项表示空字符串。默认：'',NA,N/A,NULL")

    p_profile = sub.add_parser("profile", help="对数据集生成字段 profile")
    p_profile.add_argument("data", help="数据文件路径（.csv/.jsonl/.ndjson/.parquet）")
    p_profile.add_argument("--top-k", type=int, default=10,
                           help="common_values 保留的高频值个数，默认 10")
    add_common_io(p_profile)
    p_profile.set_defaults(func=cmd_profile)

    p_validate = sub.add_parser("validate", help="按规则文件校验数据并输出逐行失败记录")
    p_validate.add_argument("data", help="数据文件路径")
    p_validate.add_argument("rules", help="规则 JSON 文件路径")
    p_validate.add_argument("--max-failures", type=int, default=None,
                            help="报告中最多保存的失败明细条数（计数不受影响），默认不限制")
    p_validate.add_argument("--show-failures", type=int, default=20,
                            help="终端摘要中展示的失败条数，默认 20")
    add_common_io(p_validate)
    p_validate.set_defaults(func=cmd_validate)

    p_baseline = sub.add_parser("baseline", help="将当前数据/当前 profile 与基线 profile 比较")
    p_baseline.add_argument("baseline", help="基线 profile JSON 文件")
    p_baseline.add_argument("data", nargs="?", help="当前数据文件（与 --current-profile 二选一）")
    p_baseline.add_argument("--current-profile",
                            help="直接指定当前 profile JSON，与已保存基线比较，无需数据文件")
    p_baseline.add_argument("--top-k", type=int, default=10)
    p_baseline.add_argument("--null-rate-abs", type=float, default=0.10,
                            help="空值率绝对差阈值，默认 0.10")
    p_baseline.add_argument("--null-rate-rel", type=float, default=0.50,
                            help="空值率相对差阈值，默认 0.50")
    p_baseline.add_argument("--range-rel", type=float, default=0.20,
                            help="数值边界相对越界阈值，默认 0.20")
    p_baseline.add_argument("--range-abs", type=float, default=0.0,
                            help="基线跨度为 0 时的绝对越界容差，默认 0")
    add_common_io(p_baseline)
    p_baseline.set_defaults(func=cmd_baseline)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) == "baseline" and not args.current_profile and not args.data:
        parser.error("baseline 需要提供数据文件，或使用 --current-profile 指定当前 profile")

    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"[dqtool] 错误：文件不存在: {exc.filename}", file=sys.stderr)
        return EXIT_USAGE_OR_INPUT_ERROR
    except CorruptDataError as exc:
        print(f"[dqtool] 数据损坏：{exc}", file=sys.stderr)
        return EXIT_USAGE_OR_INPUT_ERROR
    except (RuleError, DQError, ValueError) as exc:
        print(f"[dqtool] 错误：{exc}", file=sys.stderr)
        return EXIT_USAGE_OR_INPUT_ERROR
    except json.JSONDecodeError as exc:  # 输出阶段理论上不会触发，防御性处理
        print(f"[dqtool] 内部错误：生成 JSON 失败: {exc}", file=sys.stderr)
        return EXIT_USAGE_OR_INPUT_ERROR


if __name__ == "__main__":
    sys.exit(main())
