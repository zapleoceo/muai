"""Сессия Claude Code → одно событие-выжимка, осмысленное фоном.

Главное, что проверяем: приём быстрый (осмыслить в запросе нельзя — одно окно
не укладывается и в 120с ожидания брокера), в мозг уходит смысл, а не переписка,
одна сессия остаётся одним событием даже если её дописали, и курсор клиента
двигается только за реально осмысленным.
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


class _Row:
    """Строка очереди — то, что воркер получает из claim()."""

    def __init__(self, **over):
        self.session_id = "26014a1e-94fe"
        self.project_dir = "D--Projects-myAI"
        self.cwd = "D:/Projects/myAI"
        self.git_branch = "master"
        self.started_at = _T0.replace(tzinfo=None)
        self.ended_at = (_T0 + timedelta(hours=2)).replace(tzinfo=None)
        self.turns = [{"role": "user", "text": "собери слушателя"},
                      {"role": "assistant", "text": "def secret_helper(): pass"}]
        self.turn_count = 2
        self.attempts = 1
        self.__dict__.update(over)


class _Sess:
    """Сессия-заглушка: запоминает параметры запроса и отдаёт заданный результат."""

    def __init__(self, value=101):
        self._value = value
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
                return outer._value

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
        """Замер 2026-08-26: самая большая живая сессия — 681 тыс. символов."""
        assert SPEC.max_windows * SPEC.window_chars >= 700_000

    def test_windows_are_distilled_in_parallel(self):
        """Окна независимы, а ждём мы брокер: без параллели 20 окон это часы."""
        assert SPEC.parallel > 1


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


class TestAccept:
    """Приём: только положить в очередь, никакой работы моделью."""

    @pytest.mark.asyncio
    async def test_session_goes_to_the_queue(self):
        import gateway.claude_session as cs
        from starlette.responses import Response

        sess = _Sess(value="26014a1e-94fe")
        with patch("gateway.claude_session.get_session", lambda: sess), \
             patch("gateway.claude_session.check_internal_secret", lambda s: None):
            res = await cs.accept_claude_session(_session(), Response(),
                                                 x_internal_secret="ok")

        assert res.ok and res.status == "pending" and res.turns == 2
        assert sess.params["session_id"] == "26014a1e-94fe"
        assert sess.params["turn_count"] == 2
        assert sess.params["status"] == "pending"
        # Время без зоны: колонки timestamp WITHOUT time zone.
        assert sess.params["started_at"].tzinfo is None

    @pytest.mark.asyncio
    async def test_already_distilled_is_not_requeued(self):
        """WHERE done_turns < turns отсекает повтор — модель зря не гоняем."""
        import gateway.claude_session as cs
        from starlette.responses import Response

        sess = _Sess(value=None)
        response = Response()
        with patch("gateway.claude_session.get_session", lambda: sess), \
             patch("gateway.claude_session.check_internal_secret", lambda s: None):
            res = await cs.accept_claude_session(_session(), response,
                                                 x_internal_secret="ok")

        assert res.ok and res.unchanged and res.status == "done"
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_accept_never_calls_the_model(self):
        """Осмысление в запросе давало 504 от nginx ровно на 60-й секунде."""
        import gateway.claude_session as cs
        from starlette.responses import Response

        model = AsyncMock(return_value=(json.dumps(_FULL), {}))
        with patch.object(fold_mod, "chat_async", model), \
             patch("gateway.claude_session.get_session", lambda: _Sess(value="x")), \
             patch("gateway.claude_session.check_internal_secret", lambda s: None):
            await cs.accept_claude_session(_session(), Response(),
                                           x_internal_secret="ok")

        model.assert_not_called()


class TestStoreSummary:
    @pytest.mark.asyncio
    async def test_code_never_reaches_the_event(self):
        import gateway.claude_session as cs

        sess = _Sess()
        with patch("gateway.claude_session.get_session", lambda: sess):
            event_id = await cs.store_summary(_Row(), _FULL, {"windows": 1})

        assert event_id == 101
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
        with patch("gateway.claude_session.get_session", lambda: sess):
            await cs.store_summary(_Row(), _FULL, {})

        assert sess.params["source_event_id"] == "session:26014a1e-94fe"

    @pytest.mark.asyncio
    async def test_updated_summary_goes_back_to_triage(self):
        """Иначе новый текст останется с прежним embedding и поиск найдёт старое."""
        import gateway.claude_session as cs

        sess = _Sess()
        with patch("gateway.claude_session.get_session", lambda: sess):
            await cs.store_summary(_Row(), _FULL, {})

        assert sess.params["triage_status"] == "pending"

    @pytest.mark.asyncio
    async def test_report_is_recorded_in_metadata(self):
        """Молчаливая потеря недопустима: сколько окон и был ли обрез — видно."""
        import gateway.claude_session as cs

        sess = _Sess()
        with patch("gateway.claude_session.get_session", lambda: sess):
            await cs.store_summary(_Row(turn_count=9), _FULL,
                                   {"windows": 3, "truncated": False})

        meta = sess.params["metadata"]
        assert meta["windows"] == 3 and meta["truncated"] is False
        assert meta["turns"] == 9
        assert meta["topics"] == ["слушатель", "упаковка"]


class TestWorker:
    @pytest.mark.asyncio
    async def test_empty_queue_reports_idle(self):
        import gateway.claude_session_worker as w

        with patch.object(w, "claim", AsyncMock(return_value=None)):
            assert await w.process_one() is False

    @pytest.mark.asyncio
    async def test_success_finishes_and_clears_the_transcript(self):
        import gateway.claude_session_worker as w

        finish = AsyncMock()
        with patch.object(w, "claim", AsyncMock(return_value=_Row())), \
             patch.object(w, "distill",
                          AsyncMock(return_value=(_FULL, {"transcript_chars": 120,
                                                          "windows": 1,
                                                          "truncated": False}))), \
             patch.object(w, "store_summary", AsyncMock(return_value=55)), \
             patch.object(w, "finish", finish):
            assert await w.process_one() is True

        finish.assert_awaited_once()
        assert finish.await_args.kwargs == {"event_id": 55, "turns": 2}

    @pytest.mark.asyncio
    async def test_failure_returns_the_session_to_the_queue(self):
        """Сессия не имеет права потеряться: клиент курсор ещё не двинул."""
        import gateway.claude_session_worker as w

        fail = AsyncMock()
        with patch.object(w, "claim", AsyncMock(return_value=_Row(attempts=1))), \
             patch.object(w, "distill", AsyncMock(side_effect=RuntimeError("брокер"))), \
             patch.object(w, "fail", fail):
            assert await w.process_one() is True

        fail.assert_awaited_once()
        assert "брокер" in fail.await_args.args[1]
        assert fail.await_args.args[2] == 1

    @pytest.mark.asyncio
    async def test_broker_is_given_a_long_deadline(self):
        """Дефолтные 120с мало: замер дал 126с на одно окно в 21 тыс. символов."""
        import gateway.claude_session_worker as w

        distill = AsyncMock(return_value=(_FULL, {"transcript_chars": 1,
                                                  "windows": 1, "truncated": False}))
        with patch.object(w, "claim", AsyncMock(return_value=_Row())), \
             patch.object(w, "distill", distill), \
             patch.object(w, "store_summary", AsyncMock(return_value=1)), \
             patch.object(w, "finish", AsyncMock()):
            await w.process_one()

        assert distill.await_args.kwargs["poll_deadline_s"] >= 600


class TestFold:
    @pytest.mark.asyncio
    async def test_long_session_is_folded_not_cut(self):
        """Хвост длинной сессии обязан попасть в выжимку — как у голоса."""
        from gateway.claude_distill import distill

        turns = [{"role": "user" if i % 2 == 0 else "assistant",
                  "text": f"шаг {i} " + "x" * 3000} for i in range(40)]
        calls: list[str] = []

        async def fake_chat(**kw):
            calls.append(kw["messages"][0]["content"])
            return json.dumps(_FULL), {}

        with patch.object(fold_mod, "chat_async", AsyncMock(side_effect=fake_chat)):
            got, report = await distill(turns, project="myAI", branch="master")

        assert report["windows"] > 1 and report["truncated"] is False
        assert report["merged"] == "llm"
        assert got["summary"] == _FULL["summary"]
        # Последняя реплика попала в один из промптов, а не была срезана.
        assert any("шаг 39" in prompt for prompt in calls)


class TestQueueOnSqlite:
    """Жизненный цикл очереди на живой БД — claim/finish/fail без моков."""

    @staticmethod
    async def _put(get_session, **over):
        from vera_shared.db.models import ClaudeSessionQueueRow
        values = {
            "session_id": "s-1", "project_dir": "myAI",
            "started_at": _T0.replace(tzinfo=None),
            "ended_at": _T0.replace(tzinfo=None),
            "turns": [{"role": "user", "text": "привет"}], "turn_count": 1,
        }
        values.update(over)
        async with get_session() as s:
            s.add(ClaudeSessionQueueRow(**values))

    @staticmethod
    async def _row(get_session, session_id="s-1"):
        from sqlalchemy import select
        from vera_shared.db.models import ClaudeSessionQueueRow
        async with get_session() as s:
            return (await s.execute(
                select(ClaudeSessionQueueRow)
                .where(ClaudeSessionQueueRow.session_id == session_id)
            )).scalar_one()

    @pytest.mark.asyncio
    async def test_claim_takes_pending_and_counts_the_attempt(self, sqlite_db):
        import gateway.claude_session_worker as w

        await self._put(sqlite_db)
        with patch.object(w, "get_session", sqlite_db):
            row = await w.claim()
        assert row is not None and row.session_id == "s-1"
        assert (await self._row(sqlite_db)).status == "processing"
        assert (await self._row(sqlite_db)).attempts == 1

    @pytest.mark.asyncio
    async def test_claim_on_empty_queue(self, sqlite_db):
        import gateway.claude_session_worker as w

        with patch.object(w, "get_session", sqlite_db):
            assert await w.claim() is None

    @pytest.mark.asyncio
    async def test_finish_clears_the_raw_transcript(self, sqlite_db):
        """Сырая переписка лежит в очереди ТОЛЬКО до осмысления."""
        import gateway.claude_session_worker as w

        await self._put(sqlite_db, status="processing")
        with patch.object(w, "get_session", sqlite_db):
            await w.finish("s-1", event_id=42, turns=1)
        row = await self._row(sqlite_db)
        assert row.status == "done" and row.event_id == 42
        assert row.done_turns == 1
        assert row.turns == []

    @pytest.mark.asyncio
    async def test_fail_returns_to_queue_until_attempts_run_out(self, sqlite_db):
        import gateway.claude_session_worker as w

        await self._put(sqlite_db, status="processing", attempts=1)
        with patch.object(w, "get_session", sqlite_db):
            await w.fail("s-1", "брокер лёг", attempts=1)
        assert (await self._row(sqlite_db)).status == "pending"

        with patch.object(w, "get_session", sqlite_db):
            await w.fail("s-1", "брокер лёг", attempts=w.MAX_ATTEMPTS)
        row = await self._row(sqlite_db)
        assert row.status == "error" and "брокер" in row.error

    @pytest.mark.asyncio
    async def test_revive_stale_returns_hung_processing(self, sqlite_db):
        """Перезапуск контейнера посреди осмысления не теряет сессию."""
        from datetime import timedelta

        import gateway.claude_session_worker as w

        stale = _T0.replace(tzinfo=None) - timedelta(hours=2)
        await self._put(sqlite_db, status="processing", updated_at=stale)
        await self._put(sqlite_db, session_id="s-2", status="processing")
        with patch.object(w, "get_session", sqlite_db):
            revived = await w.revive_stale()
        assert revived == 1
        assert (await self._row(sqlite_db)).status == "pending"
        assert (await self._row(sqlite_db, "s-2")).status == "processing"
