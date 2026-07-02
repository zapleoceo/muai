"""Worker loop: SELECT pending events → triage + embed → UPDATE.

Concurrency model:
- N реплик через docker compose `--scale brain-triage=N`
- Каждая реплика берёт batch через `UPDATE ... WHERE id IN (SELECT FOR UPDATE
  SKIP LOCKED) RETURNING *` — реплики не дерутся за одни и те же события
- triage_started_at используется watchdog'ом чтобы вернуть зависшие
  (а НЕ received_at — иначе старые pending мгновенно реверится при подборе)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Any

from sqlalchemy import text, update
from vera_shared.control import backfill_minute_allowance, is_backfill_paused
from vera_shared.db.engine import get_session, init_engine
from vera_shared.db.models import EventRow
from vera_shared.llm.client import LLMCallFailed, chat, embed

log = logging.getLogger(__name__)

POLL_INTERVAL_S = float(os.environ.get("TRIAGE_POLL_INTERVAL_S", "5"))
BATCH_SIZE = int(os.environ.get("TRIAGE_BATCH_SIZE", "16"))
CONCURRENCY = int(os.environ.get("TRIAGE_CONCURRENCY", "5"))
PACE_BETWEEN_S = float(os.environ.get("TRIAGE_PACE_S", "0.5"))
WORKER_ID = os.environ.get("HOSTNAME", "worker") + ":" + str(os.getpid())
# Сколько секунд триаж может работать прежде чем watchdog считает его мёртвым.
# Должно быть БОЛЬШЕ чем самый медленный LLM-вызов × CONCURRENCY.
STUCK_AFTER_S = int(os.environ.get("TRIAGE_STUCK_AFTER_S", "600"))


TRIAGE_PROMPT_TEMPLATE = """Ты — Вера, цифровая память Димы. Прочитай событие и извлеки структуру.

Контекст Димы (его текущая жизнь):
- Branch Director IT STEP Academy Jakarta с апреля 2026 (проект itstep)
- Переезд в Индонезию, виза, KPI команды
- Совладелец бара Veranda во Вьетнаме (проект veranda)
- Жена Маша, дочь Лиза (family)
- Босс Дмитрий Егоров (yegorov@itstep.org)

Событие (источник={source}, account={account}, occurred_at={occurred_at}):
---
{content}
---

Верни СТРОГО JSON по схеме:
{{
  "importance": <0-100, насколько Дима должен это видеть>,
  "project": "<РОВНО ОДНО из: itstep | veranda | family | personal | news | other>",
  "nature": "<РОВНО ОДНО из: world_event | my_intent>",
  "topics": [<2-4 тега: русский, нижний регистр, 1-2 слова. Канонические:
    финансы, должники, расписание, найм, продажи, маркетинг, crm,
    бар, меню, поставки, персонал, зарплата,
    виза, переезд, семья, здоровье, новости, война, политика,
    техника, недвижимость, документы>],
  "people_mentioned": [<упомянутые люди>],
  "signals": [
    {{"type": "task|event|news|offer|question|decision|anomaly",
      "summary": "<краткое>",
      "date": "<ISO дата если есть, иначе null>"}}
  ],
  "needs_action": <true/false>,
  "ready_subtype": <null | "deal" | "openhouse" — см. ниже>
}}

Правила project:
- itstep — академия в Джакарте: группы, студенты, должники, лиды, команда филиала
- veranda — бар во Вьетнаме: смены, заказы, выручка, поставки
- family — Маша, Лиза, родители
- personal — личные дела Димы (банки, виза, здоровье, друзья)
- news — новостные каналы и рассылки
- other — всё прочее

Правила nature:
- world_event — письмо/сообщение от человека или системы, факт мира
- my_intent — Дима сам формулирует запрос/черновик/идею (не свершившийся факт)

