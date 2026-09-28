"""Tests for streaming readers: chunking, formats and corrupt input."""

import json

import pytest

from dqtool.errors import DQInputError
from dqtool.readers import infer_format, read_chunks, rows_from_records


def _flatten(path, fmt=None, chunk_size=2):
    all_columns, all_rows = [], []
    for columns, rows in read_chunks(path, fmt=fmt, chunk_size=chunk_size):
        assert len(rows) <= chunk_size
        all_columns = columns
        all_rows.extend(rows)
    return all_columns, all_rows


def test_csv_chunked_read(write_csv):
    path = write_csv("d.csv", ["a", "b"], [(1, "x"), (2, "y"), (3, "z"),
                                           (4, "w"), (5, "q")])
    chunks = list(read_chunks(path, chunk_size=2))
    assert len(chunks) == 3
    assert [len(c[1]) for c in chunks] == [2, 2, 1]
    columns, rows = _flatten(path, chunk_size=2)
    assert columns == ["a", "b"]
    assert rows[0] == {"a": "1", "b": "x"}
    assert rows[-1] == {"a": "5", "b": "q"}
    # empty cell is an empty string (null decision belongs to rules)
    path2 = write_csv("d2.csv", ["a", "b"], [(1, ""), ("", "y")])
    _, rows2 = _flatten(path2)
    assert rows2[0]["b"] == ""
    assert rows2[1]["a"] == ""


def test_jsonl_chunked_read(write_jsonl):
    path = write_jsonl(
        "d.jsonl",
        [{"a": 1}, {"a": 2}, {"a": 3}, {"a": 4}, {"a": 5}],
    )
    chunks = list(read_chunks(path, chunk_size=2))
    assert len(chunks) == 3
    # union of columns discovered across chunks
    path2 = write_jsonl(
        "d2.jsonl",
        [{"a": 1, "b": 2}, {"a": 3}, {"b": 4, "c": 5}],
    )
    columns, rows = _flatten(path2)
    assert set(columns) == {"a", "b", "c"}
    assert rows[1] == {"a": 3}
    assert rows[2] == {"b": 4, "c": 5}
    # blank lines are tolerated and do not count as rows
    path3 = write_jsonl("d3.jsonl", None, raw_lines=["{\"a\": 1}", "", "  ",
                                                      "{\"a\": 2}"])
    columns, rows = _flatten(path3)
    assert len(rows) == 2
    assert [r["a"] for r in rows] == [1, 2]


def test_parquet_chunked_read(write_parquet):
    path = write_parquet("d.parquet", {"id": [1, 2, 3, 4, 5],
                                       "name": ["a", "b", None, "d", "e"]})
    chunks = list(read_chunks(path, chunk_size=2))
    assert len(chunks) == 3
    columns, rows = _flatten(path, chunk_size=2)
    assert columns == ["id", "name"]
    assert rows[2] == {"id": 3, "name": None}
    assert rows[4]["id"] == 5


def test_missing_file_raises():
    with pytest.raises(DQInputError, match="not found"):
        list(read_chunks("/nonexistent/path.csv"))
    with pytest.raises(DQInputError, match="not found"):
        list(read_chunks("/nonexistent/path.jsonl"))
    with pytest.raises(DQInputError, match="not found"):
        list(read_chunks("/nonexistent/path.parquet"))


def test_corrupt_jsonl_reports_line_number(write_jsonl):
    path = write_jsonl("bad.jsonl", None,
                       raw_lines=["{\"a\": 1}", "{oops", "{\"a\": 3}"])
    with pytest.raises(DQInputError, match="line 2"):
        list(read_chunks(path))


def test_jsonl_non_object_record(write_jsonl):
    path = write_jsonl("bad.jsonl", None, raw_lines=["[1,2,3]"])
    with pytest.raises(DQInputError, match="must be an object"):
        list(read_chunks(path))


def test_corrupt_parquet(tmp_path):
    path = tmp_path / "bad.parquet"
    path.write_bytes(b"this is definitely not a parquet file")
    with pytest.raises(DQInputError, match="[Pp]arquet"):
        list(read_chunks(str(path)))


def test_empty_files_are_input_errors(tmp_path):
    for name in ("e.csv", "e.jsonl", "e.parquet"):
        p = tmp_path / name
        p.write_bytes(b"")
        with pytest.raises(DQInputError, match="empty|no rows|Parquet"):
            list(read_chunks(str(p)))


def test_infer_format():
    assert infer_format("a.csv") == "csv"
    assert infer_format("a.jsonl") == "jsonl"
    assert infer_format("a.ndjson") == "jsonl"
    assert infer_format("a.parquet") == "parquet"
    assert infer_format("a.pq") == "parquet"
    assert infer_format("a.unknown", fmt="jsonl") == "jsonl"
    with pytest.raises(DQInputError):
        infer_format("a.unknown")
    with pytest.raises(DQInputError):
        infer_format("a.csv", fmt="orc")


def test_rows_from_records_union_columns():
    columns, rows = rows_from_records([{"a": 1}, {"a": 2, "b": 3}])
    assert columns == ["a", "b"]
    assert rows == [{"a": 1}, {"a": 2, "b": 3}]


def test_bad_chunk_size(write_csv):
    path = write_csv("d.csv", ["a"], [(1,)])
    with pytest.raises(DQInputError, match="chunk_size"):
        list(read_chunks(path, chunk_size=0))
