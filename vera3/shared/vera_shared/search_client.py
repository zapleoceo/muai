"""Клиент к brain-search `/search` — один на шлюз и MCP-сервер.

brain-search ВСЕГДА зовёт брокерную LLM для `answer`, даже при
`use_agent=false` (пропускается только ReAct-цикл с инструментами), и у этого
вызова свой внутренний таймаут 90с с текстовым фолбэком. Таймаут клиента
обязан быть выше, иначе мы оборвём запрос раньше, чем сработает фолбэк.
Замер на проде: реальный вызов занял 101с под нагрузкой брокера.
"""
from __future__ import annotations

from typing import Any

import httpx

SEARCH_TIMEOUT_S = 100.0


class SearchUnavailable(Exception):
    """brain-search недоступен или ответил ошибкой; `status` — для HTTP-слоя."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def search_brain(base_url: str, secret: str, q: str, limit: int = 15,
                       use_agent: bool = False) -> dict[str, Any]:
    url = f"{base_url}/search"
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S) as c:
            r = await c.post(url, json={"q": q, "limit": limit, "use_agent": use_agent},
                             headers={"X-Internal-Secret": secret})
    except httpx.HTTPError as e:
        raise SearchUnavailable(502, f"brain-search unreachable: {e}") from e

    if r.status_code >= 400:
        raise SearchUnavailable(r.status_code, f"brain-search error: {r.text[:300]}")
    return r.json()
