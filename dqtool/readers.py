"""分块数据读取器：CSV / JSONL / Parquet，全部流式，不一次性载入内存。

约定：
- 每个 reader 是上下文管理器，产出 ``list[dict]`` 数据块；
- dict 的键为列名，值为该格式解析出的“原始值”（CSV 为 str，
  JSONL 为 JSON 标量，Parquet 为 pyarrow 转换出的 Python 对象）；
  类型推断统一由 ``dqtool.values.normalize_row`` 完成；
- 行号 row_number 从 1 开始、按数据行计数（CSV 表头不计），
  与损坏数据报错和规则失败记录中的行位置一致。
"""

from __future__ import annotations

import csv
import io
import json
import os
from typing import Any, Iterator

from .errors import CorruptDataError

CSV_SUFFIXES = {".csv"}
JSONL_SUFFIXES = {".jsonl", ".ndjson"}
PARQUET_SUFFIXES = {".parquet", ".pq"}

_DEFAULT_CHUNKSIZE = 10_000


def infer_format(path: str) -> str:
    """根据扩展名推断文件格式，返回 csv / jsonl / parquet。"""
    suffix = os.path.splitext(path)[1].lower()
    if suffix in CSV_SUFFIXES:
        return "csv"
    if suffix in JSONL_SUFFIXES:
        return "jsonl"
    if suffix in PARQUET_SUFFIXES:
        return "parquet"
    raise ValueError(f"无法根据扩展名推断格式: {path}（支持 .csv/.jsonl/.ndjson/.parquet）")


class ChunkReader:
    """分块读取器基类。

    子类需实现 ``_iter_chunks()``。可直接迭代实例，也可用
    ``read_all()``（仅小数据/内存输入场景使用）。
    """

    format_name = "unknown"

    def __init__(self, source: Any, source_name: str, chunksize: int = _DEFAULT_CHUNKSIZE, **kwargs: Any):
        if chunksize is not None and chunksize <= 0:
            raise ValueError("chunksize 必须为正整数")
        self.source = source
        self.source_name = source_name
        self.chunksize = chunksize or _DEFAULT_CHUNKSIZE
        self.kwargs = kwargs
        self._columns: list[str] | None = None

    @property
    def columns(self) -> list[str]:
        if self._columns is None:
            raise RuntimeError("列名在开始迭代后才可用")
        return list(self._columns)

    def _iter_chunks(self) -> Iterator[list[dict]]:
        raise NotImplementedError

    def __iter__(self) -> Iterator[list[dict]]:
        return self._iter_chunks()

    def __enter__(self) -> "ChunkReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:  # pragma: no cover - 默认无资源
        pass


class CsvReader(ChunkReader):
    format_name = "csv"

    def __init__(self, source, source_name, chunksize=_DEFAULT_CHUNKSIZE, strict: bool = True, **kwargs):
        super().__init__(source, source_name, chunksize, **kwargs)
        self.strict = strict
        self._owns_file = False
        self._fh = None

    def _open(self):
        if isinstance(self.source, (str, bytes, os.PathLike)):
            self._fh = open(self.source, "r", encoding=self.kwargs.get("encoding", "utf-8-sig"), newline="")
            self._owns_file = True
        elif isinstance(self.source, (io.TextIOBase, io.RawIOBase, io.BufferedIOBase)):
            self._fh = self.source
        else:
            self._fh = io.StringIO(str(self.source))
            self._owns_file = True
        return self._fh

    def _iter_chunks(self) -> Iterator[list[dict]]:
        fh = self._open()
        try:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                raise CorruptDataError("CSV 文件为空，缺少表头", self.source_name)
            self._columns = [name.strip() for name in header]
            if not self._columns or all(name == "" for name in self._columns):
                raise CorruptDataError("CSV 表头为空", self.source_name)
            ncols = len(self._columns)

            chunk: list[dict] = []
            row_number = 0
            for raw in reader:
                # csv.reader 会跳过完全空白的物理行；这里无需计数
                row_number += 1
                if len(raw) != ncols:
                    if self.strict:
                        raise CorruptDataError(
                            f"列数不匹配：表头 {ncols} 列，当前行 {len(raw)} 列",
                            self.source_name,
                            row_number,
                        )
                    if len(raw) > ncols:
                        raw = raw[:ncols]
                    else:
                        raw = raw + [""] * (ncols - len(raw))
                chunk.append({col: raw[i] for i, col in enumerate(self._columns)})
                if len(chunk) >= self.chunksize:
                    yield chunk
                    chunk = []
            if chunk:
                yield chunk
        finally:
            if self._owns_file and self._fh is not None:
                self._fh.close()

    def close(self) -> None:
        if self._owns_file and self._fh is not None:
            self._fh.close()


