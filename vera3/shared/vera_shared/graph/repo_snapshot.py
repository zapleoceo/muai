"""Срез графа для визуализатора /graph (`graph_snapshot`)."""
from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, text

from vera_shared.db.engine import get_session
from vera_shared.graph.pair_stats import ordered

# Hard ceiling on nodes returned to the browser — 8k+ entities / 7k edges
# would hairball any force layout. The /graph page never renders "all";
# it shows the connected core (degree filter) or a focused ego network.
GRAPH_MAX_NODES = 800


async def graph_snapshot(
    *, min_degree: int = 2, limit: int = 300,
    predicate: str | None = None, focus_id: int | None = None,
    raw_edges: bool = False,
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

    Рёбра-факты — одна связь на ПАРУ (`connections.connections_among`: главная роль,
    вес, «также»); `raw_edges=True` отдаёт прежние строки `relationships` по одной.
    Членство, дублирующее пару со связью, не рисуется отдельным ребром.

    Edges are only those whose BOTH endpoints are in the returned node set,
    so the client never references a missing node. `limit` is clamped to
    GRAPH_MAX_NODES to protect the browser."""
    # Импорт здесь: connections → connection_hints → identity → dedup → ingest → repo.
    from vera_shared.graph.connections import connections_among, inferred_partner_ids
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
          WHERE r.subject_entity_id = :fid AND r.is_current {pred_clause}
        UNION
        SELECT subject_entity_id AS nb FROM relationships r
          WHERE r.object_entity_id = :fid AND r.is_current {pred_clause}
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
          WHERE r.is_current {pred_clause}
        UNION ALL
        SELECT r.object_entity_id AS eid FROM relationships r
          WHERE r.is_current {pred_clause}
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
            if want_rels and not raw_edges and predicate in (None, "coworker_of"):
                known = set(ids)
                ids += [i for i in await inferred_partner_ids(focus_id)
                        if i not in known][:max(0, limit - len(ids))]
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
                    SELECT subject_entity_id AS eid FROM relationships WHERE is_current
                    UNION ALL
                    SELECT object_entity_id  AS eid FROM relationships WHERE is_current
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
        if want_rels and raw_edges:
            edge_rows += list((await s.execute(
                text(f"""
                    SELECT r.subject_entity_id AS source, r.object_entity_id AS target,
                           r.predicate, r.confidence
                    FROM relationships r
                    WHERE r.subject_entity_id IN :ids
                      AND r.object_entity_id IN :ids AND r.is_current {pred_clause}
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

    pair_edges = await connections_among(ids, predicate, focus_id) if want_rels and not raw_edges else []
    taken = {ordered(e["source"], e["target"]) for e in pair_edges}
    edges = list(pair_edges)
    for r in edge_rows:
        pair = ordered(r["source"], r["target"])
        if not raw_edges and pair in taken:
            continue
        taken.add(pair)
        edges.append({"source": r["source"], "target": r["target"],
                      "predicate": r["predicate"], "confidence": round(float(r["confidence"]), 2)})
    return {
        "nodes": [
            {"id": r["id"], "name": r["name"], "type": r["type"],
             "degree": degree_by_id.get(r["id"], 0),
             "username": r["username"], "tg_id": r["tg_id"]}
            for r in node_rows
        ],
        "edges": edges,
    }
