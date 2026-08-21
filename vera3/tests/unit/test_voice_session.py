"""gateway.voice — приём разговора с ноутбука.

Главное свойство, которое тут закрепляется: дословная расшифровка НЕ должна
попадать в событие. В мозг идёт выжимка, сырой текст остаётся на ноутбуке.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from gateway.voice import Utterance, VoiceSession, _body, _render

_T0 = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)


def _session(**over):
    base = {
        "started_at": _T0, "ended_at": _T0 + timedelta(minutes=12),
        "app": "telegram.exe", "window_title": "Ли — Telegram",
        "utterances": [
            Utterance(at=0.0, stream="mic", text="Ли, по разделу шесть — сорок пять?"),
            Utterance(at=4.0, stream="system", text="Пусть будет пятьдесят, до пятницы."),
        ],
    }
    base.update(over)
    return VoiceSession(**base)


def test_render_marks_who_is_speaking():
    text = _render(_session().utterances)
    assert "[я] Ли, по разделу шесть" in text
    assert "[собеседник] Пусть будет пятьдесят" in text


def test_render_skips_empty_utterances():
    u = [Utterance(at=0, stream="mic", text="   "),
         Utterance(at=1, stream="system", text="есть")]
    assert _render(u) == "[собеседник] есть"


def test_body_keeps_structure_and_quotes():
    d = {"summary": "Обсудили выход по разделу 6.",
         "counterparts": ["Ли"], "topics": ["Veranda", "выход"],
         "decisions": ["цена 50к"], "commitments": ["Ли пришлёт бумаги до пятницы"],
         "numbers": ["50 000 USD", "пятница"],
         "key_quotes": ["Пусть будет пятьдесят, до пятницы."]}
    body = _body(d, "telegram.exe", "Ли — Telegram")
    assert body.startswith("Обсудили выход по разделу 6.")
    for expected in ("Участники: Ли", "Решения: цена 50к",
                     "Договорённости: Ли пришлёт бумаги",
                     "Числа и сроки: 50 000 USD", "— Пусть будет пятьдесят",
                     "Где: telegram.exe / Ли — Telegram"):
        assert expected in body


def test_body_omits_empty_sections():
    body = _body({"summary": "Короткий звонок.", "counterparts": [],
                  "topics": [], "decisions": [], "commitments": [],
                  "numbers": [], "key_quotes": []}, None, None)
    assert body == "Короткий звонок."


@pytest.mark.asyncio
async def test_verbatim_never_reaches_the_event():
    """Регресс: в content_text уходит выжимка, а не расшифровка."""
    import gateway.voice as v

    secret_phrase = "Ли, по разделу шесть — сорок пять?"
    distilled = json.dumps({
        "summary": "Обсудили цену выхода.", "counterparts": ["Ли"],
        "topics": ["Veranda"], "decisions": [], "commitments": [],
        "numbers": ["50 000"], "key_quotes": []})

    captured = {}

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt):
            captured["stmt"] = str(stmt)
            captured["params"] = stmt.compile().params
            class R:
                def scalar_one_or_none(self_inner): return 777
            return R()

    with patch.object(v, "chat_async", AsyncMock(return_value=(distilled, {}))), \
         patch.object(v, "get_session", lambda: _Sess()), \
         patch.object(v, "check_internal_secret", lambda s: None):
        res = await v.ingest_voice_session(_session(), x_internal_secret="ok")

    assert res.ok and res.event_id == 777
    body = captured["params"]["content_text"]
    assert "Обсудили цену выхода." in body
    assert secret_phrase not in body          # дословное НЕ сохраняется
    assert captured["params"]["source"] == "voice"


@pytest.mark.asyncio
async def test_broker_failure_still_stores_the_fact():
    """Сбой осмысления не должен терять сам факт разговора."""
    import gateway.voice as v
    from vera_shared.llm.client import LLMCallFailed

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt):
            self.params = stmt.compile().params
            class R:
                def scalar_one_or_none(self_inner): return 5
            return R()

    sess = _Sess()
    with patch.object(v, "chat_async", AsyncMock(side_effect=LLMCallFailed("down"))), \
         patch.object(v, "get_session", lambda: sess), \
         patch.object(v, "check_internal_secret", lambda s: None):
        res = await v.ingest_voice_session(_session(), x_internal_secret="ok")

    assert res.ok and res.event_id == 5
    assert "не удалось осмыслить" in sess.params["content_text"]
    assert sess.params["metadata"]["app"] == "telegram.exe"


@pytest.mark.asyncio
async def test_same_session_resent_is_deduped():
    import gateway.voice as v

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt):
            class R:
                def scalar_one_or_none(self_inner): return None   # ON CONFLICT
            return R()

    with patch.object(v, "chat_async", AsyncMock(return_value=(json.dumps(v._EMPTY), {}))), \
         patch.object(v, "get_session", lambda: _Sess()), \
         patch.object(v, "check_internal_secret", lambda s: None):
        res = await v.ingest_voice_session(_session(), x_internal_secret="ok")

    assert res.ok and res.deduped and res.event_id is None
