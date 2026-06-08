"""Service-level тесты gateway. SQLite in-memory для скорости."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def app_client(tmp_path):
    """Создаём приложение с file-based SQLite — иначе lifespan и фикстура
    создают отдельные in-memory DB и тесты падают на dedup конфликте."""
    db_file = tmp_path / "test.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    os.environ["DATABASE_URL"] = db_url
    os.environ["INTERNAL_SECRET"] = "test-internal-secret"

    # Reset module state
    import vera_shared.db.engine as engine_mod
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None
    import gateway.config as cfg
    cfg._settings = None

    from vera_shared.db.engine import init_engine
    from vera_shared.db.models import Base

    engine = await init_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from gateway.app import create_app
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await engine.dispose()
    engine_mod._engine = None
    engine_mod.AsyncSessionLocal = None


def _make_event(source="gmail", sid="msg_test_1", text="hello"):
    return {
        "source": source,
        "source_event_id": sid,
        "occurred_at": datetime(2026, 6, 8, 10, 0).isoformat(),
        "content_text": text,
        "category": "generic",
    }


@pytest.mark.asyncio
class TestHealth:
    async def test_healthz(self, app_client):
        r = await app_client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    async def test_root(self, app_client):
        r = await app_client.get("/")
        assert r.status_code == 200
        assert r.json()["service"] == "vera-gateway"


@pytest.mark.asyncio
class TestIngestEvent:
    async def test_ingest_basic(self, app_client):
        r = await app_client.post(
            "/event/gmail",
            json=_make_event(),
            headers={"X-Internal-Secret": "test-internal-secret"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["ok"] is True
        assert body["deduped"] is False
        assert "event_id" in body

    async def test_dedup_on_same_source_event_id(self, app_client):
        r1 = await app_client.post(
            "/event/gmail",
            json=_make_event(sid="dedup_test"),
            headers={"X-Internal-Secret": "test-internal-secret"},
        )
        assert r1.status_code == 201
        assert r1.json()["deduped"] is False

        r2 = await app_client.post(
            "/event/gmail",
            json=_make_event(sid="dedup_test"),
            headers={"X-Internal-Secret": "test-internal-secret"},
        )
        assert r2.status_code == 201
        assert r2.json()["deduped"] is True
        assert r1.json()["event_id"] == r2.json()["event_id"]

    async def test_source_mismatch_rejected(self, app_client):
        r = await app_client.post(
            "/event/telegram",
            json=_make_event(source="gmail"),
            headers={"X-Internal-Secret": "test-internal-secret"},
        )
        assert r.status_code == 400

    async def test_invalid_secret_rejected(self, app_client):
        r = await app_client.post(
            "/event/gmail",
            json=_make_event(),
            headers={"X-Internal-Secret": "wrong"},
        )
        assert r.status_code == 401

    async def test_invalid_payload_rejected(self, app_client):
        r = await app_client.post(
            "/event/gmail",
            json={"source": "gmail"},
            headers={"X-Internal-Secret": "test-internal-secret"},
        )
        assert r.status_code == 422


@pytest.mark.asyncio
class TestGetEvent:
    async def test_get_existing(self, app_client):
        r = await app_client.post(
            "/event/gmail",
            json=_make_event(sid="get_test"),
            headers={"X-Internal-Secret": "test-internal-secret"},
        )
        event_id = r.json()["event_id"]

        r = await app_client.get(f"/api/events/{event_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == event_id
        assert body["source"] == "gmail"
        assert body["triage_status"] == "pending"

    async def test_get_not_found(self, app_client):
        r = await app_client.get("/api/events/999999999")
        assert r.status_code == 404
