"""Запись сущностей и их алиасов (identity resolution): `upsert_entity`,
`upsert_entity_linked`."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import EntityAliasRow, EntityRow
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger("vera_shared.graph.repo")


async def upsert_entity(
    *, type: str, name: str,
    source: str, identifier: str,
    canonical_id: str | None = None,
    display_name: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> int:
    """Upsert (Entity + Alias). Returns entity_id.

    If an alias (source, identifier) already exists → return its entity.
    Otherwise create new Entity + Alias.
    """
    async with get_session() as s:
        alias = (await s.execute(
            select(EntityAliasRow).where(
                EntityAliasRow.source == source,
                EntityAliasRow.identifier == identifier,
            )
        )).scalar_one_or_none()

        if alias:
            # touch last_seen on entity
            await s.execute(
                update(EntityRow).where(EntityRow.id == alias.entity_id)
                .values(last_seen_at=utc_naive_now())
            )
            if attributes or name:
                ent = (await s.execute(
                    select(EntityRow).where(EntityRow.id == alias.entity_id)
                )).scalar_one()
                if attributes:
                    ent.attributes = {**(ent.attributes or {}), **attributes}
                # Бэкфил истории создаёт сущности с именем-фолбэком (username /
                # tg_user_<id>) — как только live-путь видит настоящее имя,
                # обновляем. Настоящее имя фолбэком НЕ перетираем.
                old_attrs = ent.attributes or {}
                fallbacks = {
                    str(old_attrs.get("username") or "").lower(),
                    f"tg_user_{old_attrs.get('tg_id')}",
                }
                if (name and name != ent.name
                        and (ent.name or "").lower() in fallbacks
                        and name.lower() not in fallbacks):
                    ent.name = name
            return alias.entity_id

        ent = EntityRow(
            type=type, name=name,
            canonical_id=canonical_id,
            attributes=attributes or {},
        )
        s.add(ent)
        await s.flush()
        s.add(EntityAliasRow(
            entity_id=ent.id, source=source, identifier=identifier,
            display_name=display_name or name, confidence=1.0,
        ))
        return ent.id


async def upsert_entity_linked(
    *, type: str, name: str,
    source: str, identifier: str,
    known_as: list[tuple[str, str]],
    display_name: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Как `upsert_entity`, но сначала ищет человека по УЖЕ известным алиасам.

    `known_as` — пары (source, identifier), которые источник знает про этого
    человека помимо своей: например Slack отдаёт в профиле рабочий email, а он
    же служит алиасом gmail. Это детерминированное связывание, без догадок.

    Зачем: `upsert_entity` заводит НОВУЮ сущность, если своего алиаса ещё нет,
    поэтому один человек лежал в графе тремя записями — по одной на канал.
    Ревизия 2026-08-26 на живых данных: 25 slack-сущностей, связанных с другими
    каналами — ноль; Yevhenii Pavlenko существовал тремя сущностями сразу.
    Слияние оставалось только через LLM-предложения на /entities/duplicates.

    Возвращает (entity_id, как решили): `own` — нашли по своему алиасу,
    `linked` — прицепились к существующему человеку по известному алиасу,
    `new` — не нашли никого, создали.

    Слияние уже существующих сущностей здесь НЕ делается: это отдельная,
    разрушительная операция (`graph.dedup.merge_entities`) и решать её должен
    владелец, а не ингестор.
    """
    known = [(s, i) for s, i in known_as if s and i and (s, i) != (source, identifier)]

    async with get_session() as s:
        mine = (await s.execute(
            select(EntityAliasRow).where(
                EntityAliasRow.source == source,
                EntityAliasRow.identifier == identifier,
            )
        )).scalar_one_or_none()
        if mine is not None:
            entity_id, how = mine.entity_id, "own"
        else:
            entity_id, how = None, "new"
            for alias_source, alias_id in known:
                found = (await s.execute(
                    select(EntityAliasRow).where(
                        EntityAliasRow.source == alias_source,
                        EntityAliasRow.identifier == alias_id,
                    )
                )).scalar_one_or_none()
                if found is not None:
                    entity_id, how = found.entity_id, "linked"
                    break

        if entity_id is None:
            ent = EntityRow(type=type, name=name, attributes=attributes or {})
            s.add(ent)
            await s.flush()
            entity_id = ent.id
        else:
            ent = (await s.execute(
                select(EntityRow).where(EntityRow.id == entity_id)
            )).scalar_one()
            ent.last_seen_at = utc_naive_now()
            if attributes:
                # Свои данные НЕ перетирают уже известные: профиль Slack —
                # ещё один свидетель, а не истина в последней инстанции.
                ent.attributes = {**(attributes or {}), **(ent.attributes or {})}

        # Дописываем все алиасы, каких у сущности ещё нет. Email из профиля
        # Slack как алиас gmail — не выдумка: так письмо от этого человека
        # прилетит уже на существующую сущность, без участия LLM.
        for alias_source, alias_id in [(source, identifier), *known]:
            exists = (await s.execute(
                select(EntityAliasRow.id).where(
                    EntityAliasRow.source == alias_source,
                    EntityAliasRow.identifier == alias_id,
                )
            )).scalar_one_or_none()
            if exists is None:
                s.add(EntityAliasRow(
                    entity_id=entity_id, source=alias_source, identifier=alias_id,
                    display_name=display_name or name, confidence=1.0,
                ))

    if how == "linked":
        log.info("graph: %s/%s прицеплен к существующей сущности %s",
                 source, identifier, entity_id)
    return entity_id, how
