"""Статика дашборда: CSS и скрипт одним запросом, с хэшем в адресе.

Публично: в них нет данных, а страница входа тоже стилизована. Адрес с `?v=`
меняется вместе с содержимым, поэтому кэш вечный (`immutable`).
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from dashboard.ui.js_core import UI_JS
from dashboard.ui.theme import VERA_CSS

router = APIRouter()
_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}


@router.get("/ui/vera.css")
async def vera_css() -> Response:
    return Response(VERA_CSS, media_type="text/css; charset=utf-8", headers=_CACHE)


@router.get("/ui/vera.js")
async def vera_js() -> Response:
    return Response(UI_JS, media_type="text/javascript; charset=utf-8", headers=_CACHE)
