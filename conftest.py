"""pytest 公共夹具：无需安装即可从仓库根目录导入 dqtool，并提供临时数据集。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def sample_rows():
    """含空值、重复值、类型混杂的典型数据。"""
    return [
        {"id": 1, "name": "Alice", "age": 30, "email": "alice@example.com", "status": "active"},
        {"id": 2, "name": "Bob", "age": None, "email": "bob@example.com", "status": "inactive"},
        {"id": 3, "name": "Carol", "age": 201, "email": "not-an-email", "status": "active"},
        {"id": 4, "name": "Alice", "age": 25, "email": "alice2@example.com", "status": "unknown"},
        {"id": 5, "name": "Eve", "age": "abc", "email": "eve@example.com", "status": "active"},
    ]


@pytest.fixture
def sample_rules():
    return [
        {"id": "R001", "type": "not-null", "column": "age"},
        {"id": "R002", "type": "unique", "column": "id"},
        {"id": "R003", "type": "range", "column": "age", "min": 0, "max": 120},
        {"id": "R004", "type": "regex", "column": "email", "pattern": r"^[^@]+@[^@]+\.[^@]+$"},
        {"id": "R005", "type": "allowed-values", "column": "status",
         "values": ["active", "inactive"]},
        {"id": "R006", "type": "unique-composite", "columns": ["name", "status"]},
    ]


@pytest.fixture
def write_csv(tmp_path):
    import csv as _csv

    def _write(name, rows, columns=None):
        if columns is None:
            columns = list(rows[0].keys()) if rows else []
        path = tmp_path / name
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = _csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: ("" if row.get(key) is None else row.get(key, ""))
                                 for key in columns})
        return str(path)

    return _write


@pytest.fixture
def write_jsonl(tmp_path):
    import json

    def _write(name, rows):
        path = tmp_path / name
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return str(path)

    return _write


@pytest.fixture
def write_parquet(tmp_path):
    pyarrow = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    def _write(name, rows):
        path = tmp_path / name
        table = pyarrow.Table.from_pylist(rows)
        pq.write_table(table, str(path))
        return str(path)

    return _write
