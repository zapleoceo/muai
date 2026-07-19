"""media_worker — recognition via broker + queue/retry policy.

Env defaults set before import (media_worker reads them at module load).
"""
# ruff: noqa: I001  # env setup intentionally split around imports
from __future__ import annotations

import os

os.environ.setdefault("INTERNAL_SECRET", "test-internal-secret")
os.environ.setdefault("BROKER_URL", "https://aib.zapleo.com")
os.environ.setdefault("BROKER_PROJECT_KEY", "aib_prj_test")

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

import media_worker.__main__ as mw  # noqa: E402
import media_worker.recognize as rec  # noqa: E402
import media_worker.repository as repo  # noqa: E402


def test_main_module_wires_up():
    # smoke: the glue module imports cleanly and carries the loop config
    assert mw.POLL_S >= 1
    assert mw.main_loop is not None


# ─── _is_permanent ─────────────────────────────────────────────────────────


def test_permanent_on_client_4xx():
    for e in ("broker vision HTTP 400: bad", "HTTP 401 unauth",
              "http 403 scope", "broker whisper HTTP 413: too big"):
        assert repo._is_permanent(e) is True


def test_transient_on_rate_limit_and_5xx():
    for e in ("broker vision HTTP 429: slow down",
              "broker whisper HTTP 503: no key",
              "broker vision HTTP 502: bad gateway",
              "download: connection reset"):
        assert repo._is_permanent(e) is False


def test_permanent_on_misconfig_and_empty():
    assert repo._is_permanent("BROKER_URL/BROKER_PROJECT_KEY not set") is True
    assert repo._is_permanent("broker vision returned empty text") is True


def test_permanent_on_oversize_and_timeout():
    # oversize файл не влезет никогда, зависший — зависнет снова: degrade сразу,
    # не жечь 3 ретрая (2m/15m/60m)
    assert repo._is_permanent("download: too large: 900000000 bytes (>26214400 limit)") is True
    assert repo._is_permanent("download: download timed out after 55s") is True


def test_no_provider_503_is_transient():
    # 503 "no provider available" = all gemini keys momentarily cooled
    # (free-tier churn). They recover in minutes, so this MUST be transient —
    # the backoff retry catches a live key. Degrading would lose the image.
    assert repo._is_permanent(
        "broker vision HTTP 503: no provider available for capability=vision"
    ) is False


# ─── _plan_failure (pure retry/degrade decision) ───────────────────────────


def test_plan_failure_first_transient_schedules_retry():
    plan = repo._plan_failure({}, "broker vision HTTP 503: no provider")
    assert plan["degrade"] is False
    assert plan["retry_count"] == 1
    assert plan["backoff_min"] == repo.BACKOFF_MIN[0]
    assert "retry#1" in plan["action"]


def test_plan_failure_escalates_backoff():
    p2 = repo._plan_failure({"media_retry_count": 1}, "HTTP 429")
    assert p2["retry_count"] == 2
    assert p2["backoff_min"] == repo.BACKOFF_MIN[1]


def test_plan_failure_degrades_after_max_retries():
    plan = repo._plan_failure({"media_retry_count": repo.MAX_MEDIA_RETRIES - 1},
                              "HTTP 503")
    assert plan["degrade"] is True
    assert plan["action"] == "degraded"


def test_plan_failure_degrades_immediately_on_permanent():
    plan = repo._plan_failure({}, "broker vision HTTP 403: scope")
    assert plan["degrade"] is True
    assert plan["action"] == "degraded(permanent)"


def test_plan_failure_handles_none_meta():
    plan = repo._plan_failure(None, "HTTP 503")
    assert plan["retry_count"] == 1


# ─── _claim_limit (pause + rate gate) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_limit_zero_when_paused():
    with patch.object(repo, "is_backfill_paused", AsyncMock(return_value=True)):
        assert await repo._claim_limit() == 0


@pytest.mark.asyncio
async def test_claim_limit_full_batch_when_unlimited():
    with patch.object(repo, "is_backfill_paused", AsyncMock(return_value=False)), \
         patch.object(repo, "reserve_backfill_allowance", AsyncMock(return_value=None)):
        assert await repo._claim_limit() == repo.BATCH


@pytest.mark.asyncio
async def test_claim_limit_capped_by_allowance():
    with patch.object(repo, "is_backfill_paused", AsyncMock(return_value=False)), \
         patch.object(repo, "reserve_backfill_allowance", AsyncMock(return_value=1)):
        assert await repo._claim_limit() == 1


@pytest.mark.asyncio
async def test_claim_batch_returns_empty_on_zero_limit():
    # limit<=0 short-circuits before touching the DB (both modes)
    assert await repo._claim_batch(0) == []
    assert await repo._claim_batch(0, voice_only=True) == []


def test_claim_batch_voice_only_filters_kind():
    # voice_only=True добавляет фильтр по kind, чтобы при капе vision
    # разбирать только whisper-события; без него — весь media_pending
    import inspect
    src = inspect.getsource(repo._claim_batch)
    assert "voice_only" in inspect.signature(repo._claim_batch).parameters
    assert "AND metadata->>'media_kind' IN ('voice','audio')" in src


