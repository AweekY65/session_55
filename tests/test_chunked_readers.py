"""分块读取器测试：CSV / JSONL / Parquet 流式处理与行号正确性。"""

import json

import pytest

from dqtool.errors import CorruptDataError
from dqtool.readers import MemoryReader, infer_format, open_dataset, stream_file
from dqtool.values import normalize_row


def gather(path, fmt=None, chunksize=2, **kwargs):
    """读取全部数据，同时记录块大小序列与全局行号。"""
    collected = []
    chunk_sizes = []
    start_row = 1
    with open_dataset(path, fmt=fmt, chunksize=chunksize, **kwargs) as reader:
        columns = None
        for chunk in reader:
            if columns is None:
                columns = reader.columns
            assert all(start_row + i for i in range(len(chunk)))
            collected.extend((start_row + idx, row) for idx, row in enumerate(chunk))
            chunk_sizes.append(len(chunk))
            start_row += len(chunk)
    return collected, chunk_sizes, columns


def test_csv_chunking_and_row_numbers(write_csv):
    path = write_csv("d.csv", [{"id": i, "name": f"n{i}"} for i in range(5)])
    collected, sizes, columns = gather(path, chunksize=2)
    assert sizes == [2, 2, 1]
    assert columns == ["id", "name"]
    assert [row_number for row_number, _ in collected] == [1, 2, 3, 4, 5]
    # CSV 读出来的原始值是字符串
    assert collected[0][1]["id"] == "0"
    assert normalize_row(collected[0][1])["id"] == 0


def test_csv_missing_values_become_empty(write_csv):
    path = write_csv("d.csv", [{"id": 1, "v": "a"}, {"id": 2, "v": None}])
    collected, _, _ = gather(path)
    assert collected[1][1]["v"] == ""
    assert normalize_row(collected[1][1])["v"] is None


def test_csv_strict_mode_rejects_ragged_rows(tmp_path):
    path = tmp_path / "ragged.csv"
    path.write_text("a,b\n1,2,3\n", encoding="utf-8")
    with pytest.raises(CorruptDataError) as exc:
        gather(str(path))
    assert exc.value.row_number == 1
    assert "列数不匹配" in str(exc.value)


def test_csv_lenient_mode_pads_rows(tmp_path):
    path = tmp_path / "lenient.csv"
    path.write_text("a,b,c\n1,2\n", encoding="utf-8")
    collected, _, _ = gather(str(path), strict_csv=False)
    assert collected[0][1] == {"a": "1", "b": "2", "c": ""}


