"""Маскирование секретов в HTML дашборда; хранилище не меняется.

«Как есть» (`?raw=1`) отдаётся только при действующей сессии владельца: решение
принимается здесь же, а не берётся из того, что маршрут уже проверил.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, Request
from vera_shared.redact_view import redact_fields

from dashboard.auth import COOKIE_NAME, require_owner


def owner_wants_raw(request: Request) -> bool:
    if request.query_params.get("raw") != "1":
        return False
    try:
        require_owner(request, request.cookies.get(COOKIE_NAME))
    except HTTPException:
        return False
    return True


def redact_event_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Копия строки события со скрытыми значениями секретов в текстовых полях."""
    return redact_fields(row, text_keys=("content_text", "triage_error"),
                         tree_keys=("content_extra", "metadata"))
