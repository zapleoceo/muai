"""Regression: billable free-tier token must obey the GLOBAL daily cap.

$20-burn: Gemini key tier="free" but with billing enabled in Google → once the
free quota is exhausted Google charges silently (no 429). The cost-guard used to
skip every non-"paid" tier entirely (no reservation, no global cap). Now any call
with est_cost>0 is billable and gated by the global cap regardless of tier label.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

os.environ.setdefault("TOKEN_SECRET", "test-secret")


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_SECRET", "test-secret-for-crypto-tests-only")
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    from vera_shared.db.engine import Base, init_engine
    engine = await init_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    from vera_shared.db.engine import close_engine
    await close_engine()


@pytest_asyncio.fixture
async def free_gemini(db):
    """Gemini key labelled free, NO per-key cap — the dangerous config."""
    from vera_shared.tokens import repository as repo
    return await repo.upsert(
        provider="gemini", label="demoniwwwe",
        plaintext_token="AIza-test-1234567890",
        tier="free",
    )


@pytest.mark.asyncio
async def test_billable_free_token_blocked_by_global_cap(free_gemini, monkeypatch):
    """Free-tier Gemini over the global cap → refused, NOT silently billed."""
    from vera_shared.llm import client

    monkeypatch.setenv("VERA_DAILY_GLOBAL_CAP_USD", "2.0")

    async def fake_global_cost():
        return 5.0  # already over the $2 cap today
    monkeypatch.setattr(client, "global_cost_today", fake_global_cost)

    called = False

    async def fake_call(*a, **k):
        nonlocal called
        called = True
        return "x", {"model": "gemini-2.5-flash", "tokens_in": 1,
                     "tokens_out": 1, "latency_ms": 1}
    monkeypatch.setattr(client, "_call_provider", fake_call)

    with pytest.raises(client.LLMCallFailed):
        await client.chat([{"role": "user", "content": "hi"}], capability="vision")
    assert called is False, "provider must NOT be called when global cap exceeded"


@pytest.mark.asyncio
async def test_billable_free_token_allowed_under_cap_records_cost(free_gemini, monkeypatch):
    """Under the cap → call proceeds and real cost is recorded for the tally."""
    from vera_shared.llm import client
    from vera_shared.tokens import repository as repo

    monkeypatch.setenv("VERA_DAILY_GLOBAL_CAP_USD", "2.0")

    async def fake_global_cost():
        return 0.0
    monkeypatch.setattr(client, "global_cost_today", fake_global_cost)

    async def fake_call(*a, **k):
        return "hello", {"model": "gemini-2.5-flash", "tokens_in": 1000,
                         "tokens_out": 1000, "latency_ms": 5}
    monkeypatch.setattr(client, "_call_provider", fake_call)

    text, _meta = await client.chat([{"role": "user", "content": "hi"}], capability="vision")
    assert text == "hello"
    refreshed = await repo.get_by_id(free_gemini.id)
    assert refreshed.total_cost_usd > 0, "billable cost must be recorded onto the token"