def test_empty_csv_is_corrupt(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(CorruptDataError):
        gather(str(path))


def test_jsonl_chunking_and_typed_values(write_jsonl):
    rows = [{"id": i, "ok": i % 2 == 0, "v": i * 0.5} for i in range(5)]
    path = write_jsonl("d.jsonl", rows)
    collected, sizes, columns = gather(path, chunksize=3)
    assert sizes == [3, 2]
    assert columns == ["id", "ok", "v"]
    assert [rn for rn, _ in collected] == [1, 2, 3, 4, 5]
    assert collected[0][1] == {"id": 0, "ok": True, "v": 0.0}


def test_jsonl_blank_lines_do_not_consume_row_numbers(tmp_path):
    path = tmp_path / "blank.jsonl"
    path.write_text('{"a": 1}\n\n  \n{"a": 2}\n', encoding="utf-8")
    collected, _, _ = gather(str(path))
    assert [rn for rn, _ in collected] == [1, 2]


def test_jsonl_bad_line_reports_position(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"a": 1}\n{"a": 2}\n{oops}\n{"a": 3}\n', encoding="utf-8")
    # chunksize=2：前两行构成完整第一块；继续读取第三行时损坏
    iterator = iter(stream_file(str(path), chunksize=2))
    start, first_chunk = next(iterator)
    assert (start, len(first_chunk)) == (1, 2)
    with pytest.raises(CorruptDataError) as exc:
        next(iterator)
    assert exc.value.row_number == 3


def test_jsonl_non_object_line_is_corrupt(tmp_path):
    path = tmp_path / "arr.jsonl"
    path.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(CorruptDataError):
        gather(str(path))


def test_empty_jsonl_is_corrupt(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("\n  \n", encoding="utf-8")
    with pytest.raises(CorruptDataError):
        gather(str(path))


def test_parquet_chunking(write_parquet):
    pytest.importorskip("pyarrow")
    rows = [{"id": i, "name": f"n{i}"} for i in range(7)]
    path = write_parquet("d.parquet", rows)
    collected, sizes, columns = gather(path, chunksize=3)
    assert sum(sizes) == 7
    assert columns == ["id", "name"]
    assert [rn for rn, _ in collected] == list(range(1, 8))
    assert collected[2][1]["id"] == 2


def test_corrupt_parquet_is_corrupt_error(tmp_path):
    path = tmp_path / "bad.parquet"
    path.write_text("definitely not parquet", encoding="utf-8")
    with pytest.raises(CorruptDataError):
        gather(str(path))


def test_infer_format():
    assert infer_format("a.csv") == "csv"
    assert infer_format("a.JSONL") == "jsonl"
    assert infer_format("a.ndjson") == "jsonl"
    assert infer_format("a.parquet") == "parquet"
    with pytest.raises(ValueError):
        infer_format("a.txt")


def test_stream_file_helper(write_csv):
    path = write_csv("d.csv", [{"id": i} for i in range(4)])
    starts = [start for start, chunk in stream_file(path, chunksize=2)]
    assert starts == [1, 3]


def test_memory_reader_chunks():
    rows = [{"x": i} for i in range(5)]
    with MemoryReader(rows, chunksize=2) as reader:
        chunks = list(reader)
    assert [len(c) for c in chunks] == [2, 2, 1]
    assert chunks[0][0] == {"x": 0}
    assert reader.columns == ["x"]


def test_chunked_equals_whole_dataset(sample_rows, write_csv, sample_rules):
    """分块处理与一次性处理的最终结果必须一致（核心分块正确性保证）。"""
    whole = [normalize_row(row) for row in sample_rows]

    path = write_csv("d.csv", sample_rows)
    from dqtool.rules import RuleEngine

    engine_chunked = RuleEngine(sample_rules)
    start = 1
    with open_dataset(path, chunksize=2) as reader:
        for chunk in reader:
            engine_chunked.process_chunk([normalize_row(r) for r in chunk], start)
            start += len(chunk)
    chunked = engine_chunked.result.to_dict()

    engine_whole = RuleEngine(sample_rules)
    engine_whole.process_stream(iter([whole]), known_columns=list(whole[0].keys()))
    assert chunked["failure_count"] == engine_whole.result.failure_count
    assert [f["row_number"] for f in chunked["failures"]] == [
        f.row_number for f in engine_whole.result.failures
    ]


def test_large_csv_does_not_need_full_load(tmp_path):
    """3 万行、chunksize=100 时应产出 300 个块；读取过程中内存只持有当前块。"""
    path = tmp_path / "large.csv"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("id,label\n")
        for i in range(30_000):
            fh.write(f"{i},label-{i % 50}\n")

    total = 0
    block_count = 0
    max_block = 0
    with open_dataset(str(path), chunksize=100) as reader:
        for chunk in reader:
            total += len(chunk)
            block_count += 1
            max_block = max(max_block, len(chunk))
    assert total == 30_000
    assert block_count == 300
    assert max_block == 100


def test_jsonl_round_trip(tmp_path, write_jsonl):
    rows = [{"a": "中文", "b": None}, {"a": "x", "b": 1}]
    path = write_jsonl("u.jsonl", rows)
    collected, _, _ = gather(path)
    assert collected[0][1]["a"] == "中文"
    json.loads(json.dumps(collected[0][1], ensure_ascii=False))