Правило ready_subtype (заполни ТОЛЬКО если needs_action=true):
- "deal": лид ИМЕЕТ контакт И ЯВНОЕ намерение купить курс И готов действовать ЧАС/ДЕНЬ
  Примеры: "Привет, я хочу записаться на курс. Вот мой номер: +62812..."
           "Готов платить, когда начнём?"
           "Как записаться? Дайте счёт."

- "openhouse": лид заинтересован ПОСЕТИТЬ Open House 29 июня (НЕ покупка, это мероприятие)
  Примеры: "Подойдёт ли мне курс? Я на Опен Хаусе узнаю подробнее?"
           "Когда у вас опен хаус? Хочу прийти 29 июня"
           "Расскажите про мероприятие на 29 числа"

- null (если needs_action=false ИЛИ если готовность неясна)
  Примеры: лид просто спрашивает про программу (информационный запрос)
           лид высказывает сомнения или еще не готов

ВАЖНО: только JSON, без префиксов и комментариев."""

# Детерминированная nature по источнику — надёжнее LLM там где источник
# сам по себе определяет природу. Для новых источников решает LLM-поле.
NATURE_BY_SOURCE = {
    "vera_chat": "conversation_with_me",
    "perplexity": "my_intent",
    "vera_memory": "derived_fact",
}
VALID_NATURES = {"world_event", "my_intent", "conversation_with_me", "derived_fact"}
PROJECT_VOCAB = {"itstep", "veranda", "family", "personal", "news", "other"}
# Источники-намерения не эмбеддим: их вектора засоряют семантический поиск
SKIP_EMBED_SOURCES = {"vera_chat", "perplexity"}

# json_schema (не json_object) — провайдеры с grammar-constrained decoding
# (gemini, openai, groq) физически не могут выдать невалидный JSON или
# значение вне enum. json_object давал модели "как получится" — часть
# ответов (особенно cerebras gpt-oss) приходила битой и терялась.
# postprocess_triage() остаётся: providers без constrained-decoding
# (litellm's drop_params тихо роняет response_format) всё ещё нуждаются
# в client-side защите.
TRIAGE_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "triage",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "importance": {"type": "integer", "minimum": 0, "maximum": 100},
                "project": {"type": "string", "enum": sorted(PROJECT_VOCAB)},
                "nature": {"type": "string", "enum": ["world_event", "my_intent"]},
                "topics": {"type": "array", "items": {"type": "string"}},
                "people_mentioned": {"type": "array", "items": {"type": "string"}},
                "signals": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["task", "event", "news", "offer",
                                         "question", "decision", "anomaly"],
                            },
                            "summary": {"type": "string"},
                            "date": {"type": ["string", "null"]},
                        },
                        "required": ["type", "summary", "date"],
                        "additionalProperties": False,
                    },
                },
                "needs_action": {"type": "boolean"},
                "ready_subtype": {
                    "type": ["string", "null"],
                    "enum": ["deal", "openhouse", None],
                },
            },
            "required": [
                "importance", "project", "nature", "topics",
                "people_mentioned", "signals", "needs_action", "ready_subtype",
            ],
            "additionalProperties": False,
        },
    },
}


def postprocess_triage(parsed: dict[str, Any], source: str) -> dict[str, Any]:
    """Валидация LLM-классификации против словарей + override по source."""
    nature = NATURE_BY_SOURCE.get(source) or str(parsed.get("nature") or "").strip()
    if nature not in VALID_NATURES:
        nature = "world_event"
    project = str(parsed.get("project") or "").lower().strip()
    if project not in PROJECT_VOCAB:
        project = "other"
    parsed["nature"] = nature
    parsed["project"] = project

    # Валидация ready_subtype
    ready_subtype = parsed.get("ready_subtype")
    if isinstance(ready_subtype, str):
        ready_subtype = ready_subtype.strip().lower()
    if ready_subtype not in (None, "deal", "openhouse"):
        ready_subtype = None
    # Enforce: ready_subtype can only be set if needs_action=true
    if not parsed.get("needs_action"):
        ready_subtype = None
    parsed["ready_subtype"] = ready_subtype

    return parsed


async def triage_one(event_row: EventRow) -> dict[str, Any] | None:
    """Триаж одного события. Возвращает metadata."""
    content = (event_row.content_text or "")[:8000]
    prompt = TRIAGE_PROMPT_TEMPLATE.format(
        source=event_row.source,
        account=event_row.account or "—",
        occurred_at=event_row.occurred_at.isoformat() if event_row.occurred_at else "—",
        content=content,
    )

    response_text, meta = await chat(
        messages=[{"role": "user", "content": prompt}],
        capability="chat:fast",
        response_format=TRIAGE_JSON_SCHEMA,
        max_tokens=1500,
        temperature=0.3,
        workflow="triage",
        event_id=event_row.id,
    )

    response_text = response_text.strip()
    if response_text.startswith("```"):
        response_text = re.sub(r"^```(?:json)?\n?", "", response_text)
        response_text = re.sub(r"\n?```$", "", response_text)

    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError:
        # Попытка вытащить JSON из текста (LLM иногда добавляет prefix/suffix)
        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if not match:
            raise
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            raise

    # Если LLM вернул не dict (массив, строку) — ошибка
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object, got {type(parsed).__name__}")

    parsed = postprocess_triage(parsed, event_row.source)
    parsed["triaged_by_provider"] = meta.get("provider")
    parsed["triaged_by_model"] = meta.get("model")
    parsed["triaged_at"] = datetime.utcnow().isoformat()
    return parsed


async def _claim_batch(limit: int = BATCH_SIZE) -> list[EventRow]:
    """Захватить batch событий ОДНИМ запросом через UPDATE ... RETURNING *.

    Старый код делал UPDATE + второй SELECT — между ними окно для гонки/потери
    видимости. Теперь всё в одной сессии, и `triage_started_at = NOW()` ставится
    атомарно вместе с claim'ом. `limit` урезается rate-лимитером бэкфилла.
    """
    if limit <= 0:
        return []
    async with get_session() as s:
        rs = await s.execute(text(
            """
            UPDATE events
            SET triage_status = 'processing',
                triage_started_at = NOW()
            WHERE id IN (
              SELECT id FROM events
              WHERE triage_status = 'pending' AND content_text != ''
              ORDER BY occurred_at DESC
              LIMIT :batch
              FOR UPDATE SKIP LOCKED
            )
            RETURNING id, source, source_event_id, account, category,
                      content_text, occurred_at, importance
            """
        ), {"batch": limit})
        mappings = list(rs.mappings().all())

    if not mappings:
        return []

    # Лёгкие detached объекты — не нужно второй SELECT, ORM не требуется
    out: list[EventRow] = []
    for m in mappings:
        ev = EventRow(
            id=m["id"], source=m["source"], source_event_id=m["source_event_id"],
            account=m["account"], category=m["category"],
            content_text=m["content_text"], occurred_at=m["occurred_at"],
            importance=m["importance"],
        )
        out.append(ev)
    return out


async def _embed_batch(texts: list[str]) -> list[list[float] | None]:
    """Один Voyage-запрос на N текстов."""
    if not texts:
        return []
    try:
        vectors = await embed(texts)
        if len(vectors) != len(texts):
            log.warning("Embed mismatch: got %d, expected %d", len(vectors), len(texts))
            return [None] * len(texts)
        return vectors
    except Exception as e:
        log.warning("Batch embed failed: %s", e)
        return [None] * len(texts)


async def _process_one_with_sem(
    sem: asyncio.Semaphore, row: EventRow,
) -> tuple[int, str, dict | None, str | None]:
    """Triage под семафором. Возвращает (event_id, status, metadata, error)."""
    async with sem:
        try:
            metadata = await asyncio.wait_for(triage_one(row), timeout=120)
            return row.id, "done", metadata, None
        except asyncio.TimeoutError:
            return row.id, "pending", None, "timeout"
        except LLMCallFailed as e:
            return row.id, "pending", None, str(e)[:200]
        except Exception as e:
            log.warning("Triage failed for event %s: %s", row.id, e)
            return row.id, "error", None, str(e)[:500]


async def process_pending() -> int:
    """Захватить batch, эмбедить parallel, триаж concurrent, UPDATE."""
    if await is_backfill_paused():
        return 0   # paused from dashboard — skip claiming, main loop sleeps
    # Even-tempo rate limit: claim at most this minute's remaining budget.
    allowance = await backfill_minute_allowance()
    if allowance is not None and allowance <= 0:
        return 0   # rate reached — main loop sleeps, recheck next cycle
    batch = BATCH_SIZE if allowance is None else min(BATCH_SIZE, allowance)
    rows = await _claim_batch(batch)
    if not rows:
        return 0

    log.info("[%s] claimed batch of %d events", WORKER_ID, len(rows))

    # Источники-намерения (vera_chat, perplexity) не эмбеддим — их вектора
    # засоряют семантический поиск. Эмбеддим только события мира.
    embed_idx = [i for i, r in enumerate(rows) if r.source not in SKIP_EMBED_SOURCES]
    embed_texts = [(rows[i].content_text or "")[:8000] for i in embed_idx]
    embed_vectors = await _embed_batch(embed_texts)
    embeddings: list[list[float] | None] = [None] * len(rows)
    for pos, vec in zip(embed_idx, embed_vectors):
        embeddings[pos] = vec

    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [_process_one_with_sem(sem, row) for row in rows]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    src_by_id = {r.id: r.source for r in rows}
    processed = 0
    llm_exhausted = 0
    async with get_session() as s:
        for (event_id, status, metadata, error), embedding in zip(results, embeddings):
            if status == "pending":
                # LLM пул занят — вернём в pending, освобождаем triage_started_at
                await s.execute(
                    update(EventRow).where(EventRow.id == event_id).values(
                        triage_status="pending",
                        triage_started_at=None,
                    )
                )
                llm_exhausted += 1
            elif status == "done":
                await s.execute(
                    update(EventRow).where(EventRow.id == event_id).values(
                        triage_status="done",
                        triage_metadata=metadata,
                        importance=metadata.get("importance") if metadata else None,
                        nature=metadata.get("nature") if metadata else None,
                        project=metadata.get("project") if metadata else None,
                        ready_subtype=metadata.get("ready_subtype") if metadata else None,
                        embedding_voyage_3=embedding,
                        triage_started_at=None,
                    )
                )
                processed += 1
                # Fire relationship extraction in background — non-blocking,
                # doesn't gate triage success. Only for high-signal events.
                if metadata and metadata.get("importance", 0) >= 3:
                    row = next((r for r in rows if r.id == event_id), None)
                    if row and row.content_text:
                        asyncio.create_task(
                            _safe_rel_extract(event_id, row.content_text)
                        )
            else:  # error
                # nature детерминируема по source даже без LLM
                err_nature = NATURE_BY_SOURCE.get(
                    src_by_id.get(event_id, ""), "world_event")
                await s.execute(
                    update(EventRow).where(EventRow.id == event_id).values(
                        triage_status="error",
                        triage_error=error,
                        nature=err_nature,
                        embedding_voyage_3=embedding,
                        triage_started_at=None,
                    )
                )

    log.info("[%s] processed: %d done, %d exhausted, %d errors",
             WORKER_ID, processed, llm_exhausted, len(rows) - processed - llm_exhausted)

    if PACE_BETWEEN_S > 0:
        await asyncio.sleep(PACE_BETWEEN_S)
    return processed


async def _watchdog_loop() -> None:
    """Возвращает 'processing' события в 'pending' если воркер крашнулся.

    Использует `triage_started_at` (когда захвачено), НЕ `received_at`.
    Это исправляет баг: старое pending событие (received_at месячной давности)
    мгновенно реверится сразу после claim'a.
    """
    sql = (
        "UPDATE events SET "
        "  triage_status='pending', "
        "  triage_started_at=NULL "
        "WHERE triage_status='processing' "
        f"  AND triage_started_at < NOW() - INTERVAL '{STUCK_AFTER_S} seconds' "
        "RETURNING id"
    )
    while True:
        await asyncio.sleep(60)
        try:
            async with get_session() as s:
                rs = await s.execute(text(sql))
                stuck = list(rs.scalars().all())
            if stuck:
                log.warning("Watchdog: %d stuck events returned to pending: %s",
                            len(stuck), stuck[:5])
        except Exception as e:
            log.warning("Watchdog error: %s", e)


async def _safe_rel_extract(event_id: int, body: str) -> None:
    """Fire-and-forget rel extraction; never crashes triage."""
    try:
        from vera_shared.graph.rel_extract import extract_and_store
        await extract_and_store(event_id, body)
    except Exception as e:
        log.debug("rel_extract event=%s failed: %s", event_id, e)


BACKOFF_MINUTES = [1, 5, 30, 120, 720]   # 1m, 5m, 30m, 2h, 12h → then dead
MAX_RETRIES = len(BACKOFF_MINUTES)


async def _retry_failed_loop() -> None:
    """Pick up 'error' events whose backoff window expired, re-pend them.

    Counter prevents flapping: each retry pushes next attempt further out.
    After MAX_RETRIES attempts, status='dead' — drops out of the loop and
    becomes visible in the dashboard as 'truly stuck, needs manual review'.
    """
    while True:
        await asyncio.sleep(120)
        try:
            async with get_session() as s:
                # Schedule retries: bump counter, push to pending, advance next_retry_at.
                # CASE picks the right backoff for the *next* (retry_count+1) attempt.
                bumped = await s.execute(text(f"""
                    UPDATE events SET
                      triage_status = CASE
                        WHEN triage_retry_count + 1 >= {MAX_RETRIES} THEN 'dead'
                        ELSE 'pending'
                      END,
                      triage_retry_count = triage_retry_count + 1,
                      triage_started_at = NULL,
                      triage_next_retry_at = CASE
                        WHEN triage_retry_count + 1 >= {MAX_RETRIES} THEN NULL
                        ELSE NOW() + (
                          (ARRAY{BACKOFF_MINUTES})[triage_retry_count + 2]
                          || ' minutes'
                        )::interval
                      END
                    WHERE triage_status = 'error'
                      AND triage_retry_count < {MAX_RETRIES}
                      AND (triage_next_retry_at IS NULL OR triage_next_retry_at < NOW())
                    RETURNING id, triage_retry_count, triage_status
                """))
                rows = list(bumped.mappings().all())
            if rows:
                live = [r for r in rows if r["triage_status"] == "pending"]
                dead = [r for r in rows if r["triage_status"] == "dead"]
                log.info("retry-loop: re-pended %d events (dead=%d)",
                         len(live), len(dead))
        except Exception as e:
            log.warning("retry-loop error: %s", e)


async def main_loop() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    log.info("[%s] brain-triage worker started, poll=%ss batch=%s concurrency=%s",
             WORKER_ID, POLL_INTERVAL_S, BATCH_SIZE, CONCURRENCY)

    asyncio.create_task(_watchdog_loop())
    asyncio.create_task(_retry_failed_loop())

    while True:
        try:
            n = await process_pending()
            if n == 0:
                await asyncio.sleep(POLL_INTERVAL_S)
        except Exception as e:
            log.exception("Outer loop error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_S * 2)


if __name__ == "__main__":
    asyncio.run(main_loop())
