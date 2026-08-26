"""Дубли по рабочему email — детерминированные, и досье по всем каналам.

Почему это вообще есть: `upsert_entity` заводил новую сущность, если своего
алиаса ещё нет, поэтому один человек множился по записи на канал. Замер
2026-08-26: Igor Nerozya, Olga Kryachko, Ruslan Kovtiukh — по две сущности
(gmail + slack) с буквально одинаковым адресом `*@itstep.org`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from vera_shared.db.models_graph import EntityAliasRow, EntityRow
from vera_shared.graph import collisions, dossiers


async def _person(name: str, *, email_attr: str | None = None,
                  aliases: list[tuple[str, str]] | None = None,
                  type_: str = "person") -> int:
    from vera_shared.db.engine import get_session
    attrs = {"email": email_attr} if email_attr else {}
    async with get_session() as s:
        ent = EntityRow(type=type_, name=name, attributes=attrs)
        s.add(ent)
        await s.flush()
        for source, identifier in (aliases or []):
            s.add(EntityAliasRow(entity_id=ent.id, source=source,
                                 identifier=identifier, confidence=1.0))
        return ent.id


async def _alive() -> set[int]:
    from vera_shared.db.engine import get_session
    async with get_session() as s:
        return set((await s.execute(select(EntityRow.id))).scalars().all())


@pytest.mark.usefixtures("sqlite_db")
class TestFind:

    @pytest.mark.asyncio
    async def test_gmail_alias_and_profile_email_are_the_same_claim(self):
        """Почтовый ингестор держит адрес алиасом, профиль Slack — атрибутом.
        Это один и тот же человек, а не два разных признака."""
        a = await _person("Igor Nerozya", aliases=[("gmail", "nerozya_i@itstep.org")])
        b = await _person("Igor Nerozya", email_attr="nerozya_i@itstep.org",
                          aliases=[("slack", "user:U04")])
        groups = await collisions.find_email_collisions()
        assert len(groups) == 1
        assert {c["id"] for c in groups[0]["candidates"]} == {a, b}
        assert groups[0]["email"] == "nerozya_i@itstep.org"

    @pytest.mark.asyncio
    async def test_case_and_spaces_do_not_split_a_pair(self):
        a = await _person("Olga", aliases=[("gmail", "kryachko_o@itstep.org")])
        b = await _person("Olga", email_attr="  Kryachko_O@ItStep.org ")
        groups = await collisions.find_email_collisions()
        assert {c["id"] for c in groups[0]["candidates"]} == {a, b}

    @pytest.mark.asyncio
    async def test_different_addresses_are_not_a_group(self):
        await _person("A", aliases=[("gmail", "a@itstep.org")])
        await _person("B", aliases=[("gmail", "b@itstep.org")])
        assert await collisions.find_email_collisions() == []

    @pytest.mark.asyncio
    async def test_non_person_entities_are_ignored(self):
        """Организация с адресом рассылки — не дубль человека."""
        await _person("Bank", email_attr="no-reply@bank.com", type_="organization")
        await _person("Bank too", email_attr="no-reply@bank.com",
                      type_="organization")
        assert await collisions.find_email_collisions() == []

    @pytest.mark.asyncio
    async def test_garbage_in_the_email_field_is_skipped(self):
        await _person("X", email_attr="не адрес")
        await _person("Y", email_attr="не адрес")
        assert await collisions.find_email_collisions() == []


@pytest.mark.usefixtures("sqlite_db")
class TestMerge:

    @pytest.mark.asyncio
    async def test_unambiguous_pair_is_merged_and_aliases_survive(self):
        keeper = await _person("Ruslan", aliases=[("gmail", "r@itstep.org")])
        other = await _person("Ruslan", email_attr="r@itstep.org",
                              aliases=[("slack", "user:U09")])
        done = await collisions.merge_email_collision_pairs()
        assert len(done) == 1
        survivors = await _alive()
        assert len(survivors & {keeper, other}) == 1
        from vera_shared.db.engine import get_session
        async with get_session() as s:
            sources = set((await s.execute(
                select(EntityAliasRow.source)
                .where(EntityAliasRow.entity_id == done[0]["keeper"])
            )).scalars().all())
        assert sources == {"gmail", "slack"}

    @pytest.mark.asyncio
    async def test_group_of_three_is_left_to_the_owner(self):
        """Три сущности на один адрес — уже не «пара»: сливать наугад нельзя."""
        for i in range(3):
            await _person(f"Kolya {i}", email_attr="k@itstep.org")
        assert await collisions.merge_email_collision_pairs() == []
        assert len(await _alive()) == 3

    @pytest.mark.asyncio
    async def test_dry_run_changes_nothing(self):
        await _person("A", aliases=[("gmail", "z@itstep.org")])
        await _person("B", email_attr="z@itstep.org")
        before = await _alive()
        done = await collisions.merge_email_collision_pairs(dry_run=True)
        assert len(done) == 1
        assert "moved" not in done[0]
        assert await _alive() == before

    @pytest.mark.asyncio
    async def test_richer_graph_is_kept(self):
        """merge_entities отбрасывает КОНФЛИКТУЮЩИЕ связи, поэтому чем богаче
        keeper, тем меньше может быть потеряно."""
        poor = {"id": 1, "name": "A", "weight": 0, "sources": ["slack"]}
        rich = {"id": 2, "name": "A", "weight": 11, "sources": ["gmail"]}
        keeper, merged = collisions.pick_keeper([poor, rich])
        assert (keeper["id"], merged["id"]) == (2, 1)

    @pytest.mark.asyncio
    async def test_equal_weight_keeps_the_older_entity(self):
        keeper, merged = collisions.pick_keeper([
            {"id": 50, "name": "A", "weight": 3, "sources": []},
            {"id": 20, "name": "A", "weight": 3, "sources": []},
        ])
        assert (keeper["id"], merged["id"]) == (20, 50)

    @pytest.mark.asyncio
    async def test_second_run_finds_nothing_left(self):
        await _person("A", aliases=[("gmail", "q@itstep.org")])
        await _person("B", email_attr="q@itstep.org")
        await collisions.merge_email_collision_pairs()
        assert await collisions.merge_email_collision_pairs() == []


@pytest.mark.usefixtures("sqlite_db")
class TestDossiers:
    """Досье умело только telegram, поэтому у slack/gmail-людей оно было пустым
    и судить пары «по контексту» было физически нечем."""

    async def _event(self, source: str, **meta) -> None:
        from datetime import datetime

        from vera_shared.db.engine import get_session
        from vera_shared.db.models import EventRow
        async with get_session() as s:
            s.add(EventRow(
                source=source,
                source_event_id=f"{source}:{meta.get('sender_id') or meta.get('from')}:"
                                f"{meta.pop('n', 1)}",
                occurred_at=datetime(2026, 8, 26, 12, 0),
                content_text=meta.pop("text", "Author: X [counterparty]\n---\nтекст"),
                project=meta.pop("project", None),
                metadata_=meta))

    @pytest.mark.asyncio
    async def test_slack_person_gets_samples_and_places(self):
        eid = await _person("Igor", aliases=[("slack", "user:U04")])
        await self._event("slack", sender_id="U04", channel_name="devops-developers",
                          text="Author: Igor [counterparty]\n---\nне маю доступу",
                          project="itstep", n=1)
        got = (await dossiers.build([eid]))[eid]
        assert got["channels"] == ["slack"]
        assert any("не маю доступу" in s for s in got["samples"])
        assert got["top_places"][0][0].startswith("devops-developers")
        assert got["dom_project"] == "itstep"
        assert got["msg_count"] == 1

    @pytest.mark.asyncio
    async def test_gmail_person_is_matched_by_address_in_from(self):
        eid = await _person("Ruslan", aliases=[("gmail", "kovtyukh_r@itstep.org")])
        await self._event("gmail", **{"from": "Ruslan <kovtyukh_r@itstep.org>"},
                          text="Author: Ruslan [counterparty]\n---\nпо лендингам")
        got = (await dossiers.build([eid]))[eid]
        assert any("по лендингам" in s for s in got["samples"])
        assert got["msg_count"] == 1

    @pytest.mark.asyncio
    async def test_one_person_across_channels_gets_both(self):
        eid = await _person("Zhenya", email_attr="z@itstep.org",
                            aliases=[("slack", "user:U08"),
                                     ("gmail", "z@itstep.org")])
        await self._event("slack", sender_id="U08", channel_name="pm-only",
                          text="Author: Z [counterparty]\n---\nмит перенёс", n=1)
        await self._event("gmail", **{"from": "Z <z@itstep.org>"},
                          text="Author: Z [counterparty]\n---\nсчёт приложил")
        got = (await dossiers.build([eid]))[eid]
        assert got["channels"] == ["gmail", "slack"]
        assert any("[slack]" in s for s in got["samples"])
        assert any("[gmail]" in s for s in got["samples"])
        assert got["msg_count"] == 2

    @pytest.mark.asyncio
    async def test_entity_without_events_gets_an_empty_but_valid_dossier(self):
        eid = await _person("Никто", aliases=[("slack", "user:UZZ")])
        got = (await dossiers.build([eid]))[eid]
        assert got["samples"] == []
        assert got["msg_count"] == 0
        assert got["channels"] == ["slack"]

    @pytest.mark.asyncio
    async def test_empty_input(self):
        assert await dossiers.build([]) == {}


@pytest.mark.usefixtures("sqlite_db")
class TestMergeKeepsAttributes:
    """merge_entities удаляет строку merged целиком, и до 2026-08-26 её
    attributes уходили вместе с ней. Поймано на живых данных: при слиянии
    Igor/Olga/Ruslan пропали телефон и должность из профиля Slack — email выжил
    только потому, что его пишет ещё и почтовый ингестор."""

    @pytest.mark.asyncio
    async def test_merged_attributes_fill_the_gaps(self):
        from vera_shared.db.engine import get_session
        from vera_shared.graph.dedup import merge_entities

        keeper = await _person("Igor", aliases=[("gmail", "i@itstep.org")])
        async with get_session() as s:
            ent = (await s.execute(
                select(EntityRow).where(EntityRow.id == keeper))).scalar_one()
            ent.attributes = {"email": "i@itstep.org"}
        merged = await _person("Igor", email_attr="i@itstep.org")
        async with get_session() as s:
            ent = (await s.execute(
                select(EntityRow).where(EntityRow.id == merged))).scalar_one()
            ent.attributes = {"email": "i@itstep.org", "phone": "+380996664756",
                              "title": "Frontend Developer IT Step"}

        result = await merge_entities(keeper, merged)
        assert result["attributes_gained"] == 2

        async with get_session() as s:
            attrs = (await s.execute(
                select(EntityRow.attributes).where(EntityRow.id == keeper)
            )).scalar_one()
        assert attrs["phone"] == "+380996664756"
        assert attrs["title"] == "Frontend Developer IT Step"

    @pytest.mark.asyncio
    async def test_keeper_values_win_on_conflict(self):
        """Keeper выбран как более полная сторона — его данные приоритетнее."""
        from vera_shared.db.engine import get_session
        from vera_shared.graph.dedup import merge_entities

        keeper = await _person("A")
        merged = await _person("B")
        async with get_session() as s:
            for eid, attrs in ((keeper, {"phone": "правильный"}),
                               (merged, {"phone": "устаревший"})):
                ent = (await s.execute(
                    select(EntityRow).where(EntityRow.id == eid))).scalar_one()
                ent.attributes = attrs

        await merge_entities(keeper, merged)
        async with get_session() as s:
            attrs = (await s.execute(
                select(EntityRow.attributes).where(EntityRow.id == keeper)
            )).scalar_one()
        assert attrs["phone"] == "правильный"

    @pytest.mark.asyncio
    async def test_nothing_to_gain_is_reported_as_zero(self):
        from vera_shared.graph.dedup import merge_entities
        keeper = await _person("A")
        merged = await _person("B")
        result = await merge_entities(keeper, merged)
        assert result["attributes_gained"] == 0

    @pytest.mark.asyncio
    async def test_email_collision_merge_preserves_the_slack_profile(self):
        """Сквозная проверка того самого случая, что сломался вживую."""
        from vera_shared.db.engine import get_session

        gmail_side = await _person("Olga", aliases=[("gmail", "o@itstep.org")])
        slack_side = await _person("Olga", email_attr="o@itstep.org",
                                   aliases=[("slack", "user:U09")])
        async with get_session() as s:
            ent = (await s.execute(
                select(EntityRow).where(EntityRow.id == slack_side))).scalar_one()
            ent.attributes = {"email": "o@itstep.org", "phone": "380676574168",
                              "title": "Designer"}

        done = await collisions.merge_email_collision_pairs()
        assert len(done) == 1
        async with get_session() as s:
            attrs = (await s.execute(
                select(EntityRow.attributes).where(EntityRow.id == done[0]["keeper"])
            )).scalar_one()
        assert attrs["phone"] == "380676574168"
        assert attrs["title"] == "Designer"
        assert gmail_side in (done[0]["keeper"], done[0]["merged"])
