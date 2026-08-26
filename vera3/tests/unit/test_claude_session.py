"""Сессия Claude Code → одно событие-выжимка.

Главное, что проверяем: в мозг уходит смысл, а не переписка (кода в теле
события быть не должно), одна сессия остаётся одним событием даже если её
дописали, и повторная присылка того же не гоняет модель заново.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from gateway.claude_distill import SPEC, render
from gateway.claude_session import ClaudeSession, Turn, body_text
from vera_shared.llm import fold as fold_mod

_T0 = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)

_FULL = {
    "summary": "Вынесли свёртку в общий модуль и починили COM в упакованном exe.",
    "topics": ["слушатель", "упаковка"],
    "outline": ["собрали exe", "поймали блок Smart App Control"],
    "decisions": ["взять подписанную копию pythonw вместо своей сборки"],
    "changes": ["vera_shared/llm/fold.py", "winctx.py"],
    "problems": ["pycaw падал на инициализации COM"],
    "open_ends": ["зарегистрировать синк в планировщике"],
    "numbers": ["255 МБ"],
}


def _session(**over):
    base = {
        "session_id": "26014a1e-94fe",
        "project_dir": "D--Projects-myAI",
        "started_at": _T0,
        "ended_at": _T0 + timedelta(hours=2),
        "cwd": "D:/Projects/myAI",
        "git_branch": "master",
        "turns": [
            Turn(role="user", text="собери слушателя в приложение"),
            Turn(role="assistant", text="def secret_helper(): return 42"),
        ],
    }
    base.update(over)
    return ClaudeSession(**base)


class _Sess:
    """Сессия-заглушка: запоминает параметры INSERT и отдаёт заданный id."""

    def __init__(self, event_id=101):
        self._event_id = event_id
        self.params: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        self.params = stmt.compile().params
        outer = self

        class R:
            def scalar_one_or_none(self):
                return outer._event_id

        return R()


class TestRender:
    def test_marks_who_is_speaking(self):
        lines = render(_session().turns)
        assert lines[0].startswith("[Дима] собери слушателя")
        assert lines[1].startswith("[Claude] def secret_helper")

    def test_skips_empty_turns(self):
        turns = [Turn(role="user", text="   "), Turn(role="assistant", text="есть")]
        assert render(turns) == ["[Claude] есть"]


class TestSpec:
    def test_schema_covers_every_field(self):
        schema = SPEC.json_schema["json_schema"]["schema"]
        assert set(schema["required"]) == set(SPEC.fields)
        assert schema["properties"]["summary"]["type"] == "string"
        assert schema["properties"]["changes"]["type"] == "array"

    def test_window_cap_fits_the_longest_real_session(self):
        """Замер 2026-08-26: самая большая сессия — 681 тыс. символов."""
        assert SPEC.max_windows * SPEC.window_chars >= 700_000


class TestBodyText:
    def test_keeps_the_structure(self):
        body = body_text(_FULL, "D--Projects-myAI", "master")
        assert body.startswith("Вынесли свёртку")
        assert "Изменено: vera_shared/llm/fold.py; winctx.py" in body
        assert "Осталось: зарегистрировать синк в планировщике" in body
        assert "— собрали exe" in body
        assert "Где: D--Projects-myAI / master" in body

    def test_omits_empty_sections(self):
        body = body_text({"summary": "коротко", "topics": [], "changes": []},
                         "проект", None)
        assert body.strip() == "коротко\n\nГде: проект".strip()


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_code_never_reaches_the_event(self):
        import gateway.claude_session as cs

        sess = _Sess()
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(return_value=(json.dumps(_FULL), {}))), \
             patch("gateway.claude_session.get_session", lambda: sess), \
             patch("gateway.claude_session.check_internal_secret", lambda s: None):
            res = await cs.ingest_claude_session(_session(), x_internal_secret="ok")

        assert res.ok and res.event_id == 101
        body = sess.params["content_text"]
        assert "Вынесли свёртку" in body
        assert "secret_helper" not in body          # переписка НЕ сохраняется
        assert sess.params["source"] == "claude_chat"
        assert sess.params["category"] == "session"

    @pytest.mark.asyncio
    async def test_one_session_is_one_event(self):
        """Дописанная сессия обновляет свою выжимку, а не заводит вторую."""
        import gateway.claude_session as cs

        sess = _Sess()
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(return_value=(json.dumps(_FULL), {}))), \
             patch("gateway.claude_session.get_session", lambda: sess), \
             patch("gateway.claude_session.check_internal_secret", lambda s: None):
            await cs.ingest_claude_session(_session(), x_internal_secret="ok")

        assert sess.params["source_event_id"] == "session:26014a1e-94fe"

    @pytest.mark.asyncio
    async def test_updated_summary_goes_back_to_triage(self):
        """Иначе новый текст останется с прежним embedding и поиск найдёт старое."""
        import gateway.claude_session as cs

        sess = _Sess()
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(return_value=(json.dumps(_FULL), {}))), \
             patch("gateway.claude_session.get_session", lambda: sess), \
             patch("gateway.claude_session.check_internal_secret", lambda s: None):
            await cs.ingest_claude_session(_session(), x_internal_secret="ok")

        assert sess.params["triage_status"] == "pending"

    @pytest.mark.asyncio
    async def test_nothing_new_is_reported_as_unchanged(self):
        """Реплик не прибавилось — WHERE отсекает UPDATE, событие не тронуто."""
        import gateway.claude_session as cs

        sess = _Sess(event_id=None)
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(return_value=(json.dumps(_FULL), {}))), \
             patch("gateway.claude_session.get_session", lambda: sess), \
             patch("gateway.claude_session.check_internal_secret", lambda s: None):
            res = await cs.ingest_claude_session(_session(), x_internal_secret="ok")

        assert res.ok and res.unchanged and res.event_id is None

    @pytest.mark.asyncio
    async def test_broker_failure_still_stores_the_fact(self):
        import gateway.claude_session as cs
        from vera_shared.llm.client import LLMCallFailed

        sess = _Sess(event_id=7)
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(side_effect=LLMCallFailed("down"))), \
             patch("gateway.claude_session.get_session", lambda: sess), \
             patch("gateway.claude_session.check_internal_secret", lambda s: None):
            res = await cs.ingest_claude_session(_session(), x_internal_secret="ok")

        assert res.ok and res.event_id == 7
        assert "не удалось осмыслить" in sess.params["content_text"]
        assert sess.params["metadata"]["distilled"] is False
        assert sess.params["metadata"]["turns"] == 2

    @pytest.mark.asyncio
    async def test_long_session_is_folded_not_cut(self):
        """Хвост длинной сессии обязан попасть в выжимку — как у голоса."""
        import gateway.claude_session as cs

        turns = [Turn(role="user" if i % 2 == 0 else "assistant",
                      text=f"шаг {i} " + "x" * 3000) for i in range(40)]
        calls: list[str] = []

        async def fake_chat(**kw):
            prompt = kw["messages"][0]["content"]
            calls.append(prompt)
            return json.dumps(_FULL), {}

        sess = _Sess()
        with patch.object(fold_mod, "chat_async", AsyncMock(side_effect=fake_chat)), \
             patch("gateway.claude_session.get_session", lambda: sess), \
             patch("gateway.claude_session.check_internal_secret", lambda s: None):
            await cs.ingest_claude_session(_session(turns=turns),
                                           x_internal_secret="ok")

        meta = sess.params["metadata"]
        assert meta["windows"] > 1
        assert meta["truncated"] is False
        assert meta["merged"] == "llm"
        # Последняя реплика попала в один из промптов, а не была срезана.
        assert any("шаг 39" in prompt for prompt in calls)

    @pytest.mark.asyncio
    async def test_timezone_aware_stamp_is_stored_naive(self):
        """occurred_at — timestamp WITHOUT time zone: со зоной asyncpg падает."""
        import gateway.claude_session as cs

        sess = _Sess()
        with patch.object(fold_mod, "chat_async",
                          AsyncMock(return_value=(json.dumps(_FULL), {}))), \
             patch("gateway.claude_session.get_session", lambda: sess), \
             patch("gateway.claude_session.check_internal_secret", lambda s: None):
            await cs.ingest_claude_session(_session(), x_internal_secret="ok")

        assert sess.params["occurred_at"].tzinfo is None
