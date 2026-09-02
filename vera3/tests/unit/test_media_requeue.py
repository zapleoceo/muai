"""scripts/media_requeue.py — одна политика на уборку, пересмотр и доливку очереди.

Раньше отбор жил условиями SQL в кроне и был копией питоновского денилиста
названий чатов. Тесты держат главное: в очередь не попадает то, что политика
не пропускает, а пропущенное «нет участия» возвращается, когда участие
появилось.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
from vera_shared import chat_activity

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "media_requeue.py"
_T0 = datetime(2026, 8, 27, 9, 0)


def _load():
    spec = importlib.util.spec_from_file_location("media_requeue", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def requeue():
    return _load()


@pytest.fixture(autouse=True)
def _clean_cache():
    chat_activity.forget()
    yield
    chat_activity.forget()


async def _event(get_session, *, chat_id, chat_kind, media_kind, status,
                 direction="received", recognition=None, skip_reason=None,
                 error=None, permanent=None, tag=""):
    from vera_shared.db.models import EventRow
    meta = {"chat_id": chat_id, "chat_kind": chat_kind,
            "media_kind": media_kind, "direction": direction}
    if recognition:
        meta["media_recognition"] = recognition
    if skip_reason:
        meta["media_skip_reason"] = skip_reason
    if permanent is not None:
        meta["media_permanent"] = "true" if permanent else "false"
    async with get_session() as s:
        row = EventRow(
            source="telegram",
            source_event_id=f"tg:{chat_id}:{media_kind}:{status}:{tag}",
            category="message", content_text="[photo]", occurred_at=_T0,
            metadata_=meta, triage_status=status, triage_error=error,
        )
        s.add(row)


async def _status(get_session, source_event_id):
    from sqlalchemy import select
    from vera_shared.db.models import EventRow
    async with get_session() as s:
        row = (await s.execute(
            select(EventRow).where(EventRow.source_event_id == source_event_id)
        )).scalar_one()
        return row.triage_status, (row.metadata_ or {}).get("media_skip_reason")


def _wire(mp, requeue, sqlite_db):
    mp.setattr(requeue, "get_session", sqlite_db)
    mp.setattr(chat_activity, "get_session", sqlite_db)


class TestSweep:
    @pytest.mark.asyncio
    async def test_drops_channel_photos_from_the_queue(self, sqlite_db, requeue):
        await _event(sqlite_db, chat_id=1, chat_kind="channel",
                     media_kind="photo", status="media_pending")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            dropped = await requeue.sweep(min_own=5, dry_run=False)
        assert dropped == {"channel": 1}
        assert await _status(sqlite_db, "tg:1:photo:media_pending:") == \
            ("pending", "channel")

    @pytest.mark.asyncio
    async def test_drops_groups_without_participation(self, sqlite_db, requeue):
        """«Быть Или»: 1792 автора, ни одного своего сообщения."""
        await _event(sqlite_db, chat_id=2, chat_kind="group",
                     media_kind="photo", status="media_pending")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            dropped = await requeue.sweep(min_own=5, dry_run=False)
        assert dropped == {"no_participation": 1}

    @pytest.mark.asyncio
    async def test_keeps_real_correspondence(self, sqlite_db, requeue):
        await _event(sqlite_db, chat_id=3, chat_kind="group",
                     media_kind="photo", status="media_pending")
        for i in range(6):
            await _event(sqlite_db, chat_id=3, chat_kind="group", media_kind=None,
                         status="done", direction="sent", tag=str(i))
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            dropped = await requeue.sweep(min_own=5, dry_run=False)
        assert dropped == {}
        assert (await _status(sqlite_db, "tg:3:photo:media_pending:"))[0] == \
            "media_pending"

    @pytest.mark.asyncio
    async def test_voice_survives_anywhere(self, sqlite_db, requeue):
        await _event(sqlite_db, chat_id=4, chat_kind="channel",
                     media_kind="voice", status="media_pending")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            assert await requeue.sweep(min_own=5, dry_run=False) == {}

    @pytest.mark.asyncio
    async def test_dry_run_changes_nothing(self, sqlite_db, requeue):
        await _event(sqlite_db, chat_id=5, chat_kind="channel",
                     media_kind="photo", status="media_pending")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            dropped = await requeue.sweep(min_own=5, dry_run=True)
        assert dropped == {"channel": 1}
        assert (await _status(sqlite_db, "tg:5:photo:media_pending:"))[0] == \
            "media_pending"


class TestRevisit:
    @pytest.mark.asyncio
    async def test_participation_appeared_photo_comes_back(self, sqlite_db, requeue):
        """Решение принимается по данным, а данные меняются."""
        await _event(sqlite_db, chat_id=6, chat_kind="group", media_kind="photo",
                     status="done", skip_reason="no_participation")
        for i in range(7):
            await _event(sqlite_db, chat_id=6, chat_kind="group", media_kind=None,
                         status="done", direction="sent", tag=str(i))
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            assert await requeue.revisit(min_own=5, dry_run=False) == 1
        status, reason = await _status(sqlite_db, "tg:6:photo:done:")
        assert status == "media_pending" and reason is None

    @pytest.mark.asyncio
    async def test_still_silent_stays_skipped(self, sqlite_db, requeue):
        await _event(sqlite_db, chat_id=7, chat_kind="group", media_kind="photo",
                     status="done", skip_reason="no_participation")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            assert await requeue.revisit(min_own=5, dry_run=False) == 0

    @pytest.mark.asyncio
    async def test_channel_skips_are_never_revisited(self, sqlite_db, requeue):
        """Канал каналом и останется — пересматривать нечего."""
        await _event(sqlite_db, chat_id=8, chat_kind="channel", media_kind="photo",
                     status="done", skip_reason="channel")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            assert await requeue.revisit(min_own=5, dry_run=False) == 0


class TestTopUp:
    @pytest.mark.asyncio
    async def test_only_policy_approved_failures_come_back(self, sqlite_db, requeue):
        # провал в канале — доливать нельзя, политика его больше не пропускает
        await _event(sqlite_db, chat_id=9, chat_kind="channel", media_kind="photo",
                     status="done", recognition="failed")
        # провал в личке — доливаем
        await _event(sqlite_db, chat_id=10, chat_kind="private", media_kind="photo",
                     status="done", recognition="failed")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            was, added, rejected = await requeue.top_up(min_own=5, dry_run=False)
        assert (was, added, rejected) == (0, 1, 1)
        assert (await _status(sqlite_db, "tg:10:photo:done:"))[0] == "media_pending"
        assert (await _status(sqlite_db, "tg:9:photo:done:"))[0] == "done"

    @pytest.mark.asyncio
    async def test_permanent_failures_are_not_retried(self, sqlite_db, requeue):
        await _event(sqlite_db, chat_id=11, chat_kind="private", media_kind="photo",
                     status="done", recognition="failed", permanent=True,
                     error="Could not find the input entity for PeerUser")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            _was, added, _rejected = await requeue.top_up(min_own=5, dry_run=False)
        assert added == 0

    @pytest.mark.asyncio
    async def test_permanence_survives_triage_wiping_triage_error(self, sqlite_db, requeue):
        """Ровно баг 02.09: метка жила в triage_error, а триаж на успехе
        ставит его в NULL. К моменту доливки признака уже не было, и 468
        событий с несуществующими файлами возвращались в очередь каждые три
        часа — вечно. Признак обязан лежать там, где триаж не пишет."""
        await _event(sqlite_db, chat_id=14, chat_kind="private", media_kind="photo",
                     status="done", recognition="failed", permanent=True,
                     error=None)          # триаж уже прошёл и стёр ошибку
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            _was, added, _rejected = await requeue.top_up(min_own=5, dry_run=False)
        assert added == 0

    @pytest.mark.asyncio
    async def test_failure_without_the_flag_gets_one_more_pass(self, sqlite_db, requeue):
        """События, деградировавшие ДО правки, поля не имеют — их берём ещё
        раз, и на этом проходе media-worker пометит их сам."""
        await _event(sqlite_db, chat_id=15, chat_kind="private", media_kind="photo",
                     status="done", recognition="failed")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            _was, added, _rejected = await requeue.top_up(min_own=5, dry_run=False)
        assert added == 1

    @pytest.mark.asyncio
    async def test_full_queue_is_left_alone(self, sqlite_db, requeue):
        for i in range(3):
            await _event(sqlite_db, chat_id=12, chat_kind="private",
                         media_kind="photo", status="media_pending", tag=str(i))
        await _event(sqlite_db, chat_id=13, chat_kind="private", media_kind="photo",
                     status="done", recognition="failed")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            mp.setattr(requeue, "TARGET", 2)
            was, added, _rejected = await requeue.top_up(min_own=5, dry_run=False)
        assert (was, added) == (3, 0)


class TestMeasure:
    """Настоящий остаток считает доливка, а не дашборд: политика «какие чаты
    вообще распознаём» живёт здесь. До 02.09 сайт показывал размер рабочего
    окна (442) и называл его очередью на триаж."""

    @pytest.mark.asyncio
    async def test_counts_only_what_policy_lets_through(self, sqlite_db, requeue):
        await _event(sqlite_db, chat_id=20, chat_kind="private", media_kind="photo",
                     status="done", recognition="failed", tag="a")
        await _event(sqlite_db, chat_id=20, chat_kind="private", media_kind="photo",
                     status="done", recognition="ok", tag="b")
        # канал политика не пропускает — ни в общий объём, ни в остаток
        await _event(sqlite_db, chat_id=21, chat_kind="channel", media_kind="photo",
                     status="done", recognition="failed")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            total, left = await requeue.measure(min_own=5, dry_run=True)
        assert (total, left) == (2, 1)

    @pytest.mark.asyncio
    async def test_never_recognised_counts_as_left(self, sqlite_db, requeue):
        await _event(sqlite_db, chat_id=22, chat_kind="private", media_kind="photo",
                     status="media_pending")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            total, left = await requeue.measure(min_own=5, dry_run=True)
        assert (total, left) == (1, 1)

    @pytest.mark.asyncio
    async def test_writes_both_numbers_for_the_dashboard(self, sqlite_db, requeue):
        # set_control пишет raw-SQL с now() (Postgres) — даём SQLite аналог
        from datetime import datetime, timezone

        import vera_shared.db.engine as engine_mod
        from sqlalchemy import event
        from vera_shared.control import get_control

        @event.listens_for(engine_mod._engine.sync_engine, "connect")
        def _register_now(dbapi_conn, _rec):
            dbapi_conn.create_function(
                "now", 0, lambda: datetime.now(timezone.utc).isoformat())

        await engine_mod._engine.dispose()   # пул создан до регистрации

        await _event(sqlite_db, chat_id=23, chat_kind="private", media_kind="photo",
                     status="done", recognition="failed")
        with pytest.MonkeyPatch.context() as mp:
            _wire(mp, requeue, sqlite_db)
            await requeue.measure(min_own=5, dry_run=False)
        assert await get_control(requeue.BACKLOG_TOTAL_KEY, "") == "1"
        assert await get_control(requeue.BACKLOG_LEFT_KEY, "") == "1"
