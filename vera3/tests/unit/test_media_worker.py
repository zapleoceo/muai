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


def test_permanent_on_broker_status_format():
    # Регрессия 17.09.2026: брокер отвечает "broker 400: ...", а маркеры знали
    # только литерал "http 400" — ошибка считалась временной, media_permanent
    # оставался false, и media_requeue.top_up возвращал событие в очередь каждые
    # три часа. Замер за 48 часов до фикса: 115 срабатываний broker 400.
    assert repo._is_permanent(
        'broker 400: {"detail":"inline image #1 is an MP4/MOV video container '
        '— the declared image/jpeg cannot be decoded by any vision provider"}'
    ) is True
    assert repo._is_permanent('broker 413: {"detail":"payload too big"}') is True
    assert repo._is_permanent("broker poll 404: job not found") is True


def test_transient_on_broker_status_format():
    # Обратная сторона той же монеты: помеченное постоянным не пробуется НИКОГДА,
    # поэтому темп (429) и отказы провайдера (5xx) обязаны остаться временными.
    for e in ('broker 429: {"detail":"rate limited"}',
              "broker 503: no provider available for capability=vision",
              "broker poll 502: bad gateway",
              "broker 500: internal error",
              "broker network: ConnectTimeout(host=broker, port=8080)"):
        assert repo._is_permanent(e) is False, e


def test_status_not_read_from_arbitrary_numbers():
    # Код читается только рядом со словом-маркером: цифры из текста ошибки
    # (размеры, id) не должны делать событие вечно-недостижимым.
    assert repo._is_permanent("download: connection reset after 404 bytes") is False


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


class _FakeClaimSession:
    """Мок-сессия: ловит выполненный SQL, возвращает пустой набор строк."""
    def __init__(self):
        self.sql = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        self.sql = str(stmt)
        from unittest.mock import MagicMock
        res = MagicMock()
        res.mappings.return_value.all.return_value = []
        return res


@pytest.mark.asyncio
async def test_claim_batch_builds_kind_filter_only_when_voice_only():
    sess = _FakeClaimSession()
    with patch.object(repo, "get_session", lambda: sess):
        assert await repo._claim_batch(5) == []
        assert "media_kind' IN ('voice','audio')" not in sess.sql.split("ORDER BY")[0]
    sess2 = _FakeClaimSession()
    with patch.object(repo, "get_session", lambda: sess2):
        assert await repo._claim_batch(5, voice_only=True) == []
        # фильтр в WHERE (до ORDER BY), не только в ORDER BY
        assert "media_kind' IN ('voice','audio')" in sess2.sql.split("ORDER BY")[0]


def test_lease_covers_worst_case_batch():
    """Строка не должна дожить до конца обработки с протухшим лизом — иначе её
    подхватит соседняя реплика и работа сгорит дважды (двойного текста не
    будет, finalize сверяет triage_status). Батч идёт параллельно, поэтому его
    длительность — максимум по строкам: дедлайн vision плюс скачивание."""
    worst_row_s = rec.VISION_DEADLINE_S + 60      # + скачивание (таймаут 55с)
    assert worst_row_s <= repo.LEASE_MIN * 60
    # Запас держим и на случай возврата к последовательной обработке.
    assert repo.LEASE_MIN * 60 >= repo.BATCH * rec.VISION_DEADLINE_S


def test_vision_deadline_exceeds_observed_local_max():
    # локальный `local/qwen3vl` замерен на 222с максимум; дефолт клиента
    # (120с) обрывал брокера на полпути
    assert rec.VISION_DEADLINE_S >= 300


@pytest.mark.asyncio
async def test_claim_batch_stamps_configured_lease():
    sess = _FakeClaimSession()
    with patch.object(repo, "get_session", lambda: sess):
        await repo._claim_batch(5)
    assert f"make_interval(mins => {repo.LEASE_MIN})" in sess.sql


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
        captured["kw"] = kw
        return "на фото кот", {"provider": "gemini"}

    with patch.object(rec, "chat_async", AsyncMock(side_effect=fake_chat_async)):
        txt = await rec._recognize_photo("BASE64DATA", "image/jpeg", event_id=42)

    assert txt == "на фото кот"
    assert captured["capability"] == "vision"
    assert captured["event_id"] == 42
    # без явного дедлайна chat_async взял бы 120с из broker_client и бросил
    # бы локальную vision-джобу, которую брокер ещё считает
    assert captured["kw"]["poll_deadline_s"] == rec.VISION_DEADLINE_S
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
    # метка успеха — иначе замер остатка считает фото несделанным вечно
    assert extra == {"media_recognition": "ok_broker"}
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
    assert params["perm"] == "true"


