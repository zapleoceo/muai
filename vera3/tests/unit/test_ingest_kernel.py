"""vera_shared.ingest — общее ядро ингестора.

Здесь проверяется именно то, что раньше было скопировано по ингесторам и в
копиях расходилось: атомарность дедупа, дедуп авторов в пределах прогона,
живучесть цикла без ключа и таблица авторства (её отсутствие приписывало
новый источник владельцу).
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import func, select
from vera_shared.db.models import EventRow
from vera_shared.ingest import (
    OWNER,
    insert_events,
    poll_forever,
    resolve_author,
    sync_author_entities,
)


def _spec(source_event_id: str, **over) -> dict:
    return {
        "source": "slack",
        "source_event_id": source_event_id,
        "occurred_at": datetime(2026, 8, 26, 10, 0),
        "category": "channel",
        "content_text": "Author: Я [self]\nтекст",
        **over,
    }


@pytest.mark.usefixtures("sqlite_db")
class TestInsertEvents:

    async def _count(self) -> int:
        from vera_shared.db.engine import get_session
        async with get_session() as s:
            return (await s.execute(select(func.count()).select_from(EventRow))).scalar_one()

    @pytest.mark.asyncio
    async def test_inserts_and_returns_event_ids(self):
        fresh = await insert_events([_spec("C1:1.1"), _spec("C1:1.2")])
        assert len(fresh) == 2
        assert all(isinstance(sp["event_id"], int) for sp in fresh)
        assert await self._count() == 2

    @pytest.mark.asyncio
    async def test_repeat_is_deduped_not_duplicated(self):
        await insert_events([_spec("C2:2.1")])
        again = await insert_events([_spec("C2:2.1")])
        assert again == []
        assert await self._count() == 1

    @pytest.mark.asyncio
    async def test_same_id_from_another_source_is_a_different_event(self):
        """Дедуп по паре (source, source_event_id), а не по одному id."""
        await insert_events([_spec("shared-id", source="slack")])
        other = await insert_events([_spec("shared-id", source="trello")])
        assert len(other) == 1
        assert await self._count() == 2

    @pytest.mark.asyncio
    async def test_spec_without_required_field_is_dropped_not_raised(self):
        """Кривая спецификация не должна ронять весь прогон источника."""
        fresh = await insert_events([
            _spec("C3:3.1"),
            {"source": "slack", "occurred_at": datetime(2026, 8, 26)},  # нет id
            _spec("", source="slack"),                                  # пустой id
        ])
        assert [sp["source_event_id"] for sp in fresh] == ["C3:3.1"]
        assert await self._count() == 1

    @pytest.mark.asyncio
    async def test_empty_input(self):
        assert await insert_events([]) == []


@pytest.mark.usefixtures("sqlite_db")
class TestSyncAuthorEntities:

    @pytest.mark.asyncio
    async def test_one_upsert_per_identifier_per_run(self):
        calls: list[dict] = []

        async def fake_upsert(**kwargs):
            calls.append(kwargs)
            return len(calls)

        import vera_shared.ingest.authors as authors_mod
        original = authors_mod.upsert_entity
        authors_mod.upsert_entity = fake_upsert
        try:
            touched = await sync_author_entities(
                [{"u": "u1"}, {"u": "u1"}, {"u": "u2"}, {"u": None}],
                source="slack",
                author_of=lambda sp: ({"identifier": sp["u"]} if sp["u"] else None),
            )
        finally:
            authors_mod.upsert_entity = original

        assert touched == 2
        assert [c["identifier"] for c in calls] == ["u1", "u2"]
        assert {c["type"] for c in calls} == {"person"}
        assert {c["source"] for c in calls} == {"slack"}

    @pytest.mark.asyncio
    async def test_extractor_can_override_type_and_attributes(self):
        """gmail заводит служебные ящики организациями, а не людьми."""
        calls: list[dict] = []

        async def fake_upsert(**kwargs):
            calls.append(kwargs)

        import vera_shared.ingest.authors as authors_mod
        original = authors_mod.upsert_entity
        authors_mod.upsert_entity = fake_upsert
        try:
            await sync_author_entities(
                [{}], source="gmail",
                author_of=lambda _: {"identifier": "no-reply@bank.com",
                                     "type": "organization",
                                     "attributes": {"email": "no-reply@bank.com"}},
            )
        finally:
            authors_mod.upsert_entity = original

        assert calls[0]["type"] == "organization"
        assert calls[0]["attributes"] == {"email": "no-reply@bank.com"}

    @pytest.mark.asyncio
    async def test_graph_failure_does_not_break_ingestion(self):
        async def boom(**_kwargs):
            raise RuntimeError("граф недоступен")

        import vera_shared.ingest.authors as authors_mod
        original = authors_mod.upsert_entity
        authors_mod.upsert_entity = boom
        try:
            touched = await sync_author_entities(
                [{}], source="slack", author_of=lambda _: {"identifier": "u9"})
        finally:
            authors_mod.upsert_entity = original
        assert touched == 1


class _Auth(Exception):
    pass


class TestPollForever:

    @pytest.mark.asyncio
    async def test_auth_error_waits_and_retries_instead_of_crashing(self):
        """restart:unless-stopped + падение = crash-loop. Так уже было
        с ingestor-instagram, пока сессия неактивна."""
        attempts = 0
        slept: list[float] = []

        async def poll_once():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise _Auth("нет токена")
            raise asyncio.CancelledError

        async def fake_sleep(seconds):
            slept.append(seconds)

        original = asyncio.sleep
        asyncio.sleep = fake_sleep
        try:
            with pytest.raises(asyncio.CancelledError):
                await poll_forever(name="t", poll_once=poll_once, interval_s=300,
                                   auth_error=_Auth, auth_retry_s=600)
        finally:
            asyncio.sleep = original

        assert attempts == 3
        # Обе неудачи — долгая пауза на ожидание ключа, без обычного интервала.
        assert slept == [600, 600]

    @pytest.mark.asyncio
    async def test_generic_error_is_logged_and_loop_continues(self):
        attempts = 0
        slept: list[float] = []

        async def poll_once():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("сбой прогона")
            raise asyncio.CancelledError

        async def fake_sleep(seconds):
            slept.append(seconds)

        original = asyncio.sleep
        asyncio.sleep = fake_sleep
        try:
            with pytest.raises(asyncio.CancelledError):
                await poll_forever(name="t", poll_once=poll_once, interval_s=300,
                                   auth_error=_Auth)
        finally:
            asyncio.sleep = original

        assert attempts == 2
        assert slept == [300]


class TestResolveAuthor:
    """Раньше это была цепочка `if source ==` с `return владелец` в конце:
    источник без ветки приписывался Диме целиком, тихо."""

    def test_incoming_telegram_resolves_sender_alias(self):
        assert resolve_author("telegram", {"sender_id": 42}) == ("telegram", "user:42")

    def test_outgoing_is_owner(self):
        assert resolve_author("telegram", {"direction": "sent"}) is OWNER

    def test_incoming_gmail_resolves_from_address(self):
        author = resolve_author("gmail", {"from": "Petr <PETR@Example.COM>"})
        assert author == ("gmail", "petr@example.com")

    def test_incoming_slack_resolves_user_alias(self):
        assert resolve_author("slack", {"sender_id": "U123",
                                        "author_role": "counterparty"}) == \
            ("slack", "user:U123")

    def test_slack_self_is_owner(self):
        assert resolve_author("slack", {"author_role": "self"}) is OWNER

    def test_incoming_trello_resolves_username(self):
        assert resolve_author("trello", {"author_role": "counterparty",
                                         "author_username": "kolya"}) == \
            ("trello", "kolya")

    def test_own_sources_are_owner(self):
        for source in ("vera_chat", "vera_memory", "perplexity", "voice", "claude"):
            assert resolve_author(source, {}) is OWNER

    @pytest.mark.parametrize("source,meta", [
        ("telegram", {}),
        ("gmail", {"from": ""}),
        ("instagram", {}),
        ("slack", {"author_role": "counterparty"}),
        ("trello", {"author_role": "counterparty"}),
    ])
    def test_unresolvable_counterparty_is_none_never_owner(self, source, meta):
        """Пустая связь лучше связи, повешенной на владельца."""
        assert resolve_author(source, meta) is None


@pytest.mark.usefixtures("sqlite_db")
class TestLinkedEntities:
    """Один человек в трёх каналах должен быть ОДНОЙ сущностью.

    До 2026-08-26 upsert_entity заводил новую запись, если своего алиаса ещё
    нет, поэтому на живых данных ни одна из 25 slack-сущностей не была связана
    с gmail или telegram, а Yevhenii Pavlenko существовал тремя сразу.
    """

    async def _aliases(self, entity_id: int) -> set[tuple[str, str]]:
        from vera_shared.db.engine import get_session
        from vera_shared.db.models_graph import EntityAliasRow
        async with get_session() as s:
            rows = (await s.execute(
                select(EntityAliasRow).where(EntityAliasRow.entity_id == entity_id)
            )).scalars().all()
        return {(r.source, r.identifier) for r in rows}

    @pytest.mark.asyncio
    async def test_links_to_an_existing_person_by_known_alias(self):
        from vera_shared.graph.repo import upsert_entity, upsert_entity_linked

        gmail_id = await upsert_entity(
            type="person", name="Yevhenii Pavlenko",
            source="gmail", identifier="yevhenii@sintegrum.com")

        slack_id, how = await upsert_entity_linked(
            type="person", name="Yevhenii Pavlenko",
            source="slack", identifier="user:U123",
            known_as=[("gmail", "yevhenii@sintegrum.com")])

        assert (slack_id, how) == (gmail_id, "linked")
        assert await self._aliases(gmail_id) == {
            ("gmail", "yevhenii@sintegrum.com"), ("slack", "user:U123")}

    @pytest.mark.asyncio
    async def test_creates_one_person_carrying_both_aliases_when_nobody_known(self):
        """Email из профиля Slack заводится алиасом gmail сразу: письмо от этого
        человека прилетит на существующую сущность, без участия LLM."""
        from vera_shared.graph.repo import upsert_entity_linked

        entity_id, how = await upsert_entity_linked(
            type="person", name="Igor Nerozya",
            source="slack", identifier="user:U777",
            known_as=[("gmail", "igor@sintegrum.com")])

        assert how == "new"
        assert await self._aliases(entity_id) == {
            ("slack", "user:U777"), ("gmail", "igor@sintegrum.com")}

    @pytest.mark.asyncio
    async def test_own_alias_wins_and_does_not_relink(self):
        from vera_shared.graph.repo import upsert_entity, upsert_entity_linked

        slack_id = await upsert_entity(
            type="person", name="Kolya", source="slack", identifier="user:U9")
        other_id = await upsert_entity(
            type="person", name="Кто-то ещё", source="gmail", identifier="k@x.com")

        got, how = await upsert_entity_linked(
            type="person", name="Kolya", source="slack", identifier="user:U9",
            known_as=[("gmail", "k@x.com")])

        assert (got, how) == (slack_id, "own")
        assert got != other_id

    @pytest.mark.asyncio
    async def test_repeat_is_idempotent(self):
        from vera_shared.graph.repo import upsert_entity_linked

        first, _ = await upsert_entity_linked(
            type="person", name="Ann", source="slack", identifier="user:U1",
            known_as=[("gmail", "ann@x.com")])
        second, how = await upsert_entity_linked(
            type="person", name="Ann", source="slack", identifier="user:U1",
            known_as=[("gmail", "ann@x.com")])
        assert (second, how) == (first, "own")
        assert len(await self._aliases(first)) == 2

    @pytest.mark.asyncio
    async def test_existing_attributes_are_not_overwritten(self):
        """Профиль Slack — ещё один свидетель, а не истина в последней
        инстанции: телефон, уже известный из другого канала, он не перетирает."""
        from vera_shared.db.engine import get_session
        from vera_shared.db.models_graph import EntityRow
        from vera_shared.graph.repo import upsert_entity, upsert_entity_linked

        gmail_id = await upsert_entity(
            type="person", name="Petr", source="gmail", identifier="p@x.com",
            attributes={"phone": "+380 111", "email": "p@x.com"})
        await upsert_entity_linked(
            type="person", name="Petr", source="slack", identifier="user:UP",
            known_as=[("gmail", "p@x.com")],
            attributes={"phone": "+380 999", "title": "CTO"})

        async with get_session() as s:
            attrs = (await s.execute(
                select(EntityRow.attributes).where(EntityRow.id == gmail_id)
            )).scalar_one()
        assert attrs["phone"] == "+380 111"     # прежнее не перетёрто
        assert attrs["title"] == "CTO"          # новое добавлено

    @pytest.mark.asyncio
    async def test_empty_known_as_behaves_like_a_plain_upsert(self):
        from vera_shared.graph.repo import upsert_entity_linked

        entity_id, how = await upsert_entity_linked(
            type="person", name="Solo", source="slack", identifier="user:US",
            known_as=[])
        assert how == "new"
        assert await self._aliases(entity_id) == {("slack", "user:US")}
