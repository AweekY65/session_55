"""JSON report serialisation helpers."""

from __future__ import annotations

import json
from typing import Any, Dict

from .errors import DQError


def write_json_report(report: Dict[str, Any], path: str | None) -> None:
    """Write *report* as pretty UTF-8 JSON to *path*, or stdout if None."""
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
            f.write("\n")
    else:
        print(text)


def error_report(message: str, kind: str) -> Dict[str, Any]:
    """Uniform payload emitted when the tool itself errors out."""
    return {"status": "error", "error_type": kind, "message": message}


def error_kind(exc: Exception) -> str:
    return type(exc).__name__ if isinstance(exc, DQError) else type(exc).__name__
