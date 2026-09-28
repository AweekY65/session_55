"""End-to-end CLI tests: exit codes, JSON reports, all three commands.

These invoke the real ``main()`` entry point with file arguments, so
they also cover terminal/CI usage and corrupt-input handling.
"""

import json
import subprocess
import sys

import pytest

from dqtool.cli import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILED,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    main,
)

ROOT_MARK = ("-m", "dqtool")  # used when spawning fresh interpreters


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


@pytest.fixture()
def good_csv(tmp_path):
    path = tmp_path / "d.csv"
    _write(
        path,
        "id,name,age,email,status\n"
        "1,Alice,30,alice@example.com,active\n"
        "2,Bob,25,bob@example.com,banned\n"
        "3,Cara,45,cara@example.com,active\n",
    )
    return str(path)


@pytest.fixture()
def rules_path(tmp_path):
    path = tmp_path / "rules.json"
    _write(
        path,
        json.dumps(
            {
                "rules": [
                    {"id": "id_nn", "type": "not-null", "field": "id"},
                    {"id": "id_uq", "type": "unique", "field": "id"},
                    {"id": "age_r", "type": "range", "field": "age",
                     "params": {"min": 0, "max": 120}},
                    {"id": "email_re", "type": "regex", "field": "email",
                     "params": {"pattern": r"^[^@]+@[^@]+\.[^@]+$"}},
                    {"id": "st_av", "type": "allowed-values",
                     "field": "status",
                     "params": {"allowed": ["active", "banned"]}},
                    {"id": "ck", "type": "composite-unique",
                     "fields": ["id", "email"]},
                ]
            }
        ),
    )
    return str(path)


# -------------------------------------------------------------- profile cmd

def test_cli_profile_stdout(good_csv, capsys):
    code = main(["profile", good_csv])
    assert code == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["row_count"] == 3
    assert out["columns"]["age"]["mean"] == pytest.approx(33.333333, abs=1e-6)


def test_cli_profile_to_file(good_csv, tmp_path):
    report = tmp_path / "p.json"
    code = main(["profile", good_csv, "-o", str(report)])
    assert code == EXIT_OK
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["columns"]["id"]["unique_count"] == 3


def test_cli_profile_chunk_size(good_csv):
    code = main(["profile", good_csv, "--chunk-size", "1"])
    assert code == EXIT_OK


# ------------------------------------------------------------- validate cmd

def test_cli_validate_passing(good_csv, rules_path, capsys):
    code = main(["validate", good_csv, "-r", rules_path])
    assert code == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "passed"
    assert out["total_failures"] == 0


def test_cli_validate_failing_exit_code_and_detail(
    tmp_path, rules_path, capsys
):
    data = tmp_path / "bad.csv"
    _write(
        data,
        "id,name,age,email,status\n"
        "1,Alice,30,alice@example.com,active\n"
        "1,Bob,200,bad-email,weird\n"      # duplicate id, bad age/email/status
        ",Cara,25,cara@example.com,active\n",  # null id
    )
    code = main(["validate", str(data), "-r", rules_path,
                 "--include-values"])
    assert code == EXIT_FAILED
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "failed"
    assert out["row_count"] == 3
    by_id = {r["rule_id"]: r for r in out["results"]}
    assert by_id["id_uq"]["failure_count"] == 1
    f = by_id["id_uq"]["failures"][0]
    assert f["row"] == 1
    assert f["field"] == "id"
    assert f["rule_id"] == "id_uq"
    assert "duplicate" in f["reason"]
    assert f["value"] == "1"
    assert by_id["id_nn"]["failure_count"] == 1
    assert by_id["age_r"]["failure_count"] == 1
    assert by_id["email_re"]["failure_count"] == 1
    assert by_id["st_av"]["failure_count"] == 1
    assert by_id["age_r"]["failures"][0]["row"] == 1


def test_cli_validate_report_file(tmp_path, good_csv, rules_path):
    out_path = tmp_path / "report.json"
    code = main(["validate", good_csv, "-r", rules_path,
                 "-o", str(out_path)])
    assert code == EXIT_OK
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["status"] == "passed"


def test_cli_validate_max_failures_truncates(tmp_path, capsys):
    data = tmp_path / "d.csv"
    rules = tmp_path / "r.json"
    _write(data, "a\n" + "".join("x\n" for _ in range(10)))
    _write(
        rules,
        json.dumps({"rules": [{"id": "u", "type": "unique",
                               "field": "a"}]}),
    )
    code = main(["validate", str(data), "-r", str(rules),
                 "--max-failures", "2"])
    assert code == EXIT_FAILED
    out = json.loads(capsys.readouterr().out)
    r = out["results"][0]
    assert r["failure_count"] == 9
    assert len(r["failures"]) == 2
    assert r["truncated"] is True


# -------------------------------------------------------------- compare cmd

