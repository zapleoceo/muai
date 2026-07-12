"""Thematic graph clusters — community detection + Vera-named labels.

`label_propagation` groups the connected core by link structure (who is
tightly connected with whom); `name_clusters_llm` then asks Vera to give each
big community a human label («Команда IT STEP», «Вьетнам/Veranda», «семья»…).
The result is cached as JSON in app_control['graph_clusters'] and joined onto
/api/graph nodes — recomputed only when the owner presses the button (graph
shape drifts slowly; no reason to burn LLM calls per page view).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from vera_shared.control import get_control, set_control

log = logging.getLogger(__name__)

CLUSTERS_KEY = "graph_clusters"
MIN_LABELED_SIZE = 5      # smaller communities stay unlabeled ("прочее")
MAX_LABELED = 12          # cap LLM calls per recompute


def label_propagation(node_ids: list[int],
                      edges: list[tuple[int, int]],
                      iterations: int = 20) -> dict[int, int]:
    """Deterministic synchronous label propagation. Returns node→community
    (community ids are renumbered 0..K by size, largest first)."""
    labels = {n: n for n in node_ids}
    neigh: dict[int, list[int]] = {n: [] for n in node_ids}
    for a, b in edges:
        if a in neigh and b in neigh:
            neigh[a].append(b)
            neigh[b].append(a)

    for _ in range(iterations):
        changed = False
        for n in sorted(node_ids):               # fixed order → deterministic
            if not neigh[n]:
                continue
            counts: dict[int, int] = {}
            for m in neigh[n]:
                counts[labels[m]] = counts.get(labels[m], 0) + 1
            # tie-break by smallest label id → stable across runs
            best = min(sorted(counts), key=lambda c: (-counts[c], c))
            if best != labels[n]:
                labels[n] = best
                changed = True
        if not changed:
            break

    by_comm: dict[int, list[int]] = {}
    for n, c in labels.items():
        by_comm.setdefault(c, []).append(n)
    renum = {c: i for i, (c, members) in enumerate(
        sorted(by_comm.items(), key=lambda kv: (-len(kv[1]), kv[0])))}
    return {n: renum[c] for n, c in labels.items()}


CLUSTER_LABEL_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "cluster_label",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
            "additionalProperties": False,
        },
    },
}

_LABEL_PROMPT = """Ты — Вера, память Димы. Вот одно сообщество из его графа
связей (люди/чаты, тесно связанные между собой):

{members}

Дай сообществу короткий ярлык на русском (1–3 слова, без кавычек), по
доминирующей теме: например «Команда IT STEP», «Вьетнам / Veranda»,
«Семья», «IT-фриланс чаты», «Новости». Верни СТРОГО JSON {{"label": "..."}}."""


async def name_clusters_llm(assign: dict[int, int],
                            nodes: list[dict[str, Any]]) -> dict[int, str]:
    """Vera names the biggest communities. Failures fall back to «кластер N»
    so a broker hiccup never blocks the recompute."""
    from vera_shared.llm.client import chat
    by_comm: dict[int, list[dict]] = {}
    node_by_id = {n["id"]: n for n in nodes}
    for nid, c in assign.items():
        if nid in node_by_id:
            by_comm.setdefault(c, []).append(node_by_id[nid])

    labels: dict[int, str] = {}
    big = [(c, m) for c, m in sorted(by_comm.items()) if len(m) >= MIN_LABELED_SIZE]
    for c, members in big[:MAX_LABELED]:
        members.sort(key=lambda n: -(n.get("degree") or 0))
        listing = "\n".join(
            f"- {m['name']} ({m['type']})" for m in members[:25])
        try:
            answer, _ = await chat(
                messages=[{"role": "user",
                           "content": _LABEL_PROMPT.format(members=listing)}],
                capability="chat:fast", max_tokens=60, temperature=0.3,
                workflow="graph_clusters",
                response_format=CLUSTER_LABEL_SCHEMA,
            )
            label = str(json.loads(answer.strip().strip("`").removeprefix("json"))
                        .get("label", "")).strip()[:40]
        except Exception as e:
            log.warning("cluster %s labelling failed: %s", c, e)
            label = ""
        labels[c] = label or f"кластер {c + 1}"
    return labels


async def recompute_clusters(limit: int = 600) -> dict[str, Any]:
    """Snapshot the connected core → communities → Vera labels → app_control."""
    from vera_shared.graph.repo import graph_snapshot
    snap = await graph_snapshot(min_degree=1, limit=limit)
    node_ids = [n["id"] for n in snap["nodes"]]
    edges = [(e["source"], e["target"]) for e in snap["edges"]]
    assign = label_propagation(node_ids, edges)
    labels = await name_clusters_llm(assign, snap["nodes"])

    payload = {
        "assign": {str(k): v for k, v in assign.items()},
        "labels": {str(k): v for k, v in labels.items()},
        "computed_at": datetime.utcnow().isoformat(),
        "nodes": len(node_ids), "edges": len(edges),
    }
    await set_control(CLUSTERS_KEY, json.dumps(payload, ensure_ascii=False))
    log.info("graph clusters recomputed: %d nodes, %d communities labeled",
             len(node_ids), len(labels))
    return payload


async def get_clusters() -> dict[str, Any] | None:
    raw = await get_control(CLUSTERS_KEY, "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None