def test_download_failures_are_permanent_and_degrade_at_once():
    """Файла нет — ни ретраить, ни доливать. 12.09.2026: 307 попыток за 4 часа
    на «Could not find the input entity», ноль успехов — три круга на каждое
    такое фото это чистый простой очереди. Один предикат на оба решения."""
    for err in ("download: message not found",
                "download: no media on this message",
                "download: download returned None (deleted?)",
                "download: too large: 90000000 bytes (>25MB)",
                "broker vision HTTP 413: payload too large"):
        assert repo._is_permanent(err), err
        plan = repo._plan_failure({}, err)
        assert plan["degrade"] is True and plan["action"] == "degraded(permanent)", err


def test_missing_entity_gets_exactly_one_retry_for_a_cold_cache():
    """warm_entity_cache — best-effort: если ингестор ещё грузится, воркер
    через 5 минут идёт клеймить с холодным кэшем, и «нет пира» в этом окне —
    не приговор. Первая осечка → ретрай через 2 мин, вторая → permanent."""
    err = "download: ValueError: Could not find the input entity for PeerUser(1)"
    first = repo._plan_failure({}, err)
    assert first["degrade"] is False
    assert first["action"] == "retry#1 in 2m"
    second = repo._plan_failure({"media_retry_count": 1}, err)
    assert second["degrade"] is True
    assert second["action"] == "degraded(permanent)"
    # удалённое сообщение кэшем не лечится — permanent сразу
    assert repo._plan_failure({}, "download: message not found")["degrade"] is True


def test_transient_failures_stay_recoverable():
    # 503/429 лечатся временем — такое ретраим и доливаем обратно
    for err in ("vision: broker vision HTTP 503: no provider available",
                "broker vision HTTP 429: rate limit",
                "job still pending after 900s"):
        assert not repo._is_permanent(err), err


def test_number_from_the_message_body_is_not_a_status_code():
    """Число в ТЕКСТЕ ошибки — не код ответа, и хоронить по нему нельзя.

    Обычный длинный войс даёт `whisper: file is 413 seconds long`. Пока зазор
    между маркером и цифрами был широким, 413 читалось как HTTP 413, событие
    уезжало в `media_permanent` и терялось навсегда.
    """
    for err in ("whisper: file is 413 seconds long",
                "broker: upload of 404 files started",
                "vision returned after 500 ms of waiting"):
        assert not repo._is_permanent(err), err


def test_status_code_right_after_the_marker_is_still_caught():
    """Все четыре живых формата кода читаются — и 5xx среди них остаётся 5xx."""
    for err, code in (("http 413: audio > 25MB", 413),
                      ('broker 400: {"detail":"bad request"}', 400),
                      ("broker poll 502: gateway", 502),
                      ("broker whisper HTTP 413: too big", 413)):
        got = repo._STATUS_RE.search(err)
        assert got is not None and int(got.group(1)) == code, err
    # хоронится только 4xx кроме 429
    assert repo._is_permanent("http 413: audio > 25MB")
    assert repo._is_permanent('broker 400: {"detail":"bad request"}')
    assert not repo._is_permanent("broker poll 502: gateway")


@pytest.mark.asyncio
async def test_on_failure_marks_transient_degrade_as_recoverable():
    """Три провала подряд на 503 — событие деградирует, но пометить его
    недостижимым нельзя: пул оживёт, и фото надо будет добрать."""
    sess = _FakeSession()
    with patch.object(repo, "get_session", lambda: sess):
        action = await repo._on_failure(
            43, {"media_retry_count": 2}, "vision: broker vision HTTP 503: no provider")
    assert action == "degraded"
    _sql, params = sess.calls[0]
    assert params["perm"] == "false"


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


# ─── возобновление джобы брокера (media_job_id) ────────────────────────────


@pytest.mark.asyncio
async def test_process_one_carries_job_id_when_broker_is_still_working():
    """Дедлайн вышел, брокер считает — job_id уезжает в carry_meta, а не
    теряется: следующая попытка вернётся за результатом."""
    from vera_shared.llm.client import LLMJobPending
    row = {"id": 1, "content_text": "[photo]",
           "metadata": {"chat_id": 1, "msg_id": 2, "media_kind": "photo"}}
    with patch.object(rec, "_download",
                      AsyncMock(return_value=(b"img", "image/jpeg", None))), \
         patch.object(rec, "_recognize_photo",
                      AsyncMock(side_effect=LLMJobPending(482778, "job 482778 still pending"))):
        seg, extra, err = await rec._process_one(row)
    assert seg == ""
    assert extra == {"media_job_id": 482778}
    assert "still pending" in err
    assert not repo._is_permanent(err)


