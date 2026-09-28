"""Streaming dataset readers for CSV, JSONL and Parquet.

Every reader is a generator that yields ``(columns, chunk)`` tuples,
where *chunk* is a list of ``dict[str, Any]`` rows.  Files are never
loaded whole: pandas/``pyarrow`` batch readers drive the chunking, so
memory usage is bounded by ``chunk_size`` rows.

Row positions used throughout the tool are **0-based record indexes**
into the data (CSV header excluded), assigned in file order and stable
across chunks.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Generator, Iterable, List, Sequence, Tuple

from .errors import DQInputError

Chunk = Tuple[List[str], List[Dict[str, Any]]]


def infer_format(path: str, fmt: str | None = None) -> str:
    """Return ``csv`` / ``jsonl`` / ``parquet`` from *fmt* or extension.

    ``.json`` and ``.ndjson`` are accepted as aliases for JSONL.
    """
    if fmt:
        fmt = fmt.lower()
        if fmt not in ("csv", "jsonl", "parquet"):
            raise DQInputError(f"unsupported format: {fmt!r}")
        return fmt
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return "csv"
    if ext in (".jsonl", ".ndjson", ".json"):
        return "jsonl"
    if ext in (".parquet", ".pq"):
        return "parquet"
    raise DQInputError(f"cannot infer format from extension {ext!r}; pass --format")


def read_chunks(
    path: str,
    fmt: str | None = None,
    chunk_size: int = 50_000,
    **kwargs: Any,
) -> Generator[Chunk, None, None]:
    """Yield ``(columns, rows)`` chunks from *path* in the inferred format."""
    if chunk_size <= 0:
        raise DQInputError("chunk_size must be a positive integer")
    fmt = infer_format(path, fmt)
    if fmt == "csv":
        yield from _read_csv(path, chunk_size, **kwargs)
    elif fmt == "jsonl":
        yield from _read_jsonl(path, chunk_size, **kwargs)
    else:
        yield from _read_parquet(path, chunk_size, **kwargs)


def rows_from_records(
    records: Iterable[Dict[str, Any]],
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Normalise an in-memory iterable of dicts into ``(columns, rows)``."""
    rows = [dict(r) for r in records]
    columns: List[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns, rows


def _read_csv(path: str, chunk_size: int, **_: Any) -> Generator[Chunk, None, None]:
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - pandas is a dependency
        raise DQInputError("pandas is required to read CSV files") from exc

    try:
        reader = pd.read_csv(
            path,
            dtype=str,            # never let pandas guess types on ingest
            keep_default_na=False,
            na_filter=False,      # empty cell -> "", so null rules decide
            chunksize=chunk_size,
            iterator=True,
            on_bad_lines="error",
        )
        yielded = False
        for frame in reader:
            columns = [str(c) for c in frame.columns]
            rows: List[Dict[str, Any]] = []
            for record in frame.to_dict(orient="records"):
                rows.append({c: record[c] for c in frame.columns})
            yield columns, rows
            yielded = True
        if not yielded:
            raise DQInputError(f"CSV file is empty: {path}")
    except DQInputError:
        raise
    except FileNotFoundError as exc:
        raise DQInputError(f"data file not found: {path}") from exc
    except pd.errors.EmptyDataError as exc:
        raise DQInputError(f"CSV file is empty: {path}") from exc
    except Exception as exc:
        raise DQInputError(f"failed to read CSV {path}: {exc}") from exc


def _read_jsonl(path: str, chunk_size: int, **_: Any) -> Generator[Chunk, None, None]:
    try:
        f = open(path, "r", encoding="utf-8")
    except FileNotFoundError as exc:
        raise DQInputError(f"data file not found: {path}") from exc
    except OSError as exc:
        raise DQInputError(f"cannot open {path}: {exc}") from exc

    with f:
        columns: List[str] = []
        rows: List[Dict[str, Any]] = []
        seen_any_line = False
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue  # blank lines are tolerated
            seen_any_line = True
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DQInputError(
                    f"invalid JSON in {path} on line {lineno}: {exc.msg}"
                ) from exc
            if not isinstance(record, dict):
                raise DQInputError(
                    f"{path} line {lineno}: each JSONL record must be an object, "
                    f"got {type(record).__name__}"
                )
            for key in record:
                if key not in columns:
                    columns.append(str(key))
            rows.append({str(k): v for k, v in record.items()})
            if len(rows) >= chunk_size:
                yield list(columns), rows
                rows = []
        if not seen_any_line:
            raise DQInputError(f"JSONL file is empty: {path}")
        if rows:
            yield list(columns), rows


def _read_parquet(
    path: str, chunk_size: int, **_: Any
) -> Generator[Chunk, None, None]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise DQInputError("pyarrow is required to read Parquet files") from exc

    try:
        parquet_file = pq.ParquetFile(path)
    except FileNotFoundError as exc:
        raise DQInputError(f"data file not found: {path}") from exc
    except Exception as exc:
        raise DQInputError(f"cannot open Parquet file {path}: {exc}") from exc

    try:
        columns: Sequence[str] = [str(c) for c in parquet_file.schema_arrow.names]
        yielded = False
        for batch in parquet_file.iter_batches(batch_size=chunk_size):
            # pylist() gives python-native types (int/float/bool/str/None,
            # or nested list/dict for complex columns).
            cols = {
                col: chunk.to_pylist()
                for col, chunk in zip(columns, batch.columns)
            }
            n = batch.num_rows
            rows = [{c: cols[c][i] for c in columns} for i in range(n)]
            yield list(columns), rows
            yielded = True
        if not yielded:
            raise DQInputError(f"Parquet file contains no rows: {path}")
    except DQInputError:
        raise
    except Exception as exc:
        raise DQInputError(f"failed to read Parquet {path}: {exc}") from exc
