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

from dashboard.render import owner_or_blank_401


def _origin_matches_host(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    return urlsplit(origin).netloc == request.headers.get("host")


def same_origin_or_403(request: Request) -> JSONResponse | None:
    if request.headers.get("sec-fetch-site") == "same-origin" or _origin_matches_host(request):
        return None
    return JSONResponse({"error": "cross-origin request refused"}, status_code=403)


def owner_gate(request: Request) -> JSONResponse | None:
    """GET-ручки JSON: только владелец (пустой 401 для чужих)."""
    if owner_or_blank_401(request) is not None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


def owner_post_gate(request: Request) -> JSONResponse | None:
    """POST-ручки правок: владелец и запрос со страницы дашборда."""
    return owner_gate(request) or same_origin_or_403(request)
