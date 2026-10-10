"""gateway.voice_intake — дедуп до свёртки и быстрый ответ слушателю.

Замер: свёртка до ~120 с, таймаут слушателя 60 с. Дедуп после свёртки давал
цикл «таймаут → ретрай → новая свёртка той же сессии».
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import gateway.voice as v
import gateway.voice_intake as vi
import pytest
from gateway.voice import Utterance, VoiceSession, VoiceSessionResult

_T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def _session() -> VoiceSession:
    return VoiceSession(started_at=_T0, ended_at=_T0 + timedelta(minutes=5),
                        app="zoom.exe", window_title="Созвон",
                        utterances=[Utterance(at=0.0, stream="mic", text="привет")])


@pytest.fixture(autouse=True)
def _secret_ok():
    with patch("gateway.voice.check_internal_secret", lambda s: None):
        yield


@pytest.mark.asyncio
async def test_existing_event_is_deduped_without_fold():
    distill = AsyncMock()
    with patch("gateway.voice.find_voice_event", AsyncMock(return_value=42)), \
         patch("gateway.voice.distill", distill):
        res = await v.ingest_voice_session(_session(), x_internal_secret="ok")
    assert res.ok and res.deduped and res.event_id is None
    distill.assert_not_called()


@pytest.mark.asyncio
async def test_slow_fold_answers_fast_and_retry_is_not_reprocessed():
    release = asyncio.Event()
    calls = 0

    async def slow_process(body, src_id):
        nonlocal calls
        calls += 1
        await release.wait()
        return VoiceSessionResult(ok=True, event_id=1)

    with patch("gateway.voice.find_voice_event", AsyncMock(return_value=None)), \
         patch("gateway.voice._process", slow_process), \
         patch.object(vi, "REPLY_WITHIN_S", 0.05):
        first = await v.ingest_voice_session(_session(), x_internal_secret="ok")
        retry = await v.ingest_voice_session(_session(), x_internal_secret="ok")
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert first.ok and first.accepted and first.event_id is None
    assert retry.ok and retry.accepted and retry.inflight and not retry.deduped
    assert calls == 1
    assert not vi.is_inflight(vi.voice_source_id(_T0, "zoom.exe", "Созвон"))


@pytest.mark.asyncio
async def test_background_fold_error_clears_inflight_and_logs(caplog):
    release = asyncio.Event()
    calls = 0

    async def failing_process(body, src_id):
        nonlocal calls
        calls += 1
        await release.wait()
        raise RuntimeError("broker down")

    with patch("gateway.voice.find_voice_event", AsyncMock(return_value=None)),          patch("gateway.voice._process", failing_process),          patch.object(vi, "REPLY_WITHIN_S", 0.05):
        first = await v.ingest_voice_session(_session(), x_internal_secret="ok")
        release.set()
        for _ in range(3):
            await asyncio.sleep(0)
        assert not vi.is_inflight(vi.voice_source_id(_T0, "zoom.exe", "Созвон"))
        assert "фоновая обработка" in caplog.text
        release.clear()
        again = await v.ingest_voice_session(_session(), x_internal_secret="ok")
        release.set()
        for _ in range(3):
            await asyncio.sleep(0)

    assert first.accepted and again.accepted and not again.inflight
    assert calls == 2


@pytest.mark.asyncio
async def test_fast_fold_returns_result_and_errors_still_propagate():
    async def boom():
        raise RuntimeError("cooling")

    with pytest.raises(RuntimeError):
        await vi.run_once("voice:x", boom)
    await asyncio.sleep(0)
    assert not vi.is_inflight("voice:x")


def test_source_id_is_stable():
    a = vi.voice_source_id(_T0, "zoom.exe", "t")
    assert a == vi.voice_source_id(_T0, "zoom.exe", "t")
    assert a.startswith("voice:") and len(a) == 22
