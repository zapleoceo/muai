"""vera_shared.llm.broker_client — toggling, response unpacking, fallback."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import vera_shared.llm.broker_client as bc


# ─── _log_usage — null-coalescing (found under concurrent-load testing) ────


@pytest.mark.asyncio
async def test_log_usage_coalesces_present_but_null_provider_and_model():
    """Broker responses under load can have provider/model KEY PRESENT with
    an explicit null (job raced/errored mid-write) — dict.get(key, default)
    only falls back when the key is ABSENT, so it let None straight through
    into usage_log's NOT NULL provider/model columns, crashing the insert."""
    captured = {}

    class _FakeSession:
        def add(self, row):
            captured["row"] = row

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *exc):
            return False

    with patch.object(bc, "get_session", lambda: _FakeCtx()):
        await bc._log_usage(
            {"provider": None, "model": None, "tokens_in": 5, "tokens_out": 1,
             "cost_usd": 0.0, "latency_ms": 10, "request_id": None, "key_label": None},
            workflow="test", event_id=None, capability="chat:fast",
        )

    row = captured["row"]
    assert row.provider == "broker"
    assert row.model == ""


def test_broker_enabled_requires_both_vars(monkeypatch):
    monkeypatch.setattr(bc, "BROKER_URL", "")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "")
    assert not bc.broker_enabled()

    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "")
    assert not bc.broker_enabled()

    monkeypatch.setattr(bc, "BROKER_URL", "")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    assert not bc.broker_enabled()

    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    assert bc.broker_enabled()


@pytest.mark.asyncio
async def test_chat_via_broker_unpacks_response(monkeypatch):
    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    monkeypatch.setattr(bc, "_http", None)

    fake = AsyncMock()
    fake.status_code = 200
    fake.json = lambda: {
        "text": "hello dima",
        "provider": "cerebras",
        "model": "cerebras/gpt-oss-120b",
        "tokens_in": 12,
        "tokens_out": 3,
        "cost_usd": 0.0,
        "latency_ms": 451,
    }

    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=fake)), \
         patch.object(bc, "_log_usage", AsyncMock()):
        text, meta = await bc.chat_via_broker(
            messages=[{"role": "user", "content": "x"}],
            capability="chat:fast",
        )
    assert text == "hello dima"
    assert meta["provider"] == "cerebras"
    assert meta["tokens_in"] == 12
    assert meta["latency_ms"] == 451


@pytest.mark.asyncio
async def test_chat_via_broker_raises_on_5xx(monkeypatch):
    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    monkeypatch.setattr(bc, "_http", None)

    fake = AsyncMock()
    fake.status_code = 503
    fake.text = "all providers exhausted"
    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=fake)), \
         pytest.raises(bc.BrokerCallFailed, match="503"):
        await bc.chat_via_broker(
            messages=[{"role": "user", "content": "x"}],
            capability="chat:fast",
        )


@pytest.mark.asyncio
async def test_embed_via_broker_with_str_input(monkeypatch):
    """str input wraps to [input] — verify it doesn't iterate over chars."""
    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    monkeypatch.setattr(bc, "_http", None)

    captured = {}

    async def fake_post(self, url, params=None, json=None):
        captured["url"] = url
        captured["json"] = json
        r = AsyncMock()
        r.status_code = 200
        r.json = lambda: {
            "embeddings": [[0.1, 0.2, 0.3]],
            "provider": "voyage",
            "model": "voyage/voyage-3",
            "tokens_in": 1,
            "cost_usd": 0.0,
            "latency_ms": 99,
        }
        return r

    with patch.object(httpx.AsyncClient, "post", fake_post), \
         patch.object(bc, "_log_usage", AsyncMock()):
        vectors = await bc.embed_via_broker("hello")

    assert vectors == [[0.1, 0.2, 0.3]]
    assert captured["json"]["input"] == ["hello"]  # NOT ['h', 'e', 'l', ...]


