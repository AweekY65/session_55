"""CLI 端到端测试：JSON 报告内容与退出码（可直接在终端复现）。"""

import json
import subprocess
import sys

import pytest

DQTOOL = [sys.executable, "-m", "dqtool"]


def run_cli(*args, cwd=None):
    proc = subprocess.run(
        DQTOOL + list(args),
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return proc


def load_report(stdout_or_path, path=None):
    if path is not None:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(stdout_or_path)


def test_cli_profile_exit_0(write_csv):
    path = write_csv("d.csv", [{"id": 1, "v": "1.5"}])
    proc = run_cli("profile", path)
    assert proc.returncode == 0, proc.stderr
    report = load_report(proc.stdout)
    assert report["status"] == "ok"
    assert report["profile"]["row_count"] == 1
    assert report["profile"]["columns"]["v"]["inferred_type"] == "float"


def test_cli_validate_exit_codes(write_csv, tmp_path):
    good = write_csv("good.csv", [{"id": "1", "v": "10"}])
    bad = write_csv("bad.csv", [{"id": "1", "v": "200"}])
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([
        {"id": "R1", "type": "range", "column": "v", "min": 0, "max": 100},
    ]), encoding="utf-8")

    ok = run_cli("validate", good, str(rules))
    assert ok.returncode == 0
    assert load_report(ok.stdout)["status"] == "passed"

    failed = run_cli("validate", bad, str(rules))
    assert failed.returncode == 1
    report = load_report(failed.stdout)
    assert report["status"] == "failed"
    failure = report["result"]["failures"][0]
    assert failure["rule_id"] == "R1"
    assert failure["row_number"] == 1
    assert failure["column"] == "v"
    assert failure["reason"]


def test_cli_validate_report_to_file(write_csv, tmp_path):
    data = write_csv("d.csv", [{"id": "1"}])
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"id": "R1", "type": "unique", "column": "id"}]),
                     encoding="utf-8")
    out = tmp_path / "report.json"
    proc = run_cli("validate", data, str(rules), "-o", str(out))
    assert proc.returncode == 0
    report = load_report(None, str(out))
    assert report["result"]["passed"] is True
    assert proc.stdout == ""  # 输出到文件时 stdout 不再打印报告


def test_cli_corrupt_csv_exit_2(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("a,b\n1,2,3\n", encoding="utf-8")
    proc = run_cli("profile", str(path))
    assert proc.returncode == 2
    assert "损坏" in proc.stderr


def test_cli_corrupt_jsonl_exit_2(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"a": 1}\n!!!\n', encoding="utf-8")
    proc = run_cli("profile", str(path))
    assert proc.returncode == 2


def test_cli_corrupt_parquet_exit_2(tmp_path):
    path = tmp_path / "bad.parquet"
    path.write_text("nope", encoding="utf-8")
    proc = run_cli("profile", str(path))
    assert proc.returncode == 2


def test_cli_missing_file_exit_2():
    proc = run_cli("profile", "/nonexistent/path/file.csv")
    assert proc.returncode == 2


def test_cli_bad_rules_exit_2(write_csv, tmp_path):
    data = write_csv("d.csv", [{"a": "1"}])
    rules = tmp_path / "rules.json"
    rules.write_text('{"rules": [{"id": "X", "type": "range", "column": "a"}]}',
                     encoding="utf-8")
    proc = run_cli("validate", data, str(rules))
    assert proc.returncode == 2


def test_cli_baseline_end_to_end(write_csv, tmp_path):
    baseline_data = write_csv("base.csv", [{"id": str(i), "v": str(i)} for i in range(1, 101)])
    baseline_report = tmp_path / "baseline.json"
    proc = run_cli("profile", baseline_data, "-o", str(baseline_report))
    assert proc.returncode == 0

    # 无漂移
    same = write_csv("same.csv", [{"id": str(i), "v": str(i)} for i in range(1, 101)])
    ok = run_cli("baseline", str(baseline_report), same)
    assert ok.returncode == 0
    assert load_report(ok.stdout)["status"] == "passed"

    # v 最大值明显漂移
    drifted = write_csv("drift.csv", [{"id": str(i), "v": str(200 if i == 100 else i)}
                                      for i in range(1, 101)])
    failed = run_cli("baseline", str(baseline_report), drifted)
    assert failed.returncode == 1
    report = load_report(failed.stdout)
    assert report["status"] == "drift_detected"
    kinds = {f["kind"] for f in report["diff"]["findings"]}
    assert "range_anomaly" in kinds


def test_cli_baseline_profile_to_profile(tmp_path):
    p1 = tmp_path / "p1.json"
    p2 = tmp_path / "p2.json"
    p1.write_text(json.dumps({"row_count": 1, "column_count": 1,
                              "columns": {"a": {"total": 1, "null_count": 0,
                                                "null_rate": 0.0, "unique_count": 1,
                                                "inferred_type": "int",
                                                "type_counts": {"int": 1},
                                                "common_values": [],
                                                "min": 1, "max": 1, "mean": 1}}}),
                 encoding="utf-8")
    p2.write_text(json.dumps({"row_count": 1, "column_count": 1,
                              "columns": {"b": {"total": 1, "null_count": 0,
                                                "null_rate": 0.0, "unique_count": 1,
                                                "inferred_type": "int",
                                                "type_counts": {"int": 1},
                                                "common_values": [],
                                                "min": 1, "max": 1, "mean": 1}}}),
                 encoding="utf-8")
    proc = run_cli("baseline", str(p1), "--current-profile", str(p2))
    assert proc.returncode == 1
    kinds = {f["kind"] for f in load_report(proc.stdout)["diff"]["findings"]}
    assert kinds == {"column_missing", "column_added"}


def test_cli_jsonl_and_parquet(write_jsonl, write_parquet):
    pytest.importorskip("pyarrow")
    rows = [{"id": i, "ok": True} for i in range(3)]
    jsonl = write_jsonl("d.jsonl", rows)
    parquet = write_parquet("d.parquet", rows)
    assert run_cli("profile", jsonl).returncode == 0
    assert run_cli("profile", parquet).returncode == 0


def test_cli_chunk_size_does_not_change_result(write_csv, tmp_path):
    rows = [{"id": str(i % 3), "v": str(i)} for i in range(50)]
    data = write_csv("d.csv", rows)
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"id": "U", "type": "unique", "column": "id"}]),
                     encoding="utf-8")
    small = run_cli("validate", data, str(rules), "--chunk-size", "7")
    large = run_cli("validate", data, str(rules), "--chunk-size", "1000")
    assert small.returncode == large.returncode == 1
    assert load_report(small.stdout)["result"]["failure_count"] == \
        load_report(large.stdout)["result"]["failure_count"]
