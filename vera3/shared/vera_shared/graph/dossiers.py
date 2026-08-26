"""Досье сущности по ВСЕМ каналам, а не только по telegram.

`dedup.get_entity_dossiers` ищет сообщения человека по числовому `tg_id` в
`metadata->>'sender_id'`. Для сущности, которая живёт только в почте или только
в Slack, это молча даёт пусто — и LLM-судья на `/entities/duplicates` судил
такие пары по воздуху. Поймано 2026-08-26 на четырёх настоящих дублях
(gmail + slack с одинаковым рабочим email): решить их «по контексту» было
физически нечем.

Здесь то же самое, но сопоставление идёт по алиасу КАЖДОГО источника:

| источник | алиас | где искать в событии |
|---|---|---|
| telegram | `user:<tg_id>` | `metadata->>'sender_id'` |
| slack | `user:<U…>` | `metadata->>'sender_id'` |
| instagram | `user:<pk>` | `metadata->>'sender_id'` |
| gmail | `<адрес>` | `metadata->>'from'` |

Запросов — по одному на источник, а не по одному на сущность: страница с
двумя сотнями кандидатов иначе выжирает пул соединений.
"""
from __future__ import annotations

import logging

from sqlalchemy import bindparam, text

from vera_shared.db.engine import get_session

log = logging.getLogger(__name__)

#: сколько сообщений показываем как образец «о чём говорит»
SAMPLES_PER_ENTITY = 3

#: источники, где автор опознаётся значением `metadata.sender_id`
_BY_SENDER_ID = ("telegram", "slack", "instagram")


def _snippet(content_text: str | None) -> str:
    """Отрезать шапку «Author:/Where:/From:…\\n---\\n», оставить тело."""
    if not content_text:
        return ""
    body = content_text.split("\n---\n", 1)[-1]
    return " ".join(body.split())[:160]


def empty(entity_id: int, name: str | None = None,
          type_: str | None = None) -> dict:
    return {"entity_id": entity_id, "name": name, "type": type_,
            "channels": [], "samples": [], "top_places": [],
            "dom_project": None, "msg_count": 0}


async def _aliases(entity_ids: list[int]) -> list[tuple[int, str, str]]:
    async with get_session() as s:
        return list((await s.execute(
            text("SELECT entity_id, source, identifier FROM entity_aliases "
                 "WHERE entity_id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": entity_ids},
        )).all())


async def _by_sender_id(source: str, keys: dict[str, int],
                        out: dict[int, dict]) -> None:
    """Сообщения источника, где автор опознаётся по `metadata.sender_id`."""
    if not keys:
        return
    ids = list(keys)
    place = "chat_title" if source == "telegram" else (
        "thread_title" if source == "instagram" else "channel_name")
    async with get_session() as s:
        samples = (await s.execute(text(f"""
            SELECT sender_id, content_text FROM (
              SELECT metadata->>'sender_id' AS sender_id, content_text,
                     row_number() OVER (PARTITION BY metadata->>'sender_id'
                                        ORDER BY occurred_at DESC) AS rn
              FROM events WHERE source = :src AND metadata->>'sender_id' IN :k
            ) x WHERE rn <= {SAMPLES_PER_ENTITY}
        """).bindparams(bindparam("k", expanding=True)),
            {"src": source, "k": ids})).all()
        places = (await s.execute(text(f"""
            SELECT metadata->>'sender_id', metadata->>'{place}', count(*), max(project)
            FROM events WHERE source = :src AND metadata->>'sender_id' IN :k
            GROUP BY 1, 2
        """).bindparams(bindparam("k", expanding=True)),
            {"src": source, "k": ids})).all()

    for sender, content in samples:
        snip = _snippet(content)
        if snip:
            out[keys[sender]]["samples"].append(f"[{source}] {snip}")
    for sender, place_name, count, project in places:
        d = out[keys[sender]]
        d["top_places"].append((f"{place_name or '—'} ({source})", count))
        d["msg_count"] += count
        if project and not d["dom_project"]:
            d["dom_project"] = project


async def _by_email(keys: dict[str, int], out: dict[int, dict]) -> None:
    """Почта: автор опознаётся адресом внутри `metadata.from`."""
    for addr, entity_id in keys.items():
        # lower(...) LIKE, а не ILIKE: ILIKE есть только в Postgres, а тесты
        # идут на SQLite — первый заход там молча не находил ничего.
        pattern = f"%{addr.lower()}%"
        async with get_session() as s:
            rows = (await s.execute(text(
                "SELECT content_text, project FROM events "
                "WHERE source='gmail' AND lower(metadata->>'from') LIKE :pat "
                "ORDER BY occurred_at DESC LIMIT :n"
            ), {"pat": pattern, "n": SAMPLES_PER_ENTITY})).all()
            total = (await s.execute(text(
                "SELECT count(*) FROM events WHERE source='gmail' "
                "AND lower(metadata->>'from') LIKE :pat"
            ), {"pat": pattern})).scalar_one()
        d = out[entity_id]
        for content, project in rows:
            snip = _snippet(content)
            if snip:
                d["samples"].append(f"[gmail] {snip}")
            if project and not d["dom_project"]:
                d["dom_project"] = project
        if total:
            d["top_places"].append((f"почта {addr}", total))
            d["msg_count"] += total


async def build(entity_ids: list[int]) -> dict[int, dict]:
    """Досье по каждой сущности: каналы, о чём пишет, где, сколько."""
    if not entity_ids:
        return {}
    async with get_session() as s:
        ents = (await s.execute(
            text("SELECT id, name, type FROM entities WHERE id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": entity_ids},
        )).mappings().all()

    out = {e["id"]: empty(e["id"], e["name"], e["type"]) for e in ents}
    for eid in entity_ids:
        out.setdefault(eid, empty(eid))

    per_source: dict[str, dict[str, int]] = {}
    for entity_id, source, identifier in await _aliases(entity_ids):
        out[entity_id]["channels"].append(source)
        key = identifier.removeprefix("user:") if source in _BY_SENDER_ID else identifier
        per_source.setdefault(source, {})[key] = entity_id

    for source in _BY_SENDER_ID:
        try:
            await _by_sender_id(source, per_source.get(source, {}), out)
        except Exception as e:  # noqa: BLE001 — досье не должно ронять страницу
            log.warning("досье: %s не собрал: %s", source, e)
    try:
        await _by_email(per_source.get("gmail", {}), out)
    except Exception as e:  # noqa: BLE001
        log.warning("досье: gmail не собрал: %s", e)

    for d in out.values():
        d["channels"] = sorted(set(d["channels"]))
        d["top_places"].sort(key=lambda p: -p[1])
        d["top_places"] = d["top_places"][:4]
    return out
