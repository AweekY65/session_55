"""库 API 直调测试：纯内存 list[dict] 输入，不接触任何文件/服务。"""

from dqtool import profile_rows, validate_rows, compare_profiles
from dqtool.profiler import Profiler
from dqtool.rules import RuleEngine
from dqtool.readers import MemoryReader
from dqtool.values import normalize_row


def test_end_to_end_in_memory_pipeline():
    rows = [
        {"id": 1, "email": "a@x.com", "age": 30, "region": "cn"},
        {"id": 1, "email": "b@x.com", "age": 250, "region": "eu"},
        {"id": 2, "email": "bad", "age": None, "region": "other"},
    ]
    normalized = [normalize_row(row) for row in rows]

    # 1) profile
    profile = profile_rows(normalized)
    assert profile["row_count"] == 3
    assert abs(profile["columns"]["age"]["null_rate"] - 1 / 3) < 1e-5

    # 2) 规则校验：所有失败都要逐行可定位
    result = validate_rows(normalized, [
        {"id": "id_unique", "type": "unique", "column": "id"},
        {"id": "email_regex", "type": "regex", "column": "email",
         "pattern": r"^[^@]+@[^@]+\.[^@]+$"},
        {"id": "age_range", "type": "range", "column": "age", "min": 0, "max": 120},
        {"id": "age_required", "type": "not-null", "column": "age"},
        {"id": "region_allowed", "type": "allowed-values", "column": "region",
         "values": ["cn", "us", "eu", "jp"]},
    ])
    by_id = {}
    for failure in result.failures:
        by_id.setdefault(failure.rule_id, []).append(failure)
    assert sorted(f.row_number for f in by_id["id_unique"]) == [2]
    assert by_id["email_regex"][0].row_number == 3
    assert by_id["age_range"][0].row_number == 2
    assert by_id["age_required"][0].row_number == 3
    assert result.passed is False

    # 3) 基线比较：当前数据相对自身无漂移
    assert compare_profiles(profile, profile).passed


def test_memory_reader_feeds_engine():
    rows = [{"k": "a"}, {"k": "a"}, {"k": "b"}]
    engine = RuleEngine([{"id": "U", "type": "unique", "column": "k"}])
    start = 1
    with MemoryReader(rows, chunksize=1) as reader:
        for chunk in reader:
            engine.process_chunk(chunk, start)
            start += len(chunk)
    assert engine.result.failure_count == 1
    assert engine.result.failures[0].row_number == 2


def test_streaming_profiler_matches_one_shot():
    rows = [{"x": i} for i in range(100)]
    one_shot = Profiler().update(rows).to_dict()

    streaming = Profiler()
    with MemoryReader(rows, chunksize=7) as reader:
        streaming.update_stream(reader)
    assert streaming.to_dict() == one_shot
