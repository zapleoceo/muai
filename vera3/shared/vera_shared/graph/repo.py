"""Graph repository — sync/upsert API for entities/aliases/memberships/etc.

**Границу этого слоя не пересекает ни один сервис.** Раньше докстринг
утверждал «это ЕДИНСТВЕННЫЙ слой, который трогает графовые таблицы», а
`gateway/query.py` джойнил `relationships` с `entities` прямо в
route-функции, и `ingestor_telegram/roster_sync.py` — `entity_aliases` с
`entities` в воркере. Оба переехали сюда (`list_relationships`,
`find_project_chats`, `get_entity`).

Что остаётся снаружи и почему: соседние модули пакета `graph/` —
`dedup.py`, `identity.py`, `dossiers.py`, `avatars.py`, `clusters.py` —
пишут по этим таблицам свой SQL. Это осознанно: `merge_entities`, разбор
коллизий и досье — операции, которые репозиторным CRUD'ом не выражаются,
и растаскивать их по обёрткам значило бы прятать транзакционную логику. Но
это ОДИН пакет: замена хранилища — правка `graph/`, а не поиск по всему
репозиторию. Формулировка «единственный слой» была про сервисы, и вот
теперь она верна.

Future swap to Neo4j is a new implementation behind the same interface (DIP).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import bindparam, func, select, text, update

from vera_shared.db.engine import get_session
from vera_shared.db.models_graph import (
    EntityAliasRow,
    EntityRow,
    IdentityNodeRow,
    MembershipRow,
    RelationshipRow,
)
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)

# Hard ceiling on nodes returned to the browser — 8k+ entities / 7k edges
# would hairball any force layout. The /graph page never renders "all";
# it shows the connected core (degree filter) or a focused ego network.
GRAPH_MAX_NODES = 800


# ─── Entities & Aliases (Identity Resolution) ────────────────────────────────


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


async def find_entity_by_name(name: str, type: str | None = None) -> int | None:
    """Fuzzy lookup. Useful for `tools.search_entities`."""
    async with get_session() as s:
        q = select(EntityRow.id).where(EntityRow.name.ilike(f"%{name}%"))
        if type:
            q = q.where(EntityRow.type == type)
        return (await s.execute(q.limit(1))).scalar_one_or_none()


async def find_entity_by_alias(source: str, identifier: str) -> int | None:
    async with get_session() as s:
        return (await s.execute(
            select(EntityAliasRow.entity_id).where(
                EntityAliasRow.source == source,
                EntityAliasRow.identifier == identifier,
            )
        )).scalar_one_or_none()


async def resolve_entity_exact(name: str) -> int | None:
    """Exact (case-insensitive) resolve by entity name, then alias
    display_name — but ONLY when the match is unambiguous.

    With 21 разных «Дима» in the graph, the old `.limit(1)` (no ORDER BY)
    attached rel-extract edges to an arbitrary namesake — non-deterministic
    and, worse, it silently fused different people into one node. Ambiguous
    names now resolve to None: rel_extract skips the edge rather than
    polluting the graph. Distinct from find_entity_by_name's fuzzy ILIKE —
    this path needs precision, not recall."""
    n = name.strip()
    async with get_session() as s:
        ids = list((await s.execute(
            select(EntityRow.id)
            .where(func.lower(EntityRow.name) == func.lower(n))
            .limit(2)
        )).scalars().all())
        if len(ids) == 1:
            return ids[0]
        if len(ids) > 1:
            return None   # namesakes — ambiguous, refuse to guess
        alias_ids = list((await s.execute(
            select(EntityAliasRow.entity_id).distinct()
            .where(func.lower(EntityAliasRow.display_name) == func.lower(n))
            .limit(2)
        )).scalars().all())
        return alias_ids[0] if len(alias_ids) == 1 else None


# ─── Graph snapshot (visualization) ──────────────────────────────────────────


async def graph_snapshot(
    *, min_degree: int = 2, limit: int = 300,
    predicate: str | None = None, focus_id: int | None = None,
) -> dict[str, Any]:
    """Node/edge slice for the /graph visualizer. Two modes:

    - focus_id set → ego network: that entity + its 1-hop neighbours.
    - else → the connected core: entities with degree ≥ min_degree, the
      top `limit` by degree (drops the long tail of once-mentioned names
      and, on its own, the ~6k entities with no relationships at all).

    Edges are relationships (LLM-факты) ПЛЮС memberships как predicate
    'member_of' — «кто в какой группе» и есть основная связность (люди
    одного проекта соединяются через узел группы; без этого коллеги по
    IT STEP выглядели несвязанными). predicate='member_of' фильтрует до
    одних членств; любой другой predicate — до одних rel-фактов.

    Edges are only those whose BOTH endpoints are in the returned node set,
    so the client never references a missing node. `limit` is clamped to
    GRAPH_MAX_NODES to protect the browser."""
    limit = max(1, min(limit, GRAPH_MAX_NODES))
    min_degree = max(1, min_degree)
    want_rels = predicate != "member_of"
    want_members = predicate in (None, "member_of")
    pred_clause = "AND r.predicate = :pred" if (predicate and want_rels) else ""
    params: dict[str, Any] = {"lim": limit}
    if predicate and want_rels:
        params["pred"] = predicate

    rel_nb = f"""
        SELECT object_entity_id AS nb FROM relationships r
          WHERE r.subject_entity_id = :fid {pred_clause}
        UNION
        SELECT subject_entity_id AS nb FROM relationships r
          WHERE r.object_entity_id = :fid {pred_clause}
    """
    mem_nb = """
        SELECT parent_entity_id AS nb FROM memberships
          WHERE child_entity_id = :fid AND is_current
        UNION
        SELECT child_entity_id AS nb FROM memberships
          WHERE parent_entity_id = :fid AND is_current
    """
    rel_deg = f"""
        SELECT r.subject_entity_id AS eid FROM relationships r
          WHERE TRUE {pred_clause}
        UNION ALL
        SELECT r.object_entity_id AS eid FROM relationships r
          WHERE TRUE {pred_clause}
    """
    mem_deg = """
        SELECT parent_entity_id AS eid FROM memberships WHERE is_current
        UNION ALL
        SELECT child_entity_id AS eid FROM memberships WHERE is_current
    """
    nb_parts = ([rel_nb] if want_rels else []) + ([mem_nb] if want_members else [])
    deg_parts = ([rel_deg] if want_rels else []) + ([mem_deg] if want_members else [])

    async with get_session() as s:
        if focus_id is not None:
            params["fid"] = focus_id
            ids = [focus_id] + list((await s.execute(text(f"""
                SELECT DISTINCT nb FROM (
                    {" UNION ".join(nb_parts)}
                ) x LIMIT :lim
            """), params)).scalars().all())
        else:
            params["mind"] = min_degree
            ids = list((await s.execute(text(f"""
                WITH degree AS (
                    SELECT eid, COUNT(*) AS deg FROM (
                        {" UNION ALL ".join(deg_parts)}
                    ) u GROUP BY eid
                )
                SELECT eid FROM degree WHERE deg >= :mind
                ORDER BY deg DESC LIMIT :lim
            """), params)).scalars().all())

        if not ids:
            return {"nodes": [], "edges": []}

        # Степень — ОДНОЙ группировкой по набору узлов. Раньше это были два
        # коррелированных подзапроса на КАЖДУЮ строку, то есть до 800×2 на
        # показ страницы, причём половина по memberships.child_entity_id, у
        # которой индекса не было вовсе (добавлен миграцией 028).
        #
        # Считается ПОЛНАЯ степень (все связи + все текущие членства), без
        # фильтра по predicate — как и раньше. Это сознательно не то же, что
        # `deg` из CTE выше: там степень В РАМКАХ фильтра, ею отбираются узлы,
        # а показываем мы «сколько связей у человека вообще».
        degree_by_id = dict((await s.execute(
            text("""
                SELECT eid, COUNT(*) AS deg FROM (
                    SELECT subject_entity_id AS eid FROM relationships
                    UNION ALL
                    SELECT object_entity_id  AS eid FROM relationships
                    UNION ALL
                    SELECT parent_entity_id  AS eid FROM memberships WHERE is_current
                    UNION ALL
                    SELECT child_entity_id   AS eid FROM memberships WHERE is_current
                ) u WHERE eid IN :ids GROUP BY eid
            """).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        )).all())

        # Expanding bindparam for IN — portable across Postgres (prod) and
        # SQLite (tests); raw `= ANY(:ids)` is Postgres-only.
        node_rows = (await s.execute(
            text("""
                SELECT e.id, e.name, e.type,
                       e.attributes->>'username' AS username,
                       e.attributes->>'tg_id'    AS tg_id
                FROM entities e WHERE e.id IN :ids
            """).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        )).mappings().all()

        edge_rows: list = []
        if want_rels:
            edge_rows += list((await s.execute(
                text(f"""
                    SELECT r.subject_entity_id AS source, r.object_entity_id AS target,
                           r.predicate, r.confidence
                    FROM relationships r
                    WHERE r.subject_entity_id IN :ids
                      AND r.object_entity_id IN :ids {pred_clause}
                """).bindparams(bindparam("ids", expanding=True)),
                {"ids": ids, **({"pred": predicate} if pred_clause else {})},
            )).mappings().all())
        if want_members:
            edge_rows += list((await s.execute(
                text("""
                    SELECT m.child_entity_id AS source, m.parent_entity_id AS target,
                           'member_of' AS predicate, 1.0 AS confidence
                    FROM memberships m
                    WHERE m.is_current
                      AND m.child_entity_id IN :ids
                      AND m.parent_entity_id IN :ids
                """).bindparams(bindparam("ids", expanding=True)),
                {"ids": ids},
            )).mappings().all())

    return {
        "nodes": [
            {"id": r["id"], "name": r["name"], "type": r["type"],
             "degree": degree_by_id.get(r["id"], 0),
             "username": r["username"], "tg_id": r["tg_id"]}
            for r in node_rows
        ],
        "edges": [
            {"source": r["source"], "target": r["target"],
             "predicate": r["predicate"], "confidence": round(float(r["confidence"]), 2)}
            for r in edge_rows
        ],
    }


# ─── Memberships ─────────────────────────────────────────────────────────────


async def upsert_membership(
    *, parent_entity_id: int, child_entity_id: int,
    source: str, role: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> None:
    """Upsert membership. Touches last_seen_at."""
    now = utc_naive_now()
    async with get_session() as s:
        existing = (await s.execute(
            select(MembershipRow).where(
                MembershipRow.parent_entity_id == parent_entity_id,
                MembershipRow.child_entity_id == child_entity_id,
                MembershipRow.source == source,
            )
        )).scalar_one_or_none()
        if existing:
            existing.last_seen_at = now
            existing.is_current = True
            if role:
                existing.role = role
            if attributes:
                existing.attributes = {**(existing.attributes or {}), **attributes}
            return
        s.add(MembershipRow(
            parent_entity_id=parent_entity_id,
            child_entity_id=child_entity_id,
            source=source, role=role,
            attributes=attributes or {},
            first_seen_at=now, last_seen_at=now, is_current=True,
        ))


async def list_members(parent_entity_id: int) -> list[dict[str, Any]]:
    async with get_session() as s:
        rs = await s.execute(
            select(MembershipRow, EntityRow)
            .join(EntityRow, EntityRow.id == MembershipRow.child_entity_id)
            .where(
                MembershipRow.parent_entity_id == parent_entity_id,
                MembershipRow.is_current.is_(True),
            )
        )
        return [
            {"entity_id": e.id, "name": e.name, "type": e.type,
             "role": m.role, "source": m.source,
             "attributes": {**e.attributes, **m.attributes}}
            for m, e in rs
        ]


# ─── Relationships (Graphiti-style facts) ────────────────────────────────────


async def get_entity(entity_id: int) -> EntityRow | None:
    """Сущность по id. Существует, чтобы вызывающему не приходилось открывать
    свою сессию и трогать таблицу напрямую (gateway так и делал)."""
    async with get_session() as s:
        return (await s.execute(
            select(EntityRow).where(EntityRow.id == entity_id)
        )).scalar_one_or_none()


async def list_relationships(entity_id: int, limit: int = 40) -> list[dict[str, Any]]:
    """Связи сущности в обе стороны, с именем и типом второго конца.

    Жила в `gateway/query.py` сырым SQL с двумя JOIN'ами по `entities` —
    то есть route-модуль ходил в графовые таблицы мимо этого слоя.
    """
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT r.predicate, r.fact, r.confidence,
                   CASE WHEN r.subject_entity_id = :eid THEN 'out' ELSE 'in' END AS direction,
                   CASE WHEN r.subject_entity_id = :eid THEN eo.name ELSE es.name END AS other_name,
                   CASE WHEN r.subject_entity_id = :eid THEN eo.type ELSE es.type END AS other_type
            FROM relationships r
            JOIN entities es ON es.id = r.subject_entity_id
            JOIN entities eo ON eo.id = r.object_entity_id
            WHERE (r.subject_entity_id = :eid OR r.object_entity_id = :eid)
              AND r.is_current
            ORDER BY r.confidence DESC
            LIMIT :limit
        """), {"eid": entity_id, "limit": limit})).mappings().all()
    return [dict(r) for r in rows]


