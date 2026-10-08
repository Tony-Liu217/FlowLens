from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any


def raw_value(value: Any) -> Any:
    """JSON-safe, typed values; never erase an invalid/nonfinite original value."""
    if isinstance(value, (datetime, date, time, Decimal)):
        return {"type": type(value).__name__, "value": str(value)}
    if isinstance(value, float) and not math.isfinite(value):
        return {"type": "float", "value": str(value)}
    if isinstance(value, dict):
        return {str(k): raw_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [raw_value(v) for v in value]
    return value


def dumps(value: Any) -> str:
    return json.dumps(raw_value(value), ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(*parts: Any) -> str:
    return hashlib.sha256(dumps(parts).encode("utf-8")).hexdigest()


@dataclass
class Row:
    values: list
    line: int
    end_line: int | None = None
    cell_types: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    formulas: dict[int, str] = field(default_factory=dict)


@dataclass
class Segment:
    name: str
    rows: list[Row]
    kind: str = "sheet"
    page: int | None = None
    table: int | None = None
    layout: tuple = ()


@dataclass
class ReadResult:
    format: str
    segments: list[Segment] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def issue(code: str, message: str, severity: str = "error", **location) -> dict:
    return {"code": code, "message": message, "severity": severity, **location}


class ReadFailure(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