def test_claim_batch_prioritises_voice_then_newest():
    # voice/audio (быстрый whisper) вперёд фото (медленный vision); внутри
    # класса — newest-first (живые впереди requeue-бэклога)
    import inspect
    src = inspect.getsource(repo._claim_batch)
    assert "IN ('voice','audio')) DESC, id DESC" in src


# ─── _broker_headers ───────────────────────────────────────────────────────


def test_broker_headers_carries_project_key():
    h = rec._broker_headers()
    assert h["X-Project-Key"] == "aib_prj_test"


def test_broker_headers_raises_when_unconfigured(monkeypatch):
    monkeypatch.setattr(rec, "BROKER_URL", "")
    with pytest.raises(RuntimeError, match="BROKER_URL"):
        rec._broker_headers()


# ─── _recognize_photo (broker vision) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_recognize_photo_sends_multimodal_and_returns_text():
    captured = {}

    async def fake_chat_async(*, messages, capability, event_id=None, **kw):
        captured["capability"] = capability
        captured["messages"] = messages
        captured["event_id"] = event_id
        return "на фото кот", {"provider": "gemini"}

    with patch.object(rec, "chat_async", AsyncMock(side_effect=fake_chat_async)):
        txt = await rec._recognize_photo("BASE64DATA", "image/jpeg", event_id=42)

    assert txt == "на фото кот"
    assert captured["capability"] == "vision"
    assert captured["event_id"] == 42
    content = captured["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,BASE64")


@pytest.mark.asyncio
async def test_recognize_photo_raises_on_broker_error():
    with patch.object(rec, "chat_async",
                      AsyncMock(side_effect=rec.LLMCallFailed("no provider"))), \
            pytest.raises(RuntimeError, match="no provider"):
        await rec._recognize_photo("x", "image/png")


@pytest.mark.asyncio
async def test_recognize_photo_raises_on_empty_text():
    with patch.object(rec, "chat_async",
                      AsyncMock(return_value=("   ", {"provider": "gemini"}))), \
            pytest.raises(RuntimeError, match="empty text"):
        await rec._recognize_photo("x", "image/png")


# ─── _recognize_audio (broker whisper) ─────────────────────────────────────


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(payload)

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_recognize_audio_returns_text():
    async def fake_post(self, url, params=None, files=None, headers=None, **kw):
        assert "transcribe" in url
        assert files is not None
        return _FakeResp(200, {"text": "привет это голосовое"})

    with patch("httpx.AsyncClient.post", fake_post):
        txt = await rec._recognize_audio(b"oggbytes", "audio/ogg")
    assert txt == "привет это голосовое"


@pytest.mark.asyncio
async def test_recognize_audio_placeholder_on_silence():
    async def fake_post(self, url, params=None, files=None, headers=None, **kw):
        return _FakeResp(200, {"text": ""})

    with patch("httpx.AsyncClient.post", fake_post):
        txt = await rec._recognize_audio(b"ogg", "audio/ogg")
    assert txt == rec._EMPTY_TRANSCRIPT


@pytest.mark.asyncio
async def test_recognize_audio_raises_on_http_error():
    async def fake_post(self, url, params=None, files=None, headers=None, **kw):
        return _FakeResp(503, {"detail": "no key"})

    with patch("httpx.AsyncClient.post", fake_post), \
            pytest.raises(RuntimeError, match="broker whisper HTTP 503"):
        await rec._recognize_audio(b"ogg", "audio/ogg")


@pytest.mark.asyncio
async def test_recognize_audio_rejects_oversize():
    big = b"x" * (rec._MAX_AUDIO_BYTES + 1)
    with pytest.raises(RuntimeError, match="413"):
        await rec._recognize_audio(big, "audio/ogg")


# ─── warm_entity_cache ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_warm_entity_cache_ok_first_try():
    async def fake_post(self, url, json=None, headers=None, **kw):
        assert url.endswith("/tools/list_dialogs")
        assert headers["X-Internal-Secret"] == "test-internal-secret"
        return _FakeResp(200, {"dialogs": [], "count": 42})

    with patch("httpx.AsyncClient.post", fake_post):
        assert await rec.warm_entity_cache(attempts=1) is True


@pytest.mark.asyncio
async def test_warm_entity_cache_gives_up_but_does_not_raise():
    async def fake_post(self, url, **kw):
        return _FakeResp(503, {})

    with patch("httpx.AsyncClient.post", fake_post):
        assert await rec.warm_entity_cache(attempts=2, delay_s=0) is False


# ─── _process_one routing ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_one_missing_metadata():
    seg, extra, err = await rec._process_one(
        {"id": 1, "content_text": "", "metadata": {}})
    assert seg == ""
    assert extra == {}
    assert "missing" in err


@pytest.mark.asyncio
async def test_process_one_photo_happy():
    row = {"id": 1, "content_text": "[photo]",
           "metadata": {"chat_id": 1, "msg_id": 2, "media_kind": "photo"}}
    with patch.object(rec, "_download",
                      AsyncMock(return_value=(b"img", "image/jpeg", None))), \
         patch.object(rec, "_recognize_photo",
                      AsyncMock(return_value="кот на диване")):
        seg, extra, err = await rec._process_one(row)
    assert err is None
    assert extra == {}
    assert "кот на диване" in seg


