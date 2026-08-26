"""Откуда поллер берёт токен и как гасит мёртвый.

Порядок источников важен: строка из дашборда старше окружения, иначе после
подключения через UI поллер продолжал бы ходить со старым токеном из .env.
"""
from __future__ import annotations

import base64
import os

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())

import pytest  # noqa: E402
from ingestor_slack import auth  # noqa: E402
from sqlalchemy import select  # noqa: E402
from vera_shared.crypto import encrypt  # noqa: E402
from vera_shared.db.models_sources import SlackAuthRow  # noqa: E402


async def _add(token: str = "xoxp-from-db", active: bool = True,
               team: str = "T1", raw: str | None = None) -> int:
    from vera_shared.db.engine import get_session
    async with get_session() as s:
        row = SlackAuthRow(team_id=team, team_name="Sintegrum Team",
                           user_id="U0ME", username="dima",
                           token_enc=raw if raw is not None else encrypt(token),
                           is_active=active)
        s.add(row)
    async with get_session() as s:
        return (await s.execute(
            select(SlackAuthRow.id).where(SlackAuthRow.team_id == team)
        )).scalar_one()


async def _row(team: str = "T1") -> SlackAuthRow:
    from vera_shared.db.engine import get_session
    async with get_session() as s:
        return (await s.execute(
            select(SlackAuthRow).where(SlackAuthRow.team_id == team)
        )).scalar_one()


@pytest.mark.usefixtures("sqlite_db")
class TestLoadToken:

    @pytest.mark.asyncio
    async def test_active_row_wins_over_environment(self, monkeypatch):
        monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-from-env")
        row_id = await _add("xoxp-from-db")
        token, got_id = await auth.load_token()
        assert (token, got_id) == ("xoxp-from-db", row_id)

    @pytest.mark.asyncio
    async def test_environment_is_the_fallback(self, monkeypatch):
        """Уже подключённый источник не должен отвалиться от появления таблицы."""
        monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-from-env")
        token, row_id = await auth.load_token()
        assert (token, row_id) == ("xoxp-from-env", None)

    @pytest.mark.asyncio
    async def test_revoked_row_is_skipped(self, monkeypatch):
        monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-from-env")
        await _add("xoxp-dead", active=False)
        token, row_id = await auth.load_token()
        assert (token, row_id) == ("xoxp-from-env", None)

    @pytest.mark.asyncio
    async def test_nothing_anywhere_is_empty_not_an_exception(self, monkeypatch):
        """Пустой токен — рабочее состояние: контейнер ждёт, а не падает."""
        monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
        assert await auth.load_token() == ("", None)

    @pytest.mark.asyncio
    async def test_broken_cipher_kills_the_row_and_falls_back(self, monkeypatch):
        """Расшифровать не вышло — строку гасим, иначе поллер бился бы в неё
        каждые десять минут, а дашборд показывал бы «подключено»."""
        monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-from-env")
        await _add(raw="это не шифр")
        token, row_id = await auth.load_token()
        assert (token, row_id) == ("xoxp-from-env", None)
        row = await _row()
        assert row.is_active is False
        assert "не расшифровался" in (row.last_error or "")


@pytest.mark.usefixtures("sqlite_db")
class TestMarks:

    @pytest.mark.asyncio
    async def test_mark_ok_records_success_and_clears_error(self):
        row_id = await _add()
        from vera_shared.db.engine import get_session
        async with get_session() as s:
            r = (await s.execute(select(SlackAuthRow))).scalar_one()
            r.last_error = "старая ошибка"
        await auth.mark_ok(row_id)
        row = await _row()
        assert row.last_ok_at is not None
        assert row.last_error is None

    @pytest.mark.asyncio
    async def test_mark_dead_deactivates_with_a_reason(self):
        row_id = await _add()
        await auth.mark_dead(row_id, "auth.test: token_revoked")
        row = await _row()
        assert row.is_active is False
        assert "token_revoked" in (row.last_error or "")

    @pytest.mark.asyncio
    async def test_marks_on_env_token_are_noops(self):
        """Токен из окружения строки не имеет — гасить нечего и падать не на чем."""
        await auth.mark_ok(None)
        await auth.mark_dead(None, "нечего гасить")


@pytest.mark.usefixtures("sqlite_db")
class TestMissingTable:
    """Деплой привозит код раньше миграции — окно между ними структурное и
    повторится с каждым новым источником. Трейсбек тут ничего не объясняет."""

    @pytest.mark.asyncio
    async def test_absent_table_degrades_to_environment_with_a_clear_line(
            self, monkeypatch, caplog):
        from sqlalchemy.exc import ProgrammingError

        monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-from-env")

        class _Boom:
            async def __aenter__(self):
                raise ProgrammingError("SELECT …", {}, Exception("no such table"))

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(auth, "get_session", lambda: _Boom())
        with caplog.at_level("ERROR"):
            token, row_id = await auth.load_token()

        assert (token, row_id) == ("xoxp-from-env", None)
        assert "миграцию 025" in caplog.text
