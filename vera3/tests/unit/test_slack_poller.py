"""Slack: обход каналов, курсоры и — главное — треды.

Ответы в тредах НЕ приходят в conversations.history, а тред, чьё корневое
сообщение старше курсора, не появится в истории вовсе. Именно этот класс
потери здесь и проверяется: без наблюдения за тредами обсуждения в Slack были
бы невидимы навсегда.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..", "services", "ingestor-slack", "src"))

os.environ.setdefault("SLACK_USER_TOKEN", "xoxp-test")

from ingestor_slack import poller, store  # noqa: E402

ME = "U0ME"

# Треды держатся под наблюдением ограниченный срок (SLACK_THREAD_WATCH_DAYS),
# поэтому метки времени в тестах отсчитываются от «сейчас», а не зашиты числом:
# зашитые протухли бы вместе с окном.
_NOW = datetime.now(tz=timezone.utc).timestamp()


def _ts(offset_s: float) -> str:
    """`ts` Slack, сдвинутый от текущего момента. Отрицательный — в прошлое."""
    return f"{_NOW + offset_s:.6f}"


class _FakeClient:
    """История по каналам и ветки по тредам, с записью параметров вызовов."""

    def __init__(self, history=None, replies=None, users=None):
        self._history = history or {}
        self._replies = replies or {}
        self._users = users or {}
        self.history_calls: list[dict] = []
        self.reply_calls: list[dict] = []

    async def history(self, channel, *, oldest, max_pages):
        self.history_calls.append({"channel": channel, "oldest": oldest})
        return self._history.get(channel, ([], True))

    async def replies(self, channel, thread_ts, *, oldest, max_pages):
        self.reply_calls.append({"channel": channel, "ts": thread_ts, "oldest": oldest})
        return self._replies.get((channel, thread_ts), ([], True))

    async def user_info(self, user_id):
        return self._users.get(user_id, {"real_name": user_id, "profile": {}})


def _msg(ts, text="сообщение", user="UKOL", **over):
    return {"ts": ts, "user": user, "text": text, **over}


async def _conversation(cid="C1", name="general", kind="channel", cursor=None):
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_sources import SlackConversationRow
    async with get_session() as s:
        s.add(SlackConversationRow(conversation_id=cid, name=name, kind=kind,
                                   is_private=False, last_ts=cursor, is_active=True))
    async with get_session() as s:
        return (await s.execute(
            select(SlackConversationRow)
            .where(SlackConversationRow.conversation_id == cid)
        )).scalar_one()


async def _cursor(cid="C1"):
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_sources import SlackConversationRow
    async with get_session() as s:
        return (await s.execute(
            select(SlackConversationRow.last_ts)
            .where(SlackConversationRow.conversation_id == cid)
        )).scalar_one()


async def _event_count():
    from vera_shared.db.engine import get_session
    from vera_shared.db.models import EventRow
    async with get_session() as s:
        return (await s.execute(
            select(func.count()).select_from(EventRow)
            .where(EventRow.source == "slack")
        )).scalar_one()


async def _watched(cid="C1"):
    from vera_shared.db.engine import get_session
    from vera_shared.db.models_sources import SlackThreadRow
    async with get_session() as s:
        return list((await s.execute(
            select(SlackThreadRow).where(SlackThreadRow.conversation_id == cid)
        )).scalars().all())


async def _run(client, row, names=None):
    names = names or poller.Profiles(client)
    return await poller.poll_conversation(client, row, ME, "acme/dima", names)


@pytest.mark.usefixtures("sqlite_db")
class TestHistory:

    @pytest.mark.asyncio
    async def test_cursor_moves_to_newest_message(self):
        row = await _conversation(cursor=_ts(-7200))
        client = _FakeClient(history={"C1": ([_msg(_ts(-100)),
                                             _msg(_ts(-200))], True)})
        assert await _run(client, row) == 2
        assert await _cursor() == _ts(-100)

    @pytest.mark.asyncio
    async def test_incomplete_backlog_keeps_cursor(self):
        row = await _conversation(cid="C2", cursor=_ts(-7200))
        client = _FakeClient(history={"C2": ([_msg(_ts(-200))], False)})
        await _run(client, row)
        # Хвост не разобран — курсор остаётся, иначе середина потеряется молча.
        assert await _cursor("C2") == _ts(-7200)

    @pytest.mark.asyncio
    async def test_repoll_of_same_messages_inserts_nothing(self):
        row = await _conversation(cid="C3", cursor="old")
        msgs = ([_msg(_ts(-200)), _msg(_ts(-100))], True)
        first = await _run(_FakeClient(history={"C3": msgs}), row)
        again = await _run(_FakeClient(history={"C3": msgs}), row)
        assert (first, again) == (2, 0)

    @pytest.mark.asyncio
    async def test_first_run_bootstraps_from_a_window_not_from_zero(self):
        row = await _conversation(cid="C4", cursor=None)
        client = _FakeClient(history={"C4": ([], True)})
        await _run(client, row)
        oldest = float(client.history_calls[0]["oldest"])
        assert oldest > 0

    @pytest.mark.asyncio
    async def test_bot_messages_never_become_events(self):
        row = await _conversation(cid="C5", cursor="old")
        client = _FakeClient(history={"C5": (
            [_msg(_ts(-200), "deploy ok", bot_id="B1"),
             _msg(_ts(-100), "живой текст")], True)})
        assert await _run(client, row) == 1


@pytest.mark.usefixtures("sqlite_db")
class TestThreads:

    @pytest.mark.asyncio
    async def test_thread_parent_from_history_is_taken_under_watch(self):
        row = await _conversation(cid="T1", cursor="old")
        parent = _msg(_ts(-200), "обсудим?",
                      thread_ts=_ts(-200), reply_count=2,
                      latest_reply=_ts(-50))
        client = _FakeClient(history={"T1": ([parent], True)})
        await _run(client, row)
        watched = await _watched("T1")
        assert [t.thread_ts for t in watched] == [_ts(-200)]

    @pytest.mark.asyncio
    async def test_replies_are_ingested_and_parent_not_duplicated(self):
        row = await _conversation(cid="T2", cursor="old")
        parent_ts = _ts(-200)
        parent = _msg(parent_ts, "обсудим?", thread_ts=parent_ts, reply_count=1,
                      latest_reply=_ts(-50))
        client = _FakeClient(
            history={"T2": ([parent], True)},
            replies={("T2", parent_ts): (
                [parent, _msg(_ts(-50), "давай", user="UANN")], True)},
        )
        saved = await _run(client, row)
        # Корневое сообщение — одно событие, ответ — второе. Корень из
        # conversations.replies не должен задваивать событие из истории.
        assert saved == 2
        assert await _event_count() == 2

    @pytest.mark.asyncio
    async def test_reply_to_thread_older_than_cursor_is_still_fetched(self):
        """Ровно та ловушка, ради которой существует slack_threads.

        Корневое сообщение старше курсора — в conversations.history оно не
        придёт. Ответ на него мы обязаны забрать через наблюдаемый тред.
        """
        row = await _conversation(cid="T3", cursor=_ts(-150))
        old_parent_ts = _ts(-4000)
        # Тред уже под наблюдением с прошлого прогона, ответов ещё не видели.
        await store.watch_thread("T3", old_parent_ts, None,
                                 poller.parse_ts(old_parent_ts))
        client = _FakeClient(
            history={"T3": ([], True)},          # история пуста — корень стар
            replies={("T3", old_parent_ts): (
                [_msg(old_parent_ts, "старый вопрос"),
                 _msg(_ts(-10), "новый ответ", user="UANN")], True)},
        )
        saved = await _run(client, row)
        assert saved == 1
        assert client.reply_calls == [
            {"channel": "T3", "ts": old_parent_ts, "oldest": None}]
        # Курсор треда сдвинулся на разобранный ответ.
        assert (await _watched("T3"))[0].last_reply_ts == _ts(-10)

    @pytest.mark.asyncio
    async def test_incomplete_thread_keeps_its_cursor(self):
        row = await _conversation(cid="T4", cursor="old")
        parent_ts = _ts(-4000)
        await store.watch_thread("T4", parent_ts, None, poller.parse_ts(parent_ts))
        client = _FakeClient(
            history={"T4": ([], True)},
            replies={("T4", parent_ts): ([_msg(_ts(-10), "ответ")], False)},
        )
        await _run(client, row)
        assert (await _watched("T4"))[0].last_reply_ts is None

    @pytest.mark.asyncio
    async def test_threads_per_run_is_capped(self, monkeypatch):
        """Следить за каждым тредом каждые пять минут — сотни лишних вызовов.
        Ограничение сдвигает проверку по времени, но не теряет тред."""
        monkeypatch.setattr(poller, "THREADS_PER_RUN", 2)
        row = await _conversation(cid="T5", cursor="old")
        for i in range(5):
            ts = _ts(-3000 - i)
            await store.watch_thread("T5", ts, None, poller.parse_ts(ts))
        client = _FakeClient(history={"T5": ([], True)})
        await _run(client, row)
        assert len(client.reply_calls) == 2


@pytest.mark.usefixtures("sqlite_db")
class TestConversationList:

    @pytest.mark.asyncio
    async def test_left_channel_is_deactivated_not_deleted(self):
        from vera_shared.db.engine import get_session
        from vera_shared.db.models_sources import SlackConversationRow
        await store.upsert_conversations(
            [{"id": "C9", "name": "живой"}, {"id": "C8", "name": "покинут"}], {})
        active = await store.upsert_conversations([{"id": "C9", "name": "живой"}], {})
        assert [r.conversation_id for r in active] == ["C9"]
        async with get_session() as s:
            gone = (await s.execute(
                select(SlackConversationRow)
                .where(SlackConversationRow.conversation_id == "C8")
            )).scalar_one()
        assert gone.is_active is False

    @pytest.mark.asyncio
    async def test_dm_takes_the_peers_name(self):
        """У лички своего имени нет — иначе в дашборде был бы столбец «D1»."""
        rows = await store.upsert_conversations(
            [{"id": "D1", "is_im": True, "user": "UKOL"}], {"UKOL": "Коля Петров"})
        assert (rows[0].name, rows[0].kind) == ("Коля Петров", "im")
        assert rows[0].is_private is True

    @pytest.mark.asyncio
    async def test_mpim_is_private_group(self):
        rows = await store.upsert_conversations(
            [{"id": "G1", "is_mpim": True, "name": "mpdm-a--b--c-1"}], {})
        assert (rows[0].kind, rows[0].is_private) == ("mpim", True)


class TestAuthResilience:
    """Падение без токена = crash-loop под restart:unless-stopped. Так уже
    было с ingestor-instagram, пока сессия неактивна — RestartCount рос без
    предела. Слушать ошибку и ждать обязан цикл, а не контейнер."""

    @pytest.mark.asyncio
    async def test_auth_error_resets_the_session_and_propagates(self, monkeypatch):
        from ingestor_slack import auth as auth_mod
        from ingestor_slack.client import SlackAuthError

        session = poller._Session()

        async def token():
            return "xoxp-live", None

        def boom(_token):
            raise SlackAuthError("token_revoked")

        monkeypatch.setattr(auth_mod, "load_token", token)
        monkeypatch.setattr(poller, "SlackClient", boom)
        with pytest.raises(SlackAuthError):
            await session.connect()
        assert session.client is None
        assert session.names is None

    @pytest.mark.asyncio
    async def test_connect_is_cached_between_runs(self, monkeypatch):
        from ingestor_slack import auth as auth_mod

        session = poller._Session()
        marked: list[int | None] = []

        async def token():
            return "xoxp-live", 7

        async def mark_ok(row_id):
            marked.append(row_id)

        class _C:
            calls = 0

            def __init__(self, _token):
                type(self).calls += 1

            async def whoami(self):
                return {"user_id": ME, "team": "acme", "user": "dima"}

        monkeypatch.setattr(auth_mod, "load_token", token)
        monkeypatch.setattr(auth_mod, "mark_ok", mark_ok)
        monkeypatch.setattr(poller, "SlackClient", _C)
        first, _ = await session.connect()
        second, _ = await session.connect()
        assert first is second and _C.calls == 1
        assert session.account == "acme/dima"
        # Успех записывается один раз, а не на каждый прогон.
        assert marked == [7]

    @pytest.mark.asyncio
    async def test_token_from_dashboard_is_used_over_environment(self, monkeypatch):
        """Подключил через UI — обход обязан пойти новым токеном, а не старым
        из .env, иначе подключение выглядит сделанным и не работает."""
        from ingestor_slack import auth as auth_mod

        monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-from-env")
        session = poller._Session()
        seen: list[str] = []

        async def token():
            return "xoxp-from-dashboard", 3

        async def mark_ok(_row):
            return None

        class _C:
            def __init__(self, token):
                seen.append(token)

            async def whoami(self):
                return {"user_id": ME, "team": "acme", "user": "dima"}

        monkeypatch.setattr(auth_mod, "load_token", token)
        monkeypatch.setattr(auth_mod, "mark_ok", mark_ok)
        monkeypatch.setattr(poller, "SlackClient", _C)
        await session.connect()
        assert seen == ["xoxp-from-dashboard"]