class JsonlReader(ChunkReader):
    format_name = "jsonl"

    def _open(self):
        if isinstance(self.source, (str, bytes, os.PathLike)):
            return open(self.source, "r", encoding="utf-8")
        if hasattr(self.source, "read"):
            return self.source
        return io.StringIO(str(self.source))

    def _iter_chunks(self) -> Iterator[list[dict]]:
        fh = self._open()
        owns = isinstance(self.source, (str, bytes, os.PathLike))
        try:
            chunk: list[dict] = []
            row_number = 0
            for line in fh:
                if not line.strip():
                    continue  # 跳过空行，不占数据行号
                row_number += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CorruptDataError(f"JSON 解析失败: {exc.msg}", self.source_name, row_number) from exc
                if not isinstance(record, dict):
                    raise CorruptDataError(
                        f"每行必须是 JSON 对象，实际为 {type(record).__name__}",
                        self.source_name,
                        row_number,
                    )
                if self._columns is None:
                    self._columns = list(record.keys())
                else:
                    # 新出现的列也加入列集合，保持与 profile 的列发现一致
                    for key in record:
                        if key not in self._columns:
                            self._columns.append(key)
                chunk.append(record)
                if len(chunk) >= self.chunksize:
                    yield chunk
                    chunk = []
            if row_number == 0:
                raise CorruptDataError("JSONL 文件为空，没有任何数据行", self.source_name)
            if chunk:
                yield chunk
        finally:
            if owns:
                fh.close()


class ParquetReader(ChunkReader):
    format_name = "parquet"

    def _iter_chunks(self) -> Iterator[list[dict]]:
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover
            raise CorruptDataError("读取 Parquet 需要安装 pyarrow") from exc

        try:
            parquet_file = pq.ParquetFile(self.source)
        except Exception as exc:
            raise CorruptDataError(f"Parquet 文件无法打开: {exc}", self.source_name) from exc

        try:
            self._columns = [field.name for field in parquet_file.schema_arrow]
            for batch in parquet_file.iter_batches(batch_size=self.chunksize):
                # to_pylist 只转换当前批次，天然分块
                yield batch.to_pylist()
        except Exception as exc:
            raise CorruptDataError(f"Parquet 数据读取失败: {exc}", self.source_name) from exc


class MemoryReader(ChunkReader):
    """内存记录列表（list[dict]）读取器，供库 API 使用，同样按块产出。"""

    format_name = "memory"

    def __init__(self, rows, chunksize=_DEFAULT_CHUNKSIZE, source_name="<memory>", **kwargs):
        super().__init__(rows, source_name, chunksize, **kwargs)

    def _iter_chunks(self) -> Iterator[list[dict]]:
        if not isinstance(self.source, list):
            raise TypeError("MemoryReader 需要 list[dict]")
        columns: list[str] = []
        for row in self.source:
            if not isinstance(row, dict):
                raise TypeError("内存数据的每行必须是 dict")
            for key in row:
                if key not in columns:
                    columns.append(key)
        self._columns = columns
        if not self.source:
            return
        for start in range(0, len(self.source), self.chunksize):
            yield [dict(row) for row in self.source[start : start + self.chunksize]]


def open_dataset(
    source: Any,
    fmt: str | None = None,
    chunksize: int = _DEFAULT_CHUNKSIZE,
    *,
    strict_csv: bool = True,
    source_name: str | None = None,
) -> ChunkReader:
    """根据格式打开本地文件并返回分块读取器。

    fmt 为 None 时按扩展名推断。source 为文件路径时，
    source_name 默认就是路径（错误信息中展示）。
    """
    if fmt is None:
        if not isinstance(source, (str, bytes, os.PathLike)):
            raise ValueError("非路径输入必须显式指定 fmt")
        fmt = infer_format(os.fspath(source))
    fmt = fmt.lower()
    name = source_name or (os.fspath(source) if isinstance(source, (str, bytes, os.PathLike)) else "<stream>")

    if fmt == "csv":
        return CsvReader(source, name, chunksize=chunksize, strict=strict_csv)
    if fmt == "jsonl":
        return JsonlReader(source, name, chunksize=chunksize)
    if fmt == "parquet":
        return ParquetReader(source, name, chunksize=chunksize)
    raise ValueError(f"不支持的格式: {fmt}")


def stream_file(
    path: str,
    fmt: str | None = None,
    chunksize: int = _DEFAULT_CHUNKSIZE,
    *,
    strict_csv: bool = True,
) -> Iterator[tuple[int, list[dict]]]:
    """便捷迭代器：产出 (块内起始行号, 数据块)。

    仅用于简单脚本场景；调用方需自行关闭的场景请直接用
    :func:`open_dataset` 作为上下文管理器。
    """
    with open_dataset(path, fmt=fmt, chunksize=chunksize, strict_csv=strict_csv) as reader:
        start_row = 1
        for chunk in reader:
            yield start_row, chunk
            start_row += len(chunk)