@pytest.mark.asyncio
async def test_process_one_download_fail_returns_err():
    row = {"id": 1, "content_text": "[photo]",
           "metadata": {"chat_id": 1, "msg_id": 2, "media_kind": "photo"}}
    with patch.object(rec, "_download",
                      AsyncMock(return_value=(None, None, "deleted"))):
        seg, extra, err = await rec._process_one(row)
    assert seg == ""
    assert "download" in err


@pytest.mark.asyncio
async def test_process_one_sticker_goes_through_vision():
    """Stickers (static webp) are recognized via vision, labelled distinctly."""
    row = {"id": 1, "content_text": "[sticker: 😂]",
           "metadata": {"chat_id": 1, "msg_id": 2, "media_kind": "sticker"}}
    with patch.object(rec, "_download",
                      AsyncMock(return_value=(b"webp", "image/webp", None))), \
         patch.object(rec, "_recognize_photo",
                      AsyncMock(return_value="смеющийся персонаж")):
        seg, extra, err = await rec._process_one(row)
    assert err is None
    assert "смеющийся персонаж" in seg
    assert "recognized sticker" in seg


@pytest.mark.asyncio
async def test_process_one_voice_marks_source():
    row = {"id": 1, "content_text": "[voice: 5s]",
           "metadata": {"chat_id": 1, "msg_id": 2, "media_kind": "voice"}}
    with patch.object(rec, "_download",
                      AsyncMock(return_value=(b"ogg", "audio/ogg", None))), \
         patch.object(rec, "_recognize_audio",
                      AsyncMock(return_value="привет")):
        seg, extra, err = await rec._process_one(row)
    assert err is None
    assert "привет" in seg
    assert "voice transcription" in seg
    assert extra == {"media_recognition": "ok_broker"}


@pytest.mark.asyncio
async def test_process_one_voice_fail_returns_err():
    row = {"id": 1, "content_text": "[voice: 5s]",
           "metadata": {"chat_id": 1, "msg_id": 2, "media_kind": "voice"}}
    with patch.object(rec, "_download",
                      AsyncMock(return_value=(b"ogg", "audio/ogg", None))), \
         patch.object(rec, "_recognize_audio",
                      AsyncMock(side_effect=RuntimeError("broker whisper HTTP 503"))):
        seg, extra, err = await rec._process_one(row)
    assert seg == ""
    assert "whisper" in err


# ─── _on_success / _on_failure (DB mocked) ─────────────────────────────────


class _FakeResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _FakeSession:
    """Async-ctx session whose execute() just records calls — no real DB."""
    def __init__(self, rowcount: int = 1):
        self.calls = []
        self.rowcount = rowcount

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))
        return _FakeResult(self.rowcount)


@pytest.mark.asyncio
async def test_on_success_merges_extra_meta():
    sess = _FakeSession()
    with patch.object(repo, "get_session", lambda: sess):
        await repo._on_success(9, "\n--- voice transcription ---\nтекст",
                               {"media_recognition": "ok_local"})
    sql, params = sess.calls[0]
    assert "content_text" in sql
    assert params["id"] == 9
    assert params["extra"] == '{"media_recognition": "ok_local"}'


@pytest.mark.asyncio
async def test_on_success_without_extra_meta_merges_empty():
    sess = _FakeSession()
    with patch.object(repo, "get_session", lambda: sess):
        await repo._on_success(9, "text", {})
    _sql, params = sess.calls[0]
    assert params["extra"] == "{}"


@pytest.mark.asyncio
async def test_on_success_guards_double_append():
    # rowcount=0 = кто-то уже финализировал (пережитый lease) — не падаем,
    # SQL держит guard по triage_status='media_pending'
    sess = _FakeSession(rowcount=0)
    with patch.object(repo, "get_session", lambda: sess):
        await repo._on_success(9, "text", {})
    sql, _params = sess.calls[0]
    assert "triage_status = 'media_pending'" in sql


@pytest.mark.asyncio
async def test_on_failure_degrade_branch_runs_sql():
    sess = _FakeSession()
    with patch.object(repo, "get_session", lambda: sess):
        action = await repo._on_failure(42, {}, "broker vision HTTP 403: scope")
    assert action == "degraded(permanent)"
    sql, params = sess.calls[0]
    assert "media_recognition" in sql
    assert params["id"] == 42


@pytest.mark.asyncio
async def test_on_failure_retry_branch_runs_sql():
    sess = _FakeSession()
    with patch.object(repo, "get_session", lambda: sess):
        action = await repo._on_failure(7, {}, "broker vision HTTP 503: busy")
    assert "retry#1" in action
    sql, params = sess.calls[0]
    assert "media_next_retry_at" in sql
    assert "make_interval" in sql
    assert params["cnt"] == 1
    assert params["backoff"] == repo.BACKOFF_MIN[0]
    assert params["id"] == 7
