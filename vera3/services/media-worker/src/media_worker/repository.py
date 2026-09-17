"""Queue side of media-worker: claim/lease, finalize, retry policy."""
from __future__ import annotations

import json
import logging
import os
import re

from sqlalchemy import text
from vera_shared.control import is_backfill_paused, reserve_backfill_allowance
from vera_shared.db.engine import get_session

log = logging.getLogger("media-worker")

# Батч — он же ПРЕДЕЛ ПАРАЛЛЕЛИЗМА: с 12.09.2026 строки обрабатываются
# одновременно, поэтому один и тот же размер задаёт и сколько клеймим, и
# сколько фото держим в памяти и в полёте у брокера. Два, а не три: у vision
# в брокере ОДИН локальный слот, и двух потоков хватает, чтобы он не простаивал
# (третий всё равно ждал бы очереди); при этом пик памяти остаётся 2×124 МБ и
# укладывается в mem_limit, а всплеск запросов к облачным ключам — вдвое, а не
# втрое (openrouter отбивал 429 даже при подаче по одному).
BATCH = int(os.environ.get("MEDIA_BATCH", "2"))
# Лиз на захват должен покрывать ХУДШИЙ случай всего батча. С 12.09.2026 батч
# обрабатывается ПАРАЛЛЕЛЬНО, поэтому его длительность — не сумма, а максимум
# по строкам: одно фото на локальном vision ждёт до MEDIA_VISION_DEADLINE_S
# (900с) плюс скачивание (до 55с). Лиз изначально стоял на 10 минутах, и при
# последовательной обработке третье фото начиналось уже с протухшим лизом —
# его подхватывала соседняя реплика. Двойного текста не будет (finalize
# сверяет triage_status), но работа сгорала бы дважды. Запас держим прежний:
# он покрывает и возврат к последовательной обработке, и рост дедлайна.
LEASE_MIN = int(os.environ.get("MEDIA_LEASE_MIN", "50"))
MAX_MEDIA_RETRIES = 3
BACKOFF_MIN = [2, 15, 60]   # minutes for retry 1, 2, 3


#: Подстроки ошибок, после которых повторять бессмысленно. Один список на
#: оба решения — «сдаваться ли сразу» (`_plan_failure`) и «возвращать ли в
#: очередь потом» (`media_permanent` → `media_requeue.top_up`). До 12.09.2026
#: ошибки скачивания намеренно не были permanent («разовая осечка Telethon
#: мыслима»): замер за 4 часа после доливки дал 307 попыток на «Could not
#: find the input entity» и ноль успехов среди них — три круга на каждое
#: такое фото это чистый простой очереди. Пир, которого нет в сессии, не
#: появится в ней через 2 минуты; удалённое сообщение не вернётся.
_PERMANENT_MARKERS: tuple[str, ...] = (
    "broker_url",                         # брокер не сконфигурирован
    "empty text",                         # vision safety-block / пустой ответ
    "too large",                          # больше лимита — не влезет никогда
    "timed out",                          # зависший файл зависнет снова
    "could not find the input entity",    # пира нет в сессии Telethon
    "message not found",                  # сообщение удалено
    "no media on this message",           # медиа в сообщении уже нет
    "download returned none",             # ингестор не отдал файл (удалён)
)

#: Код ответа в тексте ошибки. Форматов у нас минимум три и они разъезжаются:
#: `http 413: audio > 25MB` (recognize.py), `broker 400: {"detail":…}` и
#: `broker poll 502: …` (shared/llm/broker_client.py), `broker whisper HTTP 413:
#: …`. До 17.09.2026 список маркеров держал только литералы вида «http 400»,
#: поэтому `broker 400` мимо него проходил: ошибка считалась временной,
#: `media_permanent` оставался false и `media_requeue.top_up` каждые три часа
#: возвращал событие в очередь. Замер за 48 часов до фикса: 4 события, 115
#: срабатываний `broker 400` и 1 `broker 413`, 39 деградаций — вечный круг.
#: Поэтому сопоставляем по СМЫСЛУ (код), а не по написанию: слово-маркер, до 20
#: не-цифр и трёхзначный код — так же ловится и четвёртый формат, если появится.
_STATUS_RE = re.compile(
    r"\b(?:http|https|broker|whisper|vision|poll|status|code)\b\D{0,20}?(\d{3})\b",
    re.IGNORECASE,
)

#: 4xx = запрос плохой сам по себе, повтор его не исправит. Исключение — 429:
#: это темп, а не запрос. 5xx тоже остаются временными (в частности 503 «no
#: provider»: ключи выходят из кулдауна за минуты).
_TRANSIENT_STATUSES: frozenset[int] = frozenset({429})


