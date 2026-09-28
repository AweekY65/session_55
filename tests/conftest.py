"""Shared pytest fixtures/helpers and import path setup.

Running ``python -m pytest`` from the repository root works without
installing the package: the repo root is inserted onto sys.path here.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture()
def write_csv(tmp_path):
    def _write(name, header, rows, newline="\n"):
        path = tmp_path / name
        with open(path, "w", encoding="utf-8", newline=newline) as f:
            f.write(",".join(header) + "\n")
            for row in rows:
                f.write(",".join(str(v) for v in row) + "\n")
        return str(path)

    return _write


@pytest.fixture()
def write_jsonl(tmp_path):
    def _write(name, records, raw_lines=None):
        path = tmp_path / name
        with open(path, "w", encoding="utf-8") as f:
            if raw_lines is not None:
                for line in raw_lines:
                    f.write(line + "\n")
            else:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return str(path)

    return _write


@pytest.fixture()
def write_rules(tmp_path):
    def _write(rules, name="rules.json"):
        path = tmp_path / name
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"rules": rules}, f)
        return str(path)

    return _write


@pytest.fixture()
def write_parquet(tmp_path):
    def _write(name, columns_dict):
        import pyarrow as pa
        import pyarrow.parquet as pq

        path = tmp_path / name
        table = pa.table(columns_dict)
        pq.write_table(table, str(path))
        return str(path)

    return _write
