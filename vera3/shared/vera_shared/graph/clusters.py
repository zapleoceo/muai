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
from typing import Any

from vera_shared.control import (
    CLUSTER_LABEL_DEADLINE_S,
    CLUSTER_LABEL_RETRIES,
    GRAPH_HUB_PERCENTILE,
    get_control,
    get_int_setting,
    set_control,
)
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)

CLUSTERS_KEY = "graph_clusters"
MIN_LABELED_SIZE = 5      # smaller communities stay unlabeled ("прочее")
MAX_LABELED = 12          # cap LLM calls per recompute
MIN_NODES_FOR_HUB_SPLIT = 10   # на крошечном графе перцентили бессмысленны


def split_hubs(degrees: dict[int, int], percentile: int) -> set[int]:
    """Сверх-хабы (степень выше перцентиля) — узлы вроде владельца, связанные
    со всеми: оставленные в label propagation, они склеивают весь граф в одно
    сообщество. Возвращает их id; 100 = никого не исключать."""
    if percentile >= 100 or len(degrees) < MIN_NODES_FOR_HUB_SPLIT:
        return set()
    vals = sorted(degrees.values())
    idx = min(len(vals) - 1, max(0, int(len(vals) * percentile / 100)))
    threshold = vals[idx]
    return {n for n, d in degrees.items() if d > threshold}


def attach_hubs(assign: dict[int, int], hubs: set[int],
                edges: list[tuple[int, int]]) -> dict[int, int]:
    """Приписать исключённые хабы к сообществу большинства их соседей
    (не-хабов). Хаб без размеченных соседей получает своё сообщество."""
    out = dict(assign)
    next_free = max(out.values(), default=-1) + 1
    for h in sorted(hubs):
        counts: dict[int, int] = {}
        for a, b in edges:
            nb = b if a == h else (a if b == h else None)
            if nb is not None and nb not in hubs and nb in out:
                counts[out[nb]] = counts.get(out[nb], 0) + 1
        if counts:
            out[h] = min(sorted(counts), key=lambda c: (-counts[c], c))
        else:
            out[h] = next_free
            next_free += 1
    return out


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
    so a broker hiccup never blocks the recompute. chat_async — брокер
    удалил синхронный /v1/chat."""
    from vera_shared.llm.client import chat_async
    by_comm: dict[int, list[dict]] = {}
    node_by_id = {n["id"]: n for n in nodes}
    for nid, c in assign.items():
        if nid in node_by_id:
            by_comm.setdefault(c, []).append(node_by_id[nid])

    # Free-пул бывает занят: и ожидание, и число повторов — runtime-настройки
    # (/settings), не константы в коде.
    deadline = await get_int_setting(CLUSTER_LABEL_DEADLINE_S, 240)
    retries = await get_int_setting(CLUSTER_LABEL_RETRIES, 2)

    labels: dict[int, str] = {}
    big = [(c, m) for c, m in sorted(by_comm.items()) if len(m) >= MIN_LABELED_SIZE]
    for c, members in big[:MAX_LABELED]:
        members.sort(key=lambda n: -(n.get("degree") or 0))
        listing = "\n".join(
            f"- {m['name']} ({m['type']})" for m in members[:25])
        label = ""
        for attempt in range(retries + 1):
            try:
                answer, _ = await chat_async(
                    messages=[{"role": "user",
                               "content": _LABEL_PROMPT.format(members=listing)}],
                    capability="chat:fast", max_tokens=60, temperature=0.3,
                    workflow="graph_clusters",
                    response_format=CLUSTER_LABEL_SCHEMA,
                    poll_deadline_s=float(deadline),
                )
                label = str(json.loads(answer.strip().strip("`").removeprefix("json"))
                            .get("label", "")).strip()[:40]
                break
            except Exception as e:
                log.warning("cluster %s labelling attempt %d/%d failed: %s",
                            c, attempt + 1, retries + 1, e)
        labels[c] = label or f"кластер {c + 1}"
    return labels


async def recompute_clusters(limit: int = 600) -> dict[str, Any]:
    """Snapshot the connected core → communities → Vera labels → app_control.

    Сверх-хабы (см. split_hubs, порог — настройка graph_hub_percentile)
    исключаются из propagation и приписываются к сообществу большинства
    соседей — иначе узел-владелец склеивает весь граф в один кластер."""
    from vera_shared.graph.repo import graph_snapshot
    snap = await graph_snapshot(min_degree=1, limit=limit)
    node_ids = [n["id"] for n in snap["nodes"]]
    edges = [(e["source"], e["target"]) for e in snap["edges"]]

    percentile = await get_int_setting(GRAPH_HUB_PERCENTILE, 99)
    degrees = {n["id"]: int(n.get("degree") or 0) for n in snap["nodes"]}
    hubs = split_hubs(degrees, percentile)
    core_nodes = [n for n in node_ids if n not in hubs]
    core_edges = [(a, b) for a, b in edges if a not in hubs and b not in hubs]
    assign = attach_hubs(label_propagation(core_nodes, core_edges), hubs, edges)
    labels = await name_clusters_llm(assign, snap["nodes"])

    payload = {
        "assign": {str(k): v for k, v in assign.items()},
        "labels": {str(k): v for k, v in labels.items()},
        "computed_at": utc_naive_now().isoformat(),
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
