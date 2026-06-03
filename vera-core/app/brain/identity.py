"""L3 — Identity nodes: Goal, Value, NoGo, Style.

Each node lives in Neo4j alongside L1/L2. Vera reads these every
decision via decide.scoring; writes happen through brain.editor when
Dima talks to her, or directly via convenience helpers here.

Schemas (informal — properties are flexible, query code in scoring.py):

  (:Goal {id, title, metric, deadline, status='active'|'done'|'paused',
          weight, created_at})
  (:Value {id, statement, tool_pattern?, weight, created_at})
  (:NoGo {id, description, tool_pattern, targets[], weight=10,
          created_at})
  (:Style {id, relationship_id, tone, examples[], updated_at})

Edges:
  (Goal)-[:ABOUT]->(Entity)        # what the goal is about
  (Value)-[:APPLIES_TO]->(Domain)  # scoping
  (NoGo)-[:BLOCKS]->(Tool)         # which tool it bans
  (Style)-[:FOR]->(Person)         # whom to mimic-with
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from app.config import get_settings

log = logging.getLogger(__name__)


async def upsert_goal(*, id: str | None = None, title: str,
                       metric: str | None = None, deadline: str | None = None,
                       weight: float = 1.0, about_ids: list[str] | None = None) -> str:
    """Create or update a Goal. Returns the goal id."""
    return await _upsert("Goal", id=id, props={
        "title": title, "metric": metric, "deadline": deadline,
        "status": "active", "weight": weight,
    }, edge_targets={"ABOUT": about_ids or []})


async def upsert_value(*, id: str | None = None, statement: str,
                        tool_pattern: str | None = None,
                        weight: float = 1.0) -> str:
    return await _upsert("Value", id=id, props={
        "statement": statement, "tool_pattern": tool_pattern, "weight": weight,
    })


async def upsert_nogo(*, id: str | None = None, description: str,
                       tool_pattern: str, targets: list[str] | None = None,
                       weight: float = 10.0) -> str:
    return await _upsert("NoGo", id=id, props={
        "description": description, "tool_pattern": tool_pattern,
        "targets": targets or [], "weight": weight,
    })


async def upsert_style(*, id: str | None = None, relationship_id: str,
                        tone: str, examples: list[str] | None = None) -> str:
    return await _upsert("Style", id=id, props={
        "relationship_id": relationship_id, "tone": tone,
        "examples": examples or [],
    }, edge_targets={"FOR": [relationship_id]} if relationship_id else None)


async def remember(statement: str, *, scope: str | None = None,
                    related: list[str] | None = None) -> str:
    """Generic «remember this fact/rule» node — for things Дима tells Vera
    that don't fit Goal/Value/NoGo/Style. Free-form memory.

    Example: «почта itstep.org относится к работе в IT Step (Indonesia),
    не путать с veranda.my (Veranda)».

      scope:    краткий ярлык темы («email_routing», «debtors_workflow»)
      related:  list of entity ids этот memo должен связаться с (через :MENTIONS)
    """
    return await _upsert("Memo", id=None, props={
        "statement": statement,
        "scope": scope or "general",
        "weight": 1.0,
    }, edge_targets={"MENTIONS": related or []})


async def search_memos(query: str | None = None,
                        scope: str | None = None,
                        limit: int = 50) -> list[dict]:
    """Read Memo nodes (+optional scope filter)."""
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    async with client.driver.session(database=db) as ses:
        where: list[str] = []
        params: dict = {"limit": int(limit)}
        if scope:
            where.append("n.scope = $scope")
            params["scope"] = scope
        if query:
            where.append("toLower(n.statement) CONTAINS toLower($q)")
            params["q"] = query
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        r = await ses.run(
            f"MATCH (n:Memo){where_sql} "
            f"RETURN n.id AS id, n.statement AS statement, n.scope AS scope, "
            f"n.created_at AS created_at "
            f"ORDER BY coalesce(n.updated_at, n.created_at) DESC LIMIT $limit",
            **params,
        )
        return [dict(rec) async for rec in r]


async def list_active() -> dict:
    """Return only ACTIVE identity nodes. Was: returned everything including
    deactivated. Bug fix 2026-06-02 — Дима жаловался что в утреннем дайджесте
    висела «Закрыть 3 сделки в Veranda Payments» (захоллюцинированная LLM
    21 мая) даже после deactivate.
    """
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    out: dict[str, list[dict]] = {"Goal": [], "Value": [], "NoGo": [], "Style": []}
    async with client.driver.session(database=db) as ses:
        for label in out.keys():
            # NULL status (legacy nodes) treated as active. Explicit
            # 'inactive' or 'deleted' filtered out.
            r = await ses.run(
                f"MATCH (n:{label}) "
                f"WHERE NOT coalesce(n.status,'active') IN ['inactive','deleted'] "
                f"RETURN n"
            )
            async for rec in r:
                node = rec["n"]
                out[label].append({k: node.get(k) for k in node.keys()})
    return out


async def deactivate(label: str, node_id: str) -> bool:
    """Soft-delete: mark status='inactive'. list_active() will skip it."""
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    async with client.driver.session(database=db) as ses:
        r = await ses.run(
            f"MATCH (n:{label} {{id: $id}}) SET n.status='inactive', "
            f"n.deactivated_at=$ts RETURN count(n) AS n",
            id=node_id, ts=datetime.utcnow().isoformat(),
        )
        row = await r.single()
        return bool(row and row["n"])


async def _upsert(label: str, *, id: str | None, props: dict,
                   edge_targets: dict[str, list[str]] | None = None) -> str:
    from app.graph.client import get_graphiti
    client = await get_graphiti()
    db = get_settings().neo4j_database
    nid = id or f"{label.lower()}_{uuid.uuid4().hex[:10]}"
    now = datetime.utcnow().isoformat()
    set_clauses = ", ".join(
        f"n.{k}=${k}" for k in props if props[k] is not None
    )
    if set_clauses:
        set_clauses = "SET " + set_clauses + ", "
    else:
        set_clauses = "SET "
    set_clauses += "n.updated_at=$now, n.created_at=coalesce(n.created_at,$now)"

    params = {"id": nid, "now": now}
    params.update({k: v for k, v in props.items() if v is not None})

    async with client.driver.session(database=db) as ses:
        await ses.run(
            f"MERGE (n:{label} {{id: $id}}) {set_clauses}",
            **params,
        )
        for rel, targets in (edge_targets or {}).items():
            for t in targets:
                if not t:
                    continue
                await ses.run(
                    f"MATCH (n:{label} {{id: $nid}}) "
                    f"MERGE (e {{id: $tid}}) "
                    f"MERGE (n)-[:{rel}]->(e)",
                    nid=nid, tid=t,
                )
    log.info("identity: upsert %s %s", label, nid)
    return nid
