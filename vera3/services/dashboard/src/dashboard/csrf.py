"""Защита POST-правок от чужих сайтов.

Старые формы дашборда держатся только на `SameSite=Lax` у сессионной cookie.
Правки графа (разрыв связи, откат) добавляют второй рубеж: запрос обязан прийти
со страницы дашборда. Браузер сам ставит `Sec-Fetch-Site` и `Origin`, и скрипт
чужой страницы подделать их не может.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse


def _origin_matches_host(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    return urlsplit(origin).netloc == request.headers.get("host")


def same_origin_or_403(request: Request) -> JSONResponse | None:
    if request.headers.get("sec-fetch-site") == "same-origin" or _origin_matches_host(request):
        return None
    return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