def test_cli_compare_matched_and_changed(good_csv, tmp_path, capsys):
    p1 = tmp_path / "p1.json"
    p2 = tmp_path / "p2.json"
    assert main(["profile", good_csv, "-o", str(p1)]) == EXIT_OK
    assert main(["profile", good_csv, "-o", str(p2)]) == EXIT_OK

    assert main(["compare", str(p1), str(p2)]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "matched"

    changed = tmp_path / "d2.csv"
    _write(
        changed,
        "id,name,age,email,status,newcol\n"
        "1,Alice,300,alice@example.com,active,z\n"
        ",Bob,,bob@example.com,banned,z\n",
    )
    p3 = tmp_path / "p3.json"
    assert main(["profile", str(changed), "-o", str(p3)]) == EXIT_OK
    code = main(["compare", str(p1), str(p3)])
    assert code == EXIT_FAILED
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "changed"
    assert "newcol" in out["new_columns"]
    assert out["null_rate_changes"]  # age nulls jumped
    assert any(a["column"] == "age" for a in out["range_anomalies"])


def test_cli_compare_custom_thresholds(good_csv, tmp_path, capsys):
    p1 = tmp_path / "p1.json"
    assert main(["profile", good_csv, "-o", str(p1)]) == EXIT_OK
    p2 = tmp_path / "p2.json"
    assert main(["profile", good_csv, "-o", str(p2)]) == EXIT_OK
    code = main(["compare", str(p1), str(p2), "--null-rate-delta",
                 "0.5", "--range-tolerance", "0.1"])
    assert code == EXIT_OK


# ----------------------------------------------------------- error handling

def test_cli_missing_data_file(tmp_path, rules_path, capsys):
    code = main(["validate", str(tmp_path / "nope.csv"), "-r", rules_path])
    assert code == EXIT_INPUT_ERROR
    err = json.loads(capsys.readouterr().err)
    assert err["status"] == "error"
    assert err["error_type"] == "InputError"


def test_cli_corrupt_jsonl(tmp_path, capsys):
    data = tmp_path / "bad.jsonl"
    _write(data, '{"a": 1}\n{ broken\n')
    code = main(["profile", str(data)])
    assert code == EXIT_INPUT_ERROR
    err = json.loads(capsys.readouterr().err)
    assert "line 2" in err["message"]


def test_cli_corrupt_parquet(tmp_path, capsys):
    data = tmp_path / "bad.parquet"
    _write(data, "garbage")
    code = main(["profile", str(data)])
    assert code == EXIT_INPUT_ERROR


def test_cli_empty_csv(tmp_path, capsys):
    data = tmp_path / "empty.csv"
    data.write_text("", encoding="utf-8")
    assert main(["profile", str(data)]) == EXIT_INPUT_ERROR


def test_cli_bad_rule_file(tmp_path, good_csv, capsys):
    rules = tmp_path / "r.json"
    _write(rules, json.dumps({"rules": [{"type": "bogus", "field": "x"}]}))
    code = main(["validate", good_csv, "-r", str(rules)])
    assert code == EXIT_CONFIG_ERROR
    err = json.loads(capsys.readouterr().err)
    assert err["error_type"] == "ConfigError"


def test_cli_invalid_regex_rule(tmp_path, good_csv, capsys):
    rules = tmp_path / "r.json"
    _write(
        rules,
        json.dumps({"rules": [{"id": "r", "type": "regex", "field": "x",
                               "params": {"pattern": "("}}]}),
    )
    assert main(["validate", good_csv, "-r", str(rules)]) == EXIT_CONFIG_ERROR


def test_cli_bad_compare_profiles(tmp_path, good_csv, capsys):
    p1 = tmp_path / "p1.json"
    main(["profile", good_csv, "-o", str(p1)])
    not_profile = tmp_path / "p2.json"
    _write(not_profile, json.dumps({"hello": "world"}))
    code = main(["compare", str(p1), str(not_profile)])
    assert code == EXIT_CONFIG_ERROR


def test_cli_missing_profile_file(tmp_path, good_csv, capsys):
    p1 = tmp_path / "p1.json"
    main(["profile", good_csv, "-o", str(p1)])
    code = main(["compare", str(p1), str(tmp_path / "missing.json")])
    assert code == EXIT_CONFIG_ERROR


# ------------------------------------------------------ fresh interpreter

def test_module_invocation_exit_code(tmp_path):
    """`python -m dqtool` must propagate codes for real CI shells."""
    data = tmp_path / "d.csv"
    _write(data, "a,b\n,1\nx,2\n")  # first row has a null (empty) field
    rules = tmp_path / "r.json"
    _write(rules, json.dumps({"rules": [{"id": "nn", "type": "not-null",
                                         "field": "a"}]}))
    proc = subprocess.run(
        [sys.executable, "-m", "dqtool", "validate", str(data),
         "-r", str(rules)],
        capture_output=True,
        text=True,
        cwd=__import__("os").path.dirname(
            __import__("os").path.dirname(__import__("os").path.abspath(__file__))
        ),
    )
    assert proc.returncode == EXIT_FAILED
    report = json.loads(proc.stdout)
    assert report["results"][0]["failures"][0]["row"] == 0