def _is_permanent_status(err: str) -> bool:
    match = _STATUS_RE.search(err)
    if match is None:
        return False
    status = int(match.group(1))
    return 400 <= status < 500 and status not in _TRANSIENT_STATUSES


def _is_permanent(err: str) -> bool:
    """Retrying won't help: degrade now and never re-queue."""
    e = err.lower()
    return any(marker in e for marker in _PERMANENT_MARKERS) or _is_permanent_status(e)


#: Постоянные ошибки, которые на ХОЛОДНОМ кэше Telethon выглядят так же, как
#: на удалённом сообщении. См. `_plan_failure`: одна повторная попытка.
_COLD_CACHE_MARKERS: tuple[str, ...] = ("could not find the input entity",)


def _is_cold_cache(err: str) -> bool:
    e = err.lower()
    return any(marker in e for marker in _COLD_CACHE_MARKERS)


# Kinds recognised via the vision pool (chat_async capability="vision") vs the
# separate whisper pool. When vision is circuit-broken we can still drain voice.
_VOICE_KINDS = ("voice", "audio")


async def _claim_batch(limit: int = BATCH, *, voice_only: bool = False) -> list[dict]:
    """Claim media_pending whose retry window is due.

    media_next_retry_at lives in metadata (jsonb) — NULL means never tried.
    Filtering by it means a failed event with a future retry time is SKIPPED,
    so the queue advances instead of looping on the first N forever.
    `limit` is trimmed by the backfill rate limiter. `voice_only` claims only
    voice/audio (whisper pool) — used while the vision circuit is open so a
    vision budget-cap doesn't also stall speech transcription.
    """
    if limit <= 0:
        return []
    kind_filter = (
        "AND metadata->>'media_kind' IN ('voice','audio')" if voice_only else ""
    )
    # Атомарный lease-claim: одним UPDATE проставляем media_next_retry_at на
    # LEASE_MIN мин вперёд у выбранных строк и их же возвращаем. Это (а) не даёт
    # второму инстансу/следующему поллу забрать те же события (claim атомарен
    # со сменой состояния, чего SELECT FOR UPDATE в отдельной транзакции не
    # давал), (б) если finalize упадёт — строка не зациклится, лиз оттолкнёт
    # следующую попытку на LEASE_MIN мин.
    async with get_session() as s:
        rs = (await s.execute(text(f"""
            UPDATE events SET metadata = jsonb_set(
                COALESCE(metadata, '{{}}'::jsonb),
                '{{media_next_retry_at}}',
                to_jsonb((NOW() + make_interval(mins => {LEASE_MIN}))::text)
            )
            WHERE id IN (
                SELECT id FROM events
                WHERE triage_status = 'media_pending'
                  AND (
                    metadata->>'media_next_retry_at' IS NULL
                    OR (metadata->>'media_next_retry_at')::timestamp < NOW()
                  )
                  {kind_filter}
                -- voice/audio вперёд фото: whisper-пул быстрый и дешёвый, а
                -- речь — самый ценный контент; vision медленный (free-tier
                -- ~130/сутки) и не должен морозить транскрипцию голосовых.
                -- Внутри каждого класса — newest-first (живые впереди бэклога),
                -- поэтому массовый requeue старых провалов не тормозит свежие.
                ORDER BY (metadata->>'media_kind' IN ('voice','audio')) DESC, id DESC
                FOR UPDATE SKIP LOCKED
                LIMIT :lim
            )
            RETURNING id, content_text, metadata
        """), {"lim": limit})).mappings().all()
    return [dict(r) for r in rs]


async def _on_success(event_id: int, append: str, extra_meta: dict | None = None) -> None:
    """Append recognized text + merge extra metadata (how the media was
    recognized: media_recognition=ok_local|ok_broker — voice AND photo).

    Guard triage_status: воркер, переживший lease (другой инстанс уже
    обработал и перевёл в pending), не должен приклеить текст ВТОРОЙ раз."""
    async with get_session() as s:
        res = await s.execute(text("""
            UPDATE events
            SET content_text = content_text || :app,
                triage_status = 'pending',
                triage_error = NULL,
                metadata = (COALESCE(metadata, '{}'::jsonb) || CAST(:extra AS jsonb))
                           - 'media_job_id'
            WHERE id = :id AND triage_status = 'media_pending'
        """), {"app": append, "extra": json.dumps(extra_meta or {}),
               "id": event_id})
    if (res.rowcount or 0) == 0:
        log.warning("media %s: already finalized elsewhere — append skipped", event_id)


