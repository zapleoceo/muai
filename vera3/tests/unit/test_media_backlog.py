"""vera_shared.media_backlog — живой остаток распознавания для дашборда.

Панель показывала запись трёхчасового крона: файл распознан, а на экране всё
ещё «1 осталось». Тесты держат две вещи: остаток считается по той же политике,
что и очередь, и один запрос в базу не превращается в запрос на каждую вкладку.
"""
from __future__ import annotations

import pytest
from vera_shared import chat_activity, media_backlog


@pytest.fixture(autouse=True)
def _clean_caches():
    chat_activity.forget()
    media_backlog.forget()
    yield
    chat_activity.forget()
    media_backlog.forget()


async def _event(get_session, *, chat_id, chat_kind, media_kind,
                 direction="received", recognition=None, permanent=None, tag=""):
    from vera_shared.db.models import EventRow
    from vera_shared.timeutil import utc_naive_now
    meta = {"chat_id": chat_id, "chat_kind": chat_kind,
            "media_kind": media_kind, "direction": direction}
    if recognition:
        meta["media_recognition"] = recognition
    if permanent is not None:
        meta["media_permanent"] = "true" if permanent else "false"
    async with get_session() as s:
        s.add(EventRow(
            source="telegram",
            source_event_id=f"tg:{chat_id}:{media_kind}:{recognition}:{tag}",
            category="message", content_text="[photo]",
            occurred_at=utc_naive_now(), metadata_=meta, triage_status="done",
        ))


def _wire(mp, sqlite_db):
    mp.setattr(media_backlog, "get_session", sqlite_db)
    mp.setattr(chat_activity, "get_session", sqlite_db)


async def _left(mp, sqlite_db, min_own=5):
    _wire(mp, sqlite_db)
    return await media_backlog.unrecognized_left(min_own)


@pytest.mark.asyncio
async def test_counts_unrecognized_media_the_policy_lets_through(sqlite_db):
    await _event(sqlite_db, chat_id=1, chat_kind="private", media_kind="photo")
    await _event(sqlite_db, chat_id=1, chat_kind="private", media_kind="voice",
                 tag="b")
    with pytest.MonkeyPatch.context() as mp:
        assert await _left(mp, sqlite_db) == 2


@pytest.mark.asyncio
async def test_recognized_media_is_not_left(sqlite_db):
    await _event(sqlite_db, chat_id=1, chat_kind="private", media_kind="photo",
                 recognition="ok_broker")
    with pytest.MonkeyPatch.context() as mp:
        assert await _left(mp, sqlite_db) == 0


@pytest.mark.asyncio
async def test_failed_recognition_is_still_left(sqlite_db):
    await _event(sqlite_db, chat_id=1, chat_kind="private", media_kind="photo",
                 recognition="failed")
    with pytest.MonkeyPatch.context() as mp:
        assert await _left(mp, sqlite_db) == 1


@pytest.mark.asyncio
async def test_permanently_lost_media_is_not_left(sqlite_db):
    """Файла больше нет в Telegram — он не затык, а данность. До 23.09.2026
    такие 6 012 штук держали счётчик вечно ненулевым."""
    await _event(sqlite_db, chat_id=1, chat_kind="private", media_kind="photo",
                 recognition="failed", permanent=True)
    with pytest.MonkeyPatch.context() as mp:
        assert await _left(mp, sqlite_db) == 0


@pytest.mark.asyncio
async def test_channel_photos_are_not_left(sqlite_db):
    await _event(sqlite_db, chat_id=2, chat_kind="channel", media_kind="photo")
    with pytest.MonkeyPatch.context() as mp:
        assert await _left(mp, sqlite_db) == 0


@pytest.mark.asyncio
async def test_video_is_not_left(sqlite_db):
    await _event(sqlite_db, chat_id=1, chat_kind="private", media_kind="video")
    with pytest.MonkeyPatch.context() as mp:
        assert await _left(mp, sqlite_db) == 0


@pytest.mark.asyncio
async def test_group_without_participation_is_not_left(sqlite_db):
    await _event(sqlite_db, chat_id=3, chat_kind="group", media_kind="photo")
    with pytest.MonkeyPatch.context() as mp:
        assert await _left(mp, sqlite_db) == 0


@pytest.mark.asyncio
async def test_group_with_participation_is_left(sqlite_db):
    for i in range(5):
        await _event(sqlite_db, chat_id=3, chat_kind="group", media_kind="voice",
                     direction="sent", recognition="ok_broker", tag=f"own{i}")
    await _event(sqlite_db, chat_id=3, chat_kind="group", media_kind="photo")
    with pytest.MonkeyPatch.context() as mp:
        assert await _left(mp, sqlite_db) == 1


@pytest.mark.asyncio
async def test_repeat_call_does_not_hit_the_database_again(sqlite_db):
    await _event(sqlite_db, chat_id=1, chat_kind="private", media_kind="photo")
    with pytest.MonkeyPatch.context() as mp:
        assert await _left(mp, sqlite_db) == 1
        await _event(sqlite_db, chat_id=1, chat_kind="private",
                     media_kind="photo", tag="b")
        assert await media_backlog.unrecognized_left(5) == 1
        media_backlog.forget()
        assert await media_backlog.unrecognized_left(5) == 2
