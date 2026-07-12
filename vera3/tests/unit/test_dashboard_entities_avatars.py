"""Entity links + avatars + real-duplicate (username-collision) detection.

Pure helpers (tg_link, initials_avatar_svg) are tested directly; the DB-backed
find_alias_collisions grouping and the avatar serve route are tested with the
session / storage calls mocked at their boundary (same style as
test_gateway_query.py)."""
from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402

from dashboard.render import initials_avatar_svg, tg_link  # noqa: E402

# ─── tg_link ────────────────────────────────────────────────────────────────


def test_tg_link_username_is_web_url():
    assert tg_link("dimaod", 123) == "https://t.me/dimaod"
    assert tg_link("@dimaod", 123) == "https://t.me/dimaod"   # strips leading @


def test_tg_link_falls_back_to_id_scheme():
    assert tg_link(None, 6958625248) == "tg://user?id=6958625248"


def test_tg_link_none_when_nothing():
    assert tg_link(None, None) is None
    assert tg_link("", None) is None


# ─── initials_avatar_svg ────────────────────────────────────────────────────


def test_initials_two_words():
    svg = initials_avatar_svg("Дима Груздев", seed=1)
    assert ">ДГ<" in svg
    assert svg.startswith("<svg") and "circle" in svg


def test_initials_single_and_empty():
    assert ">Д<" in initials_avatar_svg("Дима")
    assert ">?<" in initials_avatar_svg(None)
    assert ">?<" in initials_avatar_svg("   ")


def test_initials_color_deterministic_by_seed():
    a = initials_avatar_svg("Anna", seed=3)
    b = initials_avatar_svg("Boris", seed=3)      # same seed → same colour
    color_a = a.split('fill="')[1].split('"')[0]
    color_b = b.split('fill="')[1].split('"')[0]
    assert color_a == color_b


def test_initials_escapes_name():
    svg = initials_avatar_svg('<x>', seed=0)
    assert "<x>" not in svg.split("<text")[1]   # initial char escaped in <text>


# ─── find_alias_collisions (grouping logic) ─────────────────────────────────


class _FakeSessionCtx:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        result = MagicMock()
        result.mappings.return_value.all.return_value = self._rows
        session = MagicMock()
        session.execute = AsyncMock(return_value=result)
        return session

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_find_alias_collisions_groups_shared_username():
    from vera_shared.graph import dedup
    rows = [
        {"id": 115, "name": "aibotlist", "type": "channel",
         "attributes": {"username": "aibotlist"}},
        {"id": 116, "name": "aibotlist", "type": "person",
         "attributes": {"username": "AIbotList"}},   # case-insensitive match
        {"id": 200, "name": "Соло", "type": "person",
         "attributes": {"username": "solo_uniq"}},    # singleton → excluded
        {"id": 201, "name": "NoName", "type": "person", "attributes": {}},  # no username
    ]
    with patch.object(dedup, "get_session", lambda: _FakeSessionCtx(rows)):
        groups = await dedup.find_alias_collisions(min_group=2)

    assert len(groups) == 1
    g = groups[0]
    assert g["username"] == "aibotlist"
    assert g["size"] == 2
    assert {c["id"] for c in g["candidates"]} == {115, 116}


# ─── avatar serve route ─────────────────────────────────────────────────────


def _owner_cookie() -> str:
    from starlette.responses import Response

    from dashboard.auth import COOKIE_NAME
    from dashboard.auth_routes import _set_session_cookie
    resp = Response()
    _set_session_cookie(resp)
    ch = resp.headers.get("set-cookie", "")
    return COOKIE_NAME, (ch.split(";")[0].split("=", 1)[1] if "=" in ch else "")


def test_avatar_route_serves_stored_image():
    from fastapi.testclient import TestClient

    from dashboard.app import app
    name, val = _owner_cookie()
    with patch("dashboard.entities_routes.get_avatar",
               AsyncMock(return_value=(b"\xff\xd8jpegbytes", "image/jpeg"))):
        r = TestClient(app).get("/entities/42/avatar", cookies={name: val})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == b"\xff\xd8jpegbytes"


def test_avatar_route_falls_back_to_initials_svg():
    from fastapi.testclient import TestClient

    from dashboard.app import app
    name, val = _owner_cookie()
    with patch("dashboard.entities_routes.get_avatar", AsyncMock(return_value=None)), \
         patch("dashboard.entities_routes.get_entity_context",
               AsyncMock(return_value={"name": "Дима Груздев"})):
        r = TestClient(app).get("/entities/7/avatar", cookies={name: val})
    assert r.status_code == 200
    assert "image/svg" in r.headers["content-type"]
    assert ">ДГ<" in r.text


def test_avatar_route_requires_auth():
    from fastapi.testclient import TestClient

    from dashboard.app import app
    r = TestClient(app).get("/entities/1/avatar")
    assert r.status_code in (401, 403)
