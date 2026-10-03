"""Первопричины дублей: чтобы они переставали появляться, а не чистились вечно.

Организация по домену, free-mail → человек, группа→супергруппа, заглушка
tg_user_<id>, двойник «Telegram ↔ рабочая почта» как предложение владельцу.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import (
    EntityAliasRow,
    EntityRow,
    MergeSuggestionRow,
)
from vera_shared.graph import twin_suggest
from vera_shared.graph.chat_link import resolve_migrated_chat
from vera_shared.graph.identity import entity_kind_for_email
from vera_shared.graph.repo import upsert_entity
from vera_shared.graph.sender_entity import sender_entity
from vera_shared.ingest import sync_author_entities


class TestSenderEntity:

    def test_service_address_becomes_organization_keyed_by_domain(self):
        spec = sender_entity("no-reply@mail.anthropic.com", "Anthropic")
        assert spec["type"] == "organization"
        assert spec["known_as"] == [("domain", "anthropic.com")]
        assert spec["identifier"] == "no-reply@mail.anthropic.com"

    def test_generic_display_name_is_replaced_by_domain_brand(self):
        assert sender_entity("noreply@tuneprotect.com", "noreply")["name"] == "Tuneprotect"

    def test_company_name_is_kept(self):
        assert sender_entity("upwork@email.upwork.com", "Upwork")["name"] == "Upwork"

    def test_person_has_no_domain_link(self):
        spec = sender_entity("ivan@itstep.org", "Ivan Petrenko")
        assert spec["type"] == "person" and "known_as" not in spec

    def test_free_mail_is_always_a_person_even_with_service_word(self):
        assert entity_kind_for_email("support@gmail.com") == "person"
        assert sender_entity("support@gmail.com", "support")["type"] == "person"

    def test_shared_platform_stays_per_address(self):
        spec = sender_entity("invoice+statements@stripe.com", "Acme Inc")
        assert spec["type"] == "organization" and "known_as" not in spec

    def test_newsletter_subdomain_is_service(self):
        assert entity_kind_for_email("trip.com@newsletter.trip.com") == "organization"


@pytest.mark.usefixtures("sqlite_db")
class TestOneOrgPerDomain:

    @pytest.mark.asyncio
    async def test_many_addresses_of_one_domain_make_one_entity(self):
        specs = [{"addr": a} for a in ("no-reply@mail.anthropic.com",
                                       "billing@anthropic.com", "team@mail.anthropic.com")]
        await sync_author_entities(
            specs, source="gmail",
            author_of=lambda sp: sender_entity(sp["addr"], "Anthropic"))
        async with get_session() as s:
            orgs = (await s.execute(select(EntityRow).where(
                EntityRow.type == "organization"))).scalars().all()
            aliases = (await s.execute(select(EntityAliasRow))).scalars().all()
        assert len(orgs) == 1
        assert {(a.source, a.identifier) for a in aliases} == {
            ("gmail", "no-reply@mail.anthropic.com"), ("gmail", "billing@anthropic.com"),
            ("gmail", "team@mail.anthropic.com"), ("domain", "anthropic.com")}

    @pytest.mark.asyncio
    async def test_new_address_joins_existing_domain_entity(self):
        async with get_session() as s:
            ent = EntityRow(type="organization", name="IT STEP", attributes={})
            s.add(ent)
            await s.flush()
            s.add(EntityAliasRow(entity_id=ent.id, source="domain", identifier="itstep.org"))
            keep_id = ent.id
        await sync_author_entities(
            [{"addr": "crm@itstep.org"}], source="gmail",
            author_of=lambda sp: sender_entity(sp["addr"], "IT Step"))
        async with get_session() as s:
            assert len((await s.execute(select(EntityRow))).scalars().all()) == 1
            alias = (await s.execute(select(EntityAliasRow).where(
                EntityAliasRow.identifier == "crm@itstep.org"))).scalar_one()
        assert alias.entity_id == keep_id


async def _person(name, aliases, attrs=None):
    async with get_session() as s:
        ent = EntityRow(type="person", name=name, attributes=attrs or {})
        s.add(ent)
        await s.flush()
        for source, ident in aliases:
            s.add(EntityAliasRow(entity_id=ent.id, source=source, identifier=ident))
        return ent.id


async def _suggestions():
    async with get_session() as s:
        return (await s.execute(select(MergeSuggestionRow))).scalars().all()


@pytest.mark.usefixtures("sqlite_db")
class TestPersonTwin:

    @pytest.fixture(autouse=True)
    def _fresh(self):
        twin_suggest.forget()
        yield
        twin_suggest.forget()

    @pytest.mark.asyncio
    async def test_telegram_and_work_person_become_a_suggestion_not_a_merge(self):
        tg = await _person("Виктор Гавриленко", [("telegram", "user:11")], {"tg_id": 11})
        work = await _person("Viktor Gavrylenko", [("gmail", "vg@itstep.org")])
        assert await twin_suggest.suggest_person_twin(work, "Viktor Gavrylenko") == tg
        [row] = await _suggestions()
        assert (row.entity_a, row.entity_b) == (min(tg, work), max(tg, work))
        assert row.status == "pending"
        async with get_session() as s:
            assert len((await s.execute(select(EntityRow))).scalars().all()) == 2

    @pytest.mark.asyncio
    async def test_namesakes_are_not_suggested(self):
        await _person("Katerina Kravchenko", [("telegram", "user:1")])
        await _person("Катерина Кравченко", [("telegram", "user:2")])
        work = await _person("Kateryna Kravchenko", [("gmail", "kk@itstep.org")])
        assert await twin_suggest.suggest_person_twin(work, "Kateryna Kravchenko") is None
        assert await _suggestions() == []

    @pytest.mark.asyncio
    async def test_checked_once_per_entity(self):
        await _person("Ivan Petrenko", [("telegram", "user:1")])
        work = await _person("Іван Петренко", [("gmail", "ip@itstep.org")])
        assert await twin_suggest.suggest_person_twin(work, "Іван Петренко") is not None
        assert await twin_suggest.suggest_person_twin(work, "Іван Петренко") is None
        assert len(await _suggestions()) == 1

    @pytest.mark.asyncio
    async def test_chat_twin_needs_exactly_one_group_with_the_name(self):
        async with get_session() as s:
            group = EntityRow(type="group", name="Team Chat", attributes={})
            sup = EntityRow(type="supergroup", name="team chat", attributes={})
            s.add_all([group, sup])
            await s.flush()
            group_id, sup_id = group.id, sup.id
        assert await twin_suggest.suggest_chat_twin(sup_id, "team chat") == group_id
        assert len(await _suggestions()) == 1


@pytest.mark.usefixtures("sqlite_db")
class TestMigratedChat:

    @pytest.mark.asyncio
    async def test_legacy_group_is_promoted_in_place(self):
        old = await upsert_entity(type="group", name="Crew", source="telegram",
                                  identifier="chat:100", attributes={"tg_id": 100})
        got = await resolve_migrated_chat(100, 200, "Crew")
        assert got == old
        async with get_session() as s:
            ent = await s.get(EntityRow, old)
            idents = {a.identifier for a in (await s.execute(select(EntityAliasRow))).scalars()}
        assert ent.type == "supergroup" and ent.attributes["migrated_from"] == 100
        assert idents == {"chat:100", "chat:200"}

    @pytest.mark.asyncio
    async def test_later_supergroup_messages_land_on_the_same_entity(self):
        old = await upsert_entity(type="group", name="Crew", source="telegram",
                                  identifier="chat:100")
        await resolve_migrated_chat(100, 200, "Crew")
        again = await upsert_entity(type="supergroup", name="Crew", source="telegram",
                                    identifier="chat:200")
        assert again == old

    @pytest.mark.asyncio
    async def test_supergroup_first_then_legacy_chat_gets_alias(self):
        new = await upsert_entity(type="supergroup", name="Crew", source="telegram",
                                  identifier="chat:200")
        assert await resolve_migrated_chat(100, 200, "Crew") == new
        assert await resolve_migrated_chat(100, 200, "Crew") == new  # идемпотентно

    @pytest.mark.asyncio
    async def test_nothing_known_creates_one_entity_with_both_aliases(self):
        eid = await resolve_migrated_chat(100, 200, "Crew")
        async with get_session() as s:
            ents = (await s.execute(select(EntityRow))).scalars().all()
            als = (await s.execute(select(EntityAliasRow))).scalars().all()
        assert [e.id for e in ents] == [eid] and len(als) == 2

    @pytest.mark.asyncio
    async def test_both_already_separate_are_proposed_not_merged(self):
        old = await upsert_entity(type="group", name="Crew", source="telegram",
                                  identifier="chat:100")
        new = await upsert_entity(type="supergroup", name="Crew", source="telegram",
                                  identifier="chat:200")
        assert await resolve_migrated_chat(100, 200, "Crew") == new
        [row] = await _suggestions()
        assert {row.entity_a, row.entity_b} == {old, new}


def _tg_chat(kind, chat_id, title="Room", **extra):
    cls = type(kind, (), {})
    return cls_with(cls, id=chat_id, title=title, **extra)


def cls_with(cls, **attrs):
    obj = cls()
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


def _user(user_id, first="", username=None):
    return cls_with(type("User", (), {}), id=user_id, first_name=first, last_name="",
                    username=username, bot=False)


@pytest.mark.usefixtures("sqlite_db")
class TestTelegramEntitySync:

    @pytest.fixture(autouse=True)
    def _fresh(self):
        twin_suggest.forget()
        yield
        twin_suggest.forget()

    @pytest.mark.asyncio
    async def test_anonymous_sender_equal_to_group_id_makes_no_placeholder(self):
        from ingestor_telegram.entity_sync import sync_message_entities
        chat = _tg_chat("Chat", 4997502244, "Children", migrated_to=None, megagroup=False)
        await sync_message_entities(chat, _user(4997502244))
        async with get_session() as s:
            ents = (await s.execute(select(EntityRow))).scalars().all()
        assert [(e.type, e.name) for e in ents] == [("group", "Children")]

    @pytest.mark.asyncio
    async def test_nameless_sender_that_is_a_known_chat_is_skipped(self):
        from ingestor_telegram.entity_sync import sync_message_entities
        await upsert_entity(type="channel", name="Some Channel", source="telegram",
                            identifier="chat:555", attributes={"tg_id": 555})
        other = _tg_chat("Chat", 999, "Other", migrated_to=None, megagroup=False)
        await sync_message_entities(other, _user(555))
        async with get_session() as s:
            names = {e.name for e in (await s.execute(select(EntityRow))).scalars()}
        assert "tg_user_555" not in names

    @pytest.mark.asyncio
    async def test_private_chat_partner_still_becomes_a_person(self):
        from ingestor_telegram.entity_sync import sync_message_entities
        private = cls_with(type("User", (), {}), id=42, first_name="Ann", last_name="",
                           username=None, bot=False)
        await sync_message_entities(private, _user(42, "Ann"))
        async with get_session() as s:
            ents = (await s.execute(select(EntityRow))).scalars().all()
        assert [(e.type, e.name) for e in ents] == [("person", "Ann")]

    @pytest.mark.asyncio
    async def test_legacy_chat_with_migrated_to_resolves_to_supergroup(self):
        from ingestor_telegram.entity_sync import sync_message_entities
        legacy = _tg_chat("Chat", 100, "Crew", megagroup=False,
                          migrated_to=SimpleNamespace(channel_id=200))
        await sync_message_entities(legacy, _user(1, "Ann"))
        supergroup = _tg_chat("Channel", 200, "Crew", megagroup=True, migrated_to=None)
        await sync_message_entities(supergroup, _user(1, "Ann"))
        async with get_session() as s:
            chats = (await s.execute(select(EntityRow).where(
                EntityRow.type != "person"))).scalars().all()
        assert [(c.type, c.name) for c in chats] == [("supergroup", "Crew")]