async def find_project_chats() -> list[dict[str, Any]]:
    """Групповые чаты проектов: entity_id + tg_id + тип.

    Источник — `project_membership` (kind='chat'), сматченный на граф через
    alias `telegram:chat:<key>`. Жила в `ingestor_telegram/roster_sync.py`:
    воркер ингестора джойнил `entity_aliases` и `entities` напрямую.
    """
    async with get_session() as s:
        rows = (await s.execute(text("""
            SELECT DISTINCT e.id AS entity_id, e.name, e.type,
                   (e.attributes->>'tg_id') AS tg_id
            FROM project_membership pm
            JOIN entity_aliases a
              ON a.source = 'telegram' AND a.identifier = 'chat:' || pm.key
            JOIN entities e ON e.id = a.entity_id
            WHERE pm.kind = 'chat'
              AND e.type IN ('group', 'supergroup')
        """))).mappings().all()
    return [dict(r) for r in rows]


async def upsert_relationship(
    *, subject_entity_id: int, object_entity_id: int,
    predicate: str, fact: str | None = None,
    confidence: float = 0.6,
    derived_from_event_id: int | None = None,
) -> bool:
    """Soft-upsert: if (subject, predicate, object) exists → touch last_seen
    and return False. Otherwise insert and return True (so callers like
    rel-extract can count genuinely new links)."""
    now = utc_naive_now()
    async with get_session() as s:
        existing = (await s.execute(
            select(RelationshipRow).where(
                RelationshipRow.subject_entity_id == subject_entity_id,
                RelationshipRow.object_entity_id == object_entity_id,
                RelationshipRow.predicate == predicate,
            )
        )).scalar_one_or_none()
        if existing:
            existing.last_seen_at = now
            existing.confidence = max(existing.confidence, confidence)
            if fact and not existing.fact:
                existing.fact = fact
            return False
        s.add(RelationshipRow(
            subject_entity_id=subject_entity_id,
            object_entity_id=object_entity_id,
            predicate=predicate, fact=fact,
            confidence=confidence,
            derived_from_event_id=derived_from_event_id,
            first_seen_at=now, last_seen_at=now, is_current=True,
        ))
        return True


