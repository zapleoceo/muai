"""Строка ORM ⇄ JSON-безопасный dict. Нужен отчёту о слиянии: он обязан
пережить `json.dump` и вернуть строку байт-в-байт, иначе откат не откат."""
from __future__ import annotations

import base64
from datetime import datetime
from typing import Any

from sqlalchemy.orm import DeclarativeBase

_DT = "$dt"
_BYTES = "$b64"


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return {_DT: value.isoformat()}
    if isinstance(value, bytes):
        return {_BYTES: base64.b64encode(value).decode("ascii")}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1:
        if _DT in value:
            return datetime.fromisoformat(value[_DT])
        if _BYTES in value:
            return base64.b64decode(value[_BYTES])
    return value


def row_dict(row: DeclarativeBase) -> dict[str, Any]:
    return {c.key: _encode(getattr(row, c.key)) for c in row.__table__.columns}


def decode_values(values: dict[str, Any]) -> dict[str, Any]:
    return {k: _decode(v) for k, v in values.items()}
