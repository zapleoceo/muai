"""vera_shared.chat_activity — участие владельца в чате, данные для политики.

Признак объективный: сколько сообщений владелец сам написал в этом чате. Тесты
идут на живой SQLite, потому что весь смысл функции — в запросе, и мок его бы
не проверил.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from vera_shared import chat_activity

_T0 = datetime(2026, 8, 27, 9, 0)


async def _put(get_session, chat_id, direction, *, source="telegram", n=1):
    from vera_shared.db.models import EventRow
    async with get_session() as s:
        for i in range(n):
            s.add(EventRow(
                source=source,
                source_event_id=f"{source}:{chat_id}:{direction}:{i}:{id(s)}",
                category="message", content_text="привет",
                occurred_at=_T0,
                metadata_={"chat_id": chat_id, "direction": direction},
            ))


@pytest.fixture(autouse=True)
def _clean_cache():
    chat_activity.forget()
    yield
    chat_activity.forget()


class TestOwnMessageCount:
    @pytest.mark.asyncio
    async def test_counts_only_my_messages(self, sqlite_db):
        await _put(sqlite_db, 111, "sent", n=3)
        await _put(sqlite_db, 111, "received", n=40)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(chat_activity, "get_session", sqlite_db)
            assert await chat_activity.own_message_count(111) == 3

    @pytest.mark.asyncio
    async def test_counts_per_chat_not_globally(self, sqlite_db):
        """«Быть Или» — 1792 автора и ни одного своего сообщения; то, что в
        других чатах владелец писал тысячи раз, ей ничего не даёт."""
        await _put(sqlite_db, 222, "sent", n=7)
        await _put(sqlite_db, 333, "received", n=99)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(chat_activity, "get_session", sqlite_db)
            assert await chat_activity.own_message_count(333) == 0
            assert await chat_activity.own_message_count(222) == 7

    @pytest.mark.asyncio
    async def test_other_sources_do_not_count(self, sqlite_db):
        await _put(sqlite_db, 444, "sent", source="slack", n=5)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(chat_activity, "get_session", sqlite_db)
            assert await chat_activity.own_message_count(444) == 0

    @pytest.mark.asyncio
    async def test_int_and_str_ids_are_the_same_chat(self, sqlite_db):
        await _put(sqlite_db, 555, "sent", n=2)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(chat_activity, "get_session", sqlite_db)
            assert await chat_activity.own_message_count(555) == 2
            assert await chat_activity.own_message_count("555") == 2

    @pytest.mark.asyncio
    async def test_no_chat_id(self, sqlite_db):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(chat_activity, "get_session", sqlite_db)
            assert await chat_activity.own_message_count(None) == 0

    @pytest.mark.asyncio
    async def test_db_failure_never_breaks_ingest(self, sqlite_db):
        """Политика не имеет права уронить приём сообщений: ноль — не «ошибка»,
        а «участие не подтверждено», событие всё равно сохранится."""
        def broken():
            raise RuntimeError("БД лежит")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(chat_activity, "get_session", broken)
            assert await chat_activity.own_message_count(666) == 0


class TestCache:
    @pytest.mark.asyncio
    async def test_second_call_does_not_hit_the_db(self, sqlite_db):
        """Решение принимается на КАЖДОМ медиа, а запрос — скан по jsonb."""
        await _put(sqlite_db, 777, "sent", n=4)
        calls = 0

        def counting():
            nonlocal calls
            calls += 1
            return sqlite_db()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(chat_activity, "get_session", counting)
            assert await chat_activity.own_message_count(777) == 4
            assert await chat_activity.own_message_count(777) == 4
        assert calls == 1

    @pytest.mark.asyncio
    async def test_expired_entry_is_recounted(self, sqlite_db):
        """TTL нужен: владелец может начать писать там, где раньше молчал."""
        await _put(sqlite_db, 888, "received", n=3)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(chat_activity, "get_session", sqlite_db)
            assert await chat_activity.own_message_count(888) == 0
            await _put(sqlite_db, 888, "sent", n=6)
            assert await chat_activity.own_message_count(888) == 0   # из кэша
            mp.setattr(chat_activity, "TTL_S", -1.0)
            chat_activity.forget(888)
            assert await chat_activity.own_message_count(888) == 6

    @pytest.mark.asyncio
    async def test_forget_one_chat_keeps_the_rest(self, sqlite_db):
        await _put(sqlite_db, 999, "sent", n=1)
        await _put(sqlite_db, 1000, "sent", n=2)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(chat_activity, "get_session", sqlite_db)
            await chat_activity.own_message_count(999)
            await chat_activity.own_message_count(1000)
            chat_activity.forget(999)
        assert "999" not in chat_activity._cache
        assert "1000" in chat_activity._cache


class TestThreshold:
    @pytest.mark.asyncio
    async def test_threshold_is_a_setting_not_a_constant(self, sqlite_db):
        import vera_shared.control as control_mod
        from vera_shared.control import MEDIA_MIN_OWN_MESSAGES
        from vera_shared.db.models import AppControlRow

        # Строку пишем через ORM, а не set_control: тот вставляет now(),
        # которого в SQLite нет. Проверяем здесь ЧТЕНИЕ настройки.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(control_mod, "get_session", sqlite_db)
            assert await chat_activity.min_own_messages() == \
                chat_activity.DEFAULT_MIN_OWN_MESSAGES
            async with sqlite_db() as s:
                s.add(AppControlRow(key=MEDIA_MIN_OWN_MESSAGES, value="12"))
            assert await chat_activity.min_own_messages() == 12