# ─── chat_async_via_broker (submit+poll /v1/jobs) ──────────────────────────


def _fake_submit(job_id=1, poll_after_s=2):
    r = AsyncMock()
    r.status_code = 202
    r.json = lambda: {"job_id": job_id, "status": "pending",
                       "poll_url": f"/v1/jobs/{job_id}", "poll_after_s": poll_after_s}
    return r


def _fake_poll(status, **extra):
    r = AsyncMock()
    r.status_code = 200
    body = {"job_id": 1, "status": status, **extra}
    r.json = lambda: body
    return r


@pytest.mark.asyncio
async def test_chat_async_via_broker_polls_until_done(monkeypatch):
    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    monkeypatch.setattr(bc, "_http", None)

    poll_responses = [
        _fake_poll("pending", poll_after_s=2),
        _fake_poll("done", text="hello dima", provider="cerebras",
                   model="cerebras/gpt-oss-120b", tokens_in=12, tokens_out=3,
                   cost_usd=0.0, latency_ms=451, request_id=999, key_label="k1"),
    ]

    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=_fake_submit())), \
         patch.object(httpx.AsyncClient, "get", AsyncMock(side_effect=poll_responses)), \
         patch.object(bc.asyncio, "sleep", AsyncMock()), \
         patch.object(bc, "_log_usage", AsyncMock()) as log_mock:
        text, meta = await bc.chat_async_via_broker(
            messages=[{"role": "user", "content": "x"}], capability="chat:fast",
        )

    assert text == "hello dima"
    assert meta["provider"] == "cerebras"
    assert meta["tokens_in"] == 12
    log_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_async_via_broker_raises_on_job_error(monkeypatch):
    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    monkeypatch.setattr(bc, "_http", None)

    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=_fake_submit())), \
         patch.object(httpx.AsyncClient, "get",
                       AsyncMock(return_value=_fake_poll("error", error="all providers failed"))), \
         patch.object(bc.asyncio, "sleep", AsyncMock()), \
         pytest.raises(bc.BrokerCallFailed, match="all providers failed"):
        await bc.chat_async_via_broker(
            messages=[{"role": "user", "content": "x"}], capability="chat:fast",
        )


@pytest.mark.asyncio
async def test_chat_async_via_broker_raises_on_submit_5xx(monkeypatch):
    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    monkeypatch.setattr(bc, "_http", None)

    fake = AsyncMock()
    fake.status_code = 503
    fake.text = "capability not available as async job"
    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=fake)), \
         pytest.raises(bc.BrokerCallFailed, match="503"):
        await bc.chat_async_via_broker(
            messages=[{"role": "user", "content": "x"}], capability="chat:fast",
        )


@pytest.mark.asyncio
async def test_chat_async_via_broker_raises_after_deadline(monkeypatch):
    """Job stays 'pending' forever — must give up after JOB_POLL_DEADLINE_S,
    not poll the broker indefinitely."""
    monkeypatch.setattr(bc, "BROKER_URL", "https://aib.zapleo.com")
    monkeypatch.setattr(bc, "BROKER_PROJECT_KEY", "aib_prj_xxx")
    monkeypatch.setattr(bc, "_http", None)
    monkeypatch.setattr(bc, "JOB_POLL_DEADLINE_S", 10.0)

    # monotonic(): once to set the deadline (t=0), then always past it (t=100).
    times = iter([0.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(bc.time, "monotonic", lambda: next(times, 100.0))

    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=_fake_submit())), \
         patch.object(httpx.AsyncClient, "get",
                       AsyncMock(return_value=_fake_poll("pending", poll_after_s=2))), \
         patch.object(bc.asyncio, "sleep", AsyncMock()), \
         pytest.raises(bc.BrokerCallFailed, match="still pending"):
        await bc.chat_async_via_broker(
            messages=[{"role": "user", "content": "x"}], capability="chat:fast",
        )