# ─── L3 Identity nodes (Goal/Value/NoGo/Style/Self/Fact) ─────────────────────


async def upsert_identity_node(
    *, type: str, label: str, payload: dict[str, Any],
    listener_entity_id: int | None = None,
    derived_from: dict[str, Any] | None = None,
    weight: float = 1.0, confidence: float = 0.7,
) -> int:
    """Upsert by (type, label, listener_entity_id). Style nodes are
    keyed by listener; Value/Goal by label only."""
    async with get_session() as s:
        q = select(IdentityNodeRow).where(
            IdentityNodeRow.type == type,
            IdentityNodeRow.label == label,
            IdentityNodeRow.listener_entity_id == listener_entity_id,
        )
        existing = (await s.execute(q)).scalar_one_or_none()
        if existing:
            existing.payload = payload
            existing.weight = weight
            existing.confidence = confidence
            if derived_from:
                existing.derived_from = derived_from
            existing.updated_at = utc_naive_now()
            return existing.id
        node = IdentityNodeRow(
            type=type, label=label, payload=payload,
            listener_entity_id=listener_entity_id,
            derived_from=derived_from or {},
            weight=weight, confidence=confidence,
            is_current=True,
        )
        s.add(node)
        await s.flush()
        return node.id


async def get_style_for_listener(listener_entity_id: int) -> dict[str, Any] | None:
    async with get_session() as s:
        row = (await s.execute(
            select(IdentityNodeRow).where(
                IdentityNodeRow.type == "style",
                IdentityNodeRow.listener_entity_id == listener_entity_id,
                IdentityNodeRow.is_current.is_(True),
            )
        )).scalar_one_or_none()
        return row.payload if row else None


async def get_global_style() -> dict[str, Any] | None:
    """Fallback style profile when no per-listener exists."""
    async with get_session() as s:
        row = (await s.execute(
            select(IdentityNodeRow).where(
                IdentityNodeRow.type == "style",
                IdentityNodeRow.listener_entity_id.is_(None),
                IdentityNodeRow.is_current.is_(True),
            )
        )).scalar_one_or_none()
        return row.payload if row else None
