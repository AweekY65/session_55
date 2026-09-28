"""Cross-format and chunk-boundary tests.

Rule results must be identical regardless of chunk size and input
format; stateful rules (unique, composite-unique) must track state across
chunk boundaries correctly.
"""

import pytest

from dqtool.profiler import profile_dataset
from dqtool.readers import read_chunks
from dqtool.rules import parse_rules
from dqtool.validation import validate_chunks


RULES = parse_rules(
    [
        {"id": "id_nn", "type": "not-null", "field": "id"},
        {"id": "id_uq", "type": "unique", "field": "id",
         "params": {"nulls": "distinct"}},
        {"id": "age_r", "type": "range", "field": "age",
         "params": {"min": 0, "max": 120}},
        {"id": "ck", "type": "composite-unique",
         "fields": ["id", "region"]},
    ]
)

ROWS = [
    {"id": str(i % 7), "age": str(20 + i % 100),
     "region": ["us", "eu", "ap"][i % 3]}
    for i in range(100)
]
# inject: a null id row, an out-of-range age row
ROWS[50] = {"id": "", "age": "200", "region": "us"}


def _run(rows, chunk_size):
    from dqtool.readers import rows_from_records

    columns, flat = rows_from_records(rows)
    # split into simulated chunks
    chunks = [
        (columns, flat[i : i + chunk_size])
        for i in range(0, len(flat), chunk_size)
    ]
    return validate_chunks(RULES, chunks)


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 32, 100, 1000])
def test_results_invariant_to_chunk_size(chunk_size):
    report = _run(ROWS, chunk_size)
    counts = {rid: r.failure_count for rid, r in report.results.items()}
    # 100 ids cycling mod 7 with one row replaced by a null: 99 non-null
    # rows over 7 distinct ids -> 92 duplicate rows.
    assert counts["id_uq"] == 92
    assert counts["id_nn"] == 1
    assert counts["age_r"] == 1
    # composite key (id%7, region%3) has 21 distinct tuples over 99 non-null
    assert counts["ck"] == 99 - 21
    assert report.row_count == 100
    rows_by_rule = {
        rid: sorted(f.row for f in r.failures)
        for rid, r in report.results.items()
        if r.failures
    }
    reference = _run(ROWS, 500)
    ref_rows = {
        rid: sorted(f.row for f in r.failures)
        for rid, r in reference.results.items()
        if r.failures
    }
    assert rows_by_rule == ref_rows


def test_csv_vs_jsonl_vs_parquet_equivalent(tmp_path, write_csv,
                                           write_jsonl, write_parquet):
    rows = [
        {"id": 1, "age": 30, "region": "us"},
        {"id": 2, "age": 45, "region": "eu"},
        {"id": 2, "age": 45, "region": "eu"},  # dup id + dup composite
        {"id": 3, "age": 130, "region": "ap"},  # range failure
    ]
    csv_path = write_csv(
        "d.csv", ["id", "age", "region"],
        [(r["id"], r["age"], r["region"]) for r in rows],
    )
    jsonl_path = write_jsonl("d.jsonl", rows)
    parquet_path = write_parquet(
        "d.parquet",
        {
            "id": [r["id"] for r in rows],
            "age": [r["age"] for r in rows],
            "region": [r["region"] for r in rows],
        },
    )

    def counts(path, fmt):
        report = validate_chunks(
            RULES, read_chunks(path, fmt=fmt, chunk_size=2)
        )
        return {rid: r.failure_count for rid, r in report.results.items()}

    c_csv = counts(csv_path, "csv")
    c_jsonl = counts(jsonl_path, "jsonl")
    c_pq = counts(parquet_path, "parquet")
    assert c_csv == c_jsonl == c_pq
    assert c_csv["id_uq"] == 1
    assert c_csv["age_r"] == 1
    assert c_csv["ck"] == 1


def test_profile_invariant_to_chunk_size_csv(write_csv):
    rows = [(i, i % 5, f"v{i % 3}") for i in range(50)]
    path = write_csv("d.csv", ["a", "b", "c"], rows)
    p1 = profile_dataset(read_chunks(path, chunk_size=50))
    p2 = profile_dataset(read_chunks(path, chunk_size=7))
    assert p1 == p2


def test_chunk_size_of_one_processes_every_row(write_csv):
    rows = [(i,) for i in range(10)]
    path = write_csv("d.csv", ["a"], rows)
    chunks = list(read_chunks(path, chunk_size=1))
    assert len(chunks) == 10
    assert sum(len(c[1]) for c in chunks) == 10