@pytest.mark.asyncio
async def test_process_one_resumes_the_remembered_job():
    row = {"id": 1, "content_text": "[photo]",
           "metadata": {"chat_id": 1, "msg_id": 2, "media_kind": "photo",
                        "media_job_id": 482778}}
    with patch.object(rec, "_download",
                      AsyncMock(return_value=(b"img", "image/jpeg", None))), \
         patch.object(rec, "_recognize_photo",
                      AsyncMock(return_value="кот")) as vision:
        _seg, _extra, err = await rec._process_one(row)
    assert err is None
    assert vision.await_args.kwargs["resume_job_id"] == 482778


@pytest.mark.asyncio
async def test_recognize_photo_forwards_resume_id_and_lets_pending_through():
    from vera_shared.llm.client import LLMJobPending
    captured = {}

    async def fake_chat_async(**kw):
        captured.update(kw)
        raise LLMJobPending(5, "job 5 still pending")

    with patch.object(rec, "chat_async", AsyncMock(side_effect=fake_chat_async)), \
         pytest.raises(LLMJobPending):
        await rec._recognize_photo("b64", "image/jpeg", resume_job_id=5)
    assert captured["resume_job_id"] == 5


@pytest.mark.asyncio
async def test_on_failure_retry_persists_carry_meta():
    sess = _FakeSession()
    with patch.object(repo, "get_session", lambda: sess):
        await repo._on_failure(7, {}, "vision: job 5 still pending after 900s",
                               carry_meta={"media_job_id": 5})
    sql, params = sess.calls[0]
    assert "CAST(:carry AS jsonb)" in sql
    assert params["carry"] == '{"media_job_id": 5}'
    # старая ссылка стирается до слияния: жива, только если carry принёс её заново
    assert "- 'media_job_id') || CAST(:carry AS jsonb)" in sql


@pytest.mark.asyncio
async def test_on_success_forgets_the_job_id():
    sess = _FakeSession()
    with patch.object(repo, "get_session", lambda: sess):
        await repo._on_success(9, "text", {"media_recognition": "ok_broker"})
    sql, _params = sess.calls[0]
    assert "- 'media_job_id'" in sql


# ─── параллельная обработка батча ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_rows_are_processed_concurrently():
    """Последовательный батч упирал темп в одно фото за раз (145с → потолок
    24 в час), при том что локальный слот брокера простаивал 88% времени.
    Проверяем именно ОДНОВРЕМЕННОСТЬ: второй ряд стартует, не дожидаясь
    первого."""
    import asyncio

    started: list[int] = []
    release = asyncio.Event()

    async def slow_process(row):
        started.append(row["id"])
        if row["id"] == 1:
            await release.wait()          # первый «зависает» на распознавании
        return "txt", {}, None

    with patch.object(mw, "_process_one", slow_process), \
         patch.object(mw, "_on_success", AsyncMock()), \
         patch.object(mw, "_on_failure", AsyncMock()):
        gathered = asyncio.gather(
            *(mw._handle_row({"id": i, "metadata": {}}) for i in (1, 2, 3)))
        await asyncio.sleep(0)            # дать очереди событий провернуться
        await asyncio.sleep(0)
        assert started == [1, 2, 3], "строки 2 и 3 ждут первую — обработка последовательная"
        release.set()
        await gathered


@pytest.mark.asyncio
async def test_one_bad_row_does_not_sink_its_neighbours():
    """gather без изоляции уронил бы весь батч на одном исключении, и соседние
    строки остались бы захваченными до истечения лиза."""
    import asyncio

    async def boom_for_two(row):
        if row["id"] == 2:
            raise RuntimeError("нежданное")
        return "txt", {}, None

    ok = AsyncMock()
    fail = AsyncMock(return_value="degraded")
    with patch.object(mw, "_process_one", boom_for_two), \
         patch.object(mw, "_on_success", ok), \
         patch.object(mw, "_on_failure", fail):
        await asyncio.gather(
            *(mw._handle_row({"id": i, "metadata": {}}) for i in (1, 2, 3)))

    assert {c.args[0] for c in ok.await_args_list} == {1, 3}
    assert fail.await_args.args[0] == 2
    assert "unexpected: RuntimeError" in fail.await_args.args[2]