def _plan_failure(meta: dict | None, err: str) -> dict:
    """Pure decision: given prior metadata + an error, decide degrade-vs-retry.

    Returns a plan dict (no DB, no clock) so it's unit-testable:
      degrade=True            → hand to normal triage now (placeholder kept)
      degrade=False           → schedule a backoff retry
      retry_count, backoff_min, action(for logs)
    Degrade when the error is permanent OR the next attempt would be the
    Nth (MAX_MEDIA_RETRIES)."""
    retries = int((meta or {}).get("media_retry_count", 0))
    # Ошибка холодного кэша получает РОВНО одну повторную попытку: воркер
    # греет кэш пиров Telethon на старте (warm_entity_cache), но best-effort —
    # если ингестор ещё грузится, он сдаётся через 5 минут и идёт клеймить.
    # Пометить фото вечно-недостижимым по первой же осечке в этом окне —
    # потеря; вторая осечка через 2 минуты — уже факт.
    permanent = _is_permanent(err) and not (_is_cold_cache(err) and retries == 0)
    if permanent or retries + 1 >= MAX_MEDIA_RETRIES:
        return {
            "degrade": True,
            "action": "degraded(permanent)" if permanent else "degraded",
            "retry_count": retries,
            "backoff_min": 0,
        }
    backoff = BACKOFF_MIN[min(retries, len(BACKOFF_MIN) - 1)]
    return {
        "degrade": False,
        "action": f"retry#{retries + 1} in {backoff}m",
        "retry_count": retries + 1,
        "backoff_min": backoff,
    }


async def _on_failure(event_id: int, meta: dict, err: str,
                      carry_meta: dict | None = None) -> str:
    """Apply the failure plan. Degraded events keep their placeholder
    ([photo]/[voice: Ns]) and go to 'pending' so they still enter the brain —
    recognition is best-effort. Returns the action taken for logging.
    `carry_meta` — что попытка хочет передать следующей (media_job_id
    недосчитанной брокером джобы); пишется только на ветке ретрая."""
    plan = _plan_failure(meta, err)

    if plan["degrade"]:
        # media_permanent пишем В МЕТАДАННЫЕ, а не полагаемся на triage_error:
        # после деградации событие уходит в обычный триаж, и тот на успехе
        # ставит triage_error = NULL. Доливка очереди (scripts/media_requeue.py)
        # отсеивала недостижимое медиа именно по triage_error — и метки к тому
        # моменту уже не было. Итог: 468 событий, файлов которых физически нет
        # (сообщение удалено, пир не резолвится), крутились по три попытки
        # каждые три часа бесконечно, съедая четверть пропускной способности.
        # Метаданные триаж не трогает.
        async with get_session() as s:
            await s.execute(text("""
                UPDATE events
                SET triage_status = 'pending',
                    triage_error = :err,
                    metadata = jsonb_set(
                      jsonb_set(
                        COALESCE(metadata, '{}'::jsonb),
                        '{media_recognition}', '"failed"'
                      ),
                      '{media_permanent}', CAST(:perm AS jsonb)
                    )
                WHERE id = :id AND triage_status = 'media_pending'
            """), {"err": err[:300], "id": event_id,
                   "perm": "true" if _is_permanent(err) else "false"})
        return plan["action"]

    # Backoff retry. retry_count bound as text → cast to int inside to_jsonb
    # via CAST() (NOT '::int' — SQLAlchemy text() mangles '::' next to a bind).
    # next_retry_at computed server-side with make_interval(mins => …).
    async with get_session() as s:
        await s.execute(text("""
            UPDATE events
            SET triage_error = :err,
                metadata = jsonb_set(
                  jsonb_set(
                    -- media_job_id живёт, пока жива джоба брокера: carry
                    -- приносит его заново только для «still pending», любой
                    -- другой провал старую ссылку стирает.
                    (COALESCE(metadata, '{}'::jsonb) - 'media_job_id') || CAST(:carry AS jsonb),
                    '{media_retry_count}', to_jsonb(CAST(:cnt AS integer))
                  ),
                  '{media_next_retry_at}',
                  to_jsonb(
                    (NOW() + make_interval(mins => CAST(:backoff AS integer)))::text
                  )
                )
            WHERE id = :id AND triage_status = 'media_pending'
        """), {"err": err[:300], "cnt": plan["retry_count"],
               "backoff": plan["backoff_min"], "id": event_id,
               "carry": json.dumps(carry_meta or {})})
    return plan["action"]


async def _claim_limit() -> int:
    """How many media events this cycle may claim. 0 = skip (paused or rate
    budget spent); else the batch size capped by the even-tempo allowance."""
    if await is_backfill_paused():
        return 0
    granted = await reserve_backfill_allowance(BATCH)
    if granted is None:
        return BATCH
    return granted
