"""Детерминированные дубли по рабочему email — не догадка, а факт.

Рабочий email глобально уникален: две person-сущности, претендующие на один
адрес, — это один человек, попавший в граф дважды. Тот же класс, что коллизия
`@username` в telegram (`dedup.merge_username_collision_pairs`), и решается так
же: однозначные пары сливаются, неоднозначные группы остаются владельцу.

Откуда берутся. `upsert_entity` заводит НОВУЮ сущность, если своего алиаса ещё
нет, поэтому до появления `upsert_entity_linked` человек множился по записи на
канал. Замер 2026-08-26: Igor Nerozya, Olga Kryachko, Ruslan Kovtiukh —
по две сущности (gmail + slack) с буквально одинаковым адресом
`*@itstep.org`; Yevhenii Pavlenko — три.

Претендовать на адрес можно двумя способами, и оба считаются:

* алиас `(gmail, <адрес>)` — так его держит почтовый ингестор;
* `attributes.email` — так его записывает профиль Slack.

Слияние НЕ автоматическое: функции вызываются кнопкой на `/entities/duplicates`
либо вручную. Разрушительную операцию запускает владелец.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import (
    EntityAliasRow,
    EntityRow,
    MembershipRow,
    RelationshipRow,
)
from vera_shared.graph.dedup import merge_entities

log = logging.getLogger(__name__)


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


async def find_email_collisions(min_group: int = 2) -> list[dict]:
    """Группы person-сущностей, претендующих на один рабочий email.

    Всё через ORM и группировка в Python — по той же причине, что в
    `find_alias_collisions`: тесты идут на SQLite, а `string_agg` и операторы
    JSON туда не переносятся. Первый заход на сырых SQL молча вернул пусто.
    """
    async with get_session() as s:
        people = (await s.execute(
            select(EntityRow).where(EntityRow.type == "person")
        )).scalars().all()
        aliases = (await s.execute(select(EntityAliasRow))).scalars().all()
        rel_rows = (await s.execute(select(
            RelationshipRow.subject_entity_id, RelationshipRow.object_entity_id
        ))).all()
        mem_rows = (await s.execute(
            select(MembershipRow.child_entity_id))).scalars().all()

    by_entity: dict[int, list[EntityAliasRow]] = {}
    for a in aliases:
        by_entity.setdefault(a.entity_id, []).append(a)

    weight: dict[int, int] = {}
    for subject, obj in rel_rows:
        for side in (subject, obj):
            weight[side] = weight.get(side, 0) + 1
    for child in mem_rows:
        weight[child] = weight.get(child, 0) + 1

    groups: dict[str, list[dict]] = {}
    for person in people:
        own = by_entity.get(person.id, [])
        gmail = next((a.identifier for a in own if a.source == "gmail"), None)
        attrs = person.attributes if isinstance(person.attributes, dict) else {}
        email = normalize_email(gmail) or normalize_email(attrs.get("email"))
        if "@" not in email:
            continue
        groups.setdefault(email, []).append({
            "id": person.id, "name": person.name,
            "weight": weight.get(person.id, 0),
            "sources": sorted({a.source for a in own}),
        })

    out = [{"email": email, "candidates": members, "size": len(members)}
           for email, members in groups.items() if len(members) >= min_group]
    out.sort(key=lambda g: -g["size"])
    return out


def pick_keeper(candidates: list[dict]) -> tuple[dict, dict]:
    """→ (кого оставляем, кого сливаем).

    Оставляем сущность с более богатым графом: `merge_entities` отбрасывает
    КОНФЛИКТУЮЩИЕ связи и членства, поэтому чем богаче keeper, тем меньше
    может быть отброшено. При равенстве — та, что старше по id.
    """
    ranked = sorted(candidates, key=lambda c: (-c["weight"], c["id"]))
    return ranked[0], ranked[1]


async def merge_email_collision_pairs(dry_run: bool = False) -> list[dict]:
    """Слить однозначные пары: ровно ДВЕ person-сущности на один email.

    Группы из трёх и более не трогаем — там уже не «пара», и разбирать их
    должен владелец глазами. Возвращает список решений (в dry_run — что было
    бы сделано).
    """
    done: list[dict] = []
    for group in await find_email_collisions(min_group=2):
        if group["size"] != 2:
            log.warning("email %s: %d сущностей — не пара, оставляю владельцу",
                        group["email"], group["size"])
            continue
        keeper, merged = pick_keeper(group["candidates"])
        decision = {
            "email": group["email"],
            "keeper": keeper["id"], "keeper_name": keeper["name"],
            "keeper_sources": keeper["sources"],
            "merged": merged["id"], "merged_name": merged["name"],
            "merged_sources": merged["sources"],
        }
        if not dry_run:
            decision["moved"] = await merge_entities(keeper["id"], merged["id"])
            log.info("email %s: сущность #%s слита в #%s",
                     group["email"], merged["id"], keeper["id"])
        done.append(decision)
    return done
