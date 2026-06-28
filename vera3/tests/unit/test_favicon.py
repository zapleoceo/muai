"""dashboard favicon — SVG endpoint + <link> on every HTML page.

Regression net: ensures FAVICON_LINKS gets injected (no leftover
__FAVICON__ placeholder) and the SVG itself stays well-formed.
"""
from __future__ import annotations

import base64
import os

# dashboard.app reads TOKEN_SECRET / TELEGRAM_BOT_TOKEN / OWNER_TELEGRAM_ID
# at import time. Provide CI-safe defaults BEFORE the import.
os.environ.setdefault(
    "TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode()
)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from fastapi.testclient import TestClient  # noqa: E402

from dashboard.app import FAVICON_LINKS, FAVICON_SVG, app  # noqa: E402

client = TestClient(app)


def test_favicon_svg_endpoint_served():
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in r.text
    assert "</svg>" in r.text


def test_favicon_ico_falls_back_to_svg():
    """Modern browsers accept SVG at .ico path — avoids 404 spam."""
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")


def test_favicon_cached():
    r = client.get("/favicon.svg")
    assert "max-age" in r.headers.get("cache-control", "")


def test_favicon_svg_constant_is_minimal():
    """Sanity: SVG is under 1 KB and contains the V-glyph stroke pair."""
    assert len(FAVICON_SVG) < 1024
    assert FAVICON_SVG.startswith("<svg")
    assert FAVICON_SVG.endswith("</svg>")
    # Two V-strokes meeting at the pulse node
    assert FAVICON_SVG.count("<line") == 2
    assert FAVICON_SVG.count("<circle") == 3


def test_favicon_links_has_three_rels():
    """icon + alternate icon + apple-touch-icon."""
    assert FAVICON_LINKS.count("<link") == 3
    assert 'rel="icon"' in FAVICON_LINKS
    assert 'rel="alternate icon"' in FAVICON_LINKS
    assert 'rel="apple-touch-icon"' in FAVICON_LINKS


def test_login_page_has_favicon_link():
    r = client.get("/login")
    assert r.status_code == 200
    assert 'href="/favicon.svg"' in r.text
    assert "__FAVICON__" not in r.text   # placeholder replaced


def test_login_page_has_no_literal_double_braces():
    """f-string template regression — would break CSS in browser."""
    r = client.get("/login")
    assert "{{" not in r.text
    assert "}}" not in r.text
