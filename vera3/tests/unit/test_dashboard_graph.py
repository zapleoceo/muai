"""dashboard /graph page + /api/graph JSON — auth gate + shape."""
from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock, patch

# dashboard.app reads secrets at import — CI-safe defaults BEFORE import.
os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from fastapi.testclient import TestClient  # noqa: E402

from dashboard.app import app  # noqa: E402

client = TestClient(app)


def _owner_cookie():
    from starlette.responses import Response

    from dashboard.auth import COOKIE_NAME
    from dashboard.auth_routes import _set_session_cookie
    resp = Response()
    _set_session_cookie(resp)
    header = resp.headers.get("set-cookie", "")
    val = header.split(";")[0].split("=", 1)[1] if "=" in header else ""
    return {COOKIE_NAME: val}


def test_graph_page_requires_auth():
    r = client.get("/graph")
    assert r.status_code in (401, 403)


def test_api_graph_requires_auth():
    r = client.get("/api/graph")
    assert r.status_code == 401


def test_graph_page_renders_for_owner():
    r = client.get("/graph", cookies=_owner_cookie())
    assert r.status_code == 200
    assert "cytoscape" in r.text.lower()
    assert "/api/graph" in r.text
    assert 'id="cy"' in r.text


def test_api_graph_returns_snapshot_json():
    snap = {"nodes": [{"id": 1, "name": "A", "type": "person", "degree": 2}],
            "edges": [{"source": 1, "target": 1, "predicate": "x", "confidence": 0.5}]}
    with patch("dashboard.graph_routes.graph_snapshot",
               AsyncMock(return_value=dict(snap))) as gs:
        r = client.get("/api/graph?min_degree=3&limit=50", cookies=_owner_cookie())
    assert r.status_code == 200
    body = r.json()
    assert body["nodes"][0]["name"] == "A"
    assert body["focus_id"] is None
    # query params flow into the repo call
    kw = gs.await_args.kwargs
    assert kw["min_degree"] == 3 and kw["limit"] == 50 and kw["focus_id"] is None


def test_api_graph_resolves_name_to_focus():
    with patch("dashboard.graph_routes.find_entity_by_name",
               AsyncMock(return_value=42)) as fbn, \
         patch("dashboard.graph_routes.graph_snapshot",
               AsyncMock(return_value={"nodes": [], "edges": []})) as gs:
        r = client.get("/api/graph?q=Дима", cookies=_owner_cookie())
    assert r.status_code == 200
    fbn.assert_awaited_once()
    assert gs.await_args.kwargs["focus_id"] == 42
    assert r.json()["focus_id"] == 42
