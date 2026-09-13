# media-worker

Recognizes photo (vision) and voice/audio (whisper) for events with
`triage_status='media_pending'`.

Code layout (one responsibility per file):
- `recognize.py` — download, vision + whisper via broker, entity-cache
  warm-up (`warm_entity_cache`)
- `repository.py` — claim/lease, success/failure bookkeeping, retry policy
- `__main__.py` — the poll loop gluing the two

## Flow

1. ingestor-telegram saves a photo/voice/audio message with placeholder text
   (e.g. `[voice: 12s]`) and `triage_status='media_pending'` plus
   `metadata.media_kind`, `chat_id`, `msg_id`.
2. On startup the worker calls ingestor-telegram `POST /tools/list_dialogs`
   (`warm_entity_cache`, up to 30 tries × 10s): a fresh Telethon session
   resolves rare peers only after seeing them once — without the warm-up,
   `/media/download` on such peers fails with "Could not find the input
   entity". Best-effort: the worker starts either way.
3. media-worker polls pending events (batch of 3, every 10s), **voice/audio
   first, then newest** (`ORDER BY (media_kind IN ('voice','audio')) DESC,
   id DESC`). Speech goes through the fast, cheap whisper pool and is the
   most valuable content, so it is never stuck behind slow free-tier vision;
   within each class, live media (highest id) is claimed ahead of re-processed
   backlog (lower id), so a bulk requeue of old recognition failures can't
   starve fresh incoming messages — see "Re-recognising the backlog" below.
   While the **vision** circuit is open (budget cap / no provider), the loop
   claims `voice_only=True` — photos wait for vision to recover, but voice/audio
   keep transcribing through the separate whisper pool (which isn't capped), so
   a vision cap no longer stalls speech. `_claim_batch`
   does the claim as ONE atomic `UPDATE ... FOR UPDATE SKIP LOCKED ...
   RETURNING` that stamps a `MEDIA_LEASE_MIN`-minute (default 50)
   `media_next_retry_at` lease on the
   selected rows — a plain `SELECT ... FOR UPDATE` in its own transaction
   released the lock the moment the `SELECT` closed, before the row was
   actually processed, so a second worker instance (or the next poll
   tick, if finalize crashed) could re-claim and double-process the same
   event.
4. The claimed batch is processed **concurrently** (`asyncio.gather` over
   `_handle_row`), not row by row. Sequential processing capped throughput at
   one photo at a time — 145 s each, so 24/hour — while the broker's single
   local-vision slot sat idle 88% of the time (measured 2026-09-12 over 5 h:
   local 75 ok @150 s, gemini 27 ok @3.3 s, openrouter 0 ok / 42
   rate-limited). The queue was waiting on us, not on the broker.
   `_handle_row` swallows its own exceptions so one bad row cannot sink its
   neighbours and leave them claimed until the lease expires.
   **`MEDIA_BATCH` is therefore also the concurrency limit**, and it was
   lowered 3 → 2 when concurrency landed, for two independent reasons:
   *memory* — the per-photo peak (raw 25 + b64 33 + data-URI in the JSON 33 +
   httpx body 33 ≈ 124 MB at the 25 MB download cap) is now simultaneous, so
   `BATCH × 124 + 50 MB runtime` must fit `mem_limit` (2 × 124 + 50 = 298 MB
   under 384m; three would be 422 MB, over it — asserted by
   `test_batch_fits_the_container_memory_ceiling`); and *pressure* — the
   broker has ONE local-vision slot, so a third stream would only queue
   behind the other two and then spill onto cloud keys that already answered
   429 even under one-at-a-time submission. Two streams are what it takes to
   keep the local slot from idling. The lease also stops scaling with the
   batch: it now only has to cover the worst single row.
5. For each: POST to ingestor-telegram `/media/download` → bytes + mime.
   That endpoint downloads through `ingestor_telegram.media_download.
   download_capped()`, which enforces the 25 MB cap **before** any bytes
   move (via `msg.file.size` from message metadata, raising `MediaTooLarge`)
   and bounds the transfer with a 55 s timeout (`MediaDownloadTimeout`).
   Before this (2026-07-19) the file was buffered whole into RAM and the
   size checked afterward — a large Telegram audio (podcasts run to
   hundreds of MB, the format allows up to 2 GB) OOM-killed the ingestor,
   and because media-worker gives up after 60 s the server-side download
   was left orphaned, buffering into memory nothing was reading. Oversize
   / timeout now come back as errors classified **permanent** by
   `_is_permanent`, so the event degrades immediately instead of burning
   three pointless retries.
6. Photo → **broker `POST /v1/chat?capability=vision`** with OpenAI-style
   multimodal content (`text` block + `image_url` data-URI) and an
   OCR/caption prompt (Russian, 1-3 sentences + verbatim text under
   `Текст:` if readable). The broker picks a vision key (gemini →
   anthropic → openai) — no provider keys live in media-worker.
   The poll deadline for one photo is `MEDIA_VISION_DEADLINE_S`
   (default 900 s), not the client-wide 120 s: the broker can serve vision
   from a **local** model (`local/qwen3vl`), which is unlimited but slow —
   measured 42 s min / 117 s avg / 222 s max on 2026-09-01, so 5 of 8 local
   jobs used to overrun the 120 s ceiling. The broker still finished them;
   Vera had already given up, and the photo burned a retry and degraded
   after three rounds. The 420 s ceiling set that day stopped fitting within
   a week: by 2026-09-12 the model's median was 120–180 s and **57 jobs a
   day (16% of the work) hit 420 s and were abandoned** — 6.6 worker-hours
   thrown away. An abandoned job is worse than a slow one, hence 900 s.
   And even past 900 s the job is no longer lost: the deadline surfaces as
   `LLMJobPending(job_id)`, `_process_one` hands the id back as
   `carry_meta={"media_job_id": …}`, `_on_failure` persists it on the retry
   path, and the next attempt calls `_recognize_photo(resume_job_id=…)` —
   which polls the *same* broker job instead of resubmitting the image
   (`chat_async(resume_job_id=)`, see [`llm-broker.md`](llm-broker.md)).
   `_on_success` strips the key; the requeue's `top_up` drops it too, so a
   stale id can never leak into a refilled event. Measured 2026-09-12: three
   jobs waited 1 500 s in the broker's queue, were abandoned at 900 s and
   recomputed from scratch on retry.
   **Invariant:** `MEDIA_LEASE_MIN * 60 >= MEDIA_BATCH *
   MEDIA_VISION_DEADLINE_S` — the batch is processed sequentially, so a
   shorter lease would let a sibling replica re-claim the last row while
   it is still being recognised (`test_lease_covers_worst_case_batch`).
7. Voice/audio → **broker `POST /v1/transcribe`** (multipart upload).
   Whisper is hosted broker-side since 2026-07-18 (the local `asr-local`
   experiment was removed the same day — the backlog of ~3.3k failed
   voices had been cleared by a one-off local faster-whisper run,
   `media_recognition=ok_local`). Empty transcription (silence) becomes
   `(тишина/неразборчиво)`.
8. On success: append `\n--- recognized photo ---\n<text>` (or
   `voice transcription` / `audio transcription`) to `content_text`,
   set `triage_status='pending'` so normal triage takes over. For
   voice/audio the source is recorded in
   `metadata.media_recognition='ok_broker'`. Finalize SQL guards on
   `triage_status='media_pending'` so a worker that outlived its lease
   can't append the text a second time.

## Retry + degrade (no more queue stalls)

Recognition failures used to keep the event in `media_pending`; because
the claim is `ORDER BY id`, the same first-N events looped forever while
the rest of the queue (87k events at peak) never advanced.

Now each failure is tracked in metadata:
- `metadata.media_retry_count` — incremented per attempt
- `metadata.media_next_retry_at` — claim skips events whose window hasn't
  elapsed (backoff 2m, 15m, 60m), so the queue advances past failures

Outcomes:
| Kind | Result |
|---|---|
| Success | append recognized text, `triage_status='pending'` |
| Transient fail (broker 5xx/429, network) | `media_pending` + backoff, up to 3 tries |
| Permanent (4xx scope/bad-req, 413 oversize, empty text) | **degrade now** |
| After 3 transient tries | **degrade** |

**Degrade** = keep the placeholder (`[photo]`/`[voice: Ns]`), set
`triage_status='pending'` + `metadata.media_recognition='failed'`. The
event still enters the brain — recognition is best-effort, media is never
lost. When keys are added later, re-seed degraded events if desired.

## Env

- `INTERNAL_SECRET` — required, used to call ingestor-telegram
- `TELEGRAM_TOOLS_URL` — default `http://ingestor-telegram:8000`
- `BROKER_URL` — broker base, e.g. `https://aib.zapleo.com`
- `BROKER_PROJECT_KEY` — `aib_prj_…`; project must hold `llm:vision` +
  `llm:audio` scopes (set on the `vera` project)
- `MEDIA_POLL_S` (default 10), `MEDIA_BATCH` (default 3)
- `MEDIA_VISION_DEADLINE_S` (default 900) — per-photo broker poll ceiling
- `MEDIA_LEASE_MIN` (default 50) — claim lease; must cover
  `MEDIA_BATCH * MEDIA_VISION_DEADLINE_S`

No provider keys here — vision/whisper keys live in the broker. Whisper
audio is capped at 25 MB (mirrors the broker's limit); larger files degrade.

## Cost

Goes through the broker's free-first chains:
- Vision: gemini free → anthropic → openai
- Whisper: broker-hosted whisper → provider fallback (см. конфиг брокера)

## Media kinds

| Kind | Recognized? | How |
|---|---|---|
| photo / image (личка) | ✅ | vision |
| photo / image (группа, владелец в ней пишет) | ✅ | vision |
| photo / image (группа, владелец молчит) | ❌ | заглушка `[photo]`, причина `no_participation` |
| photo / image (вещательный канал) | ❌ | заглушка `[photo]`, причина `channel` |
| sticker | ❌ | заглушка `[sticker: <emoji>]`, причина `kind` |
| voice / audio | ✅ | whisper у брокера — отдельный дешёвый пул, работает везде |
| video / video_note / document | ❌ | причина `kind` |

Решение принимает одна чистая функция —
`media_policy.media_skip_reason(media_kind, chat_kind, own_messages=…,
min_own_messages=…)`; `should_recognize_media` — та же проверка одним булевым
ответом. Данные для неё приносит `chat_activity.own_message_count(chat_id)`
(кэш на час, `forget()` сбрасывает), порог — `chat_activity.min_own_messages()`,
то есть настройка `media_min_own_messages`, а не константа в коде. Тип чата
даёт `media_policy.classify_chat_kind(chat_type, is_megagroup)`: супергруппа
приходит от Telethon как Channel, и без второго аргумента её приняли бы за
вещательный канал.

Отфильтрованное всё равно попадает в мозг: событие сохраняется с заглушкой,
причина пишется в `media_skip_reason`, а признак участия — в
`owner_participates` (его же читает `should_extract_relations` из `graph/rel_policy.py`, чтобы не
строить граф связей по чужой публичной болтовне).

### Почему участие, а не список названий (2026-08-27)

С 2026-07-29 группы фильтровались денилистом по началу названия чата. Он
оказался и неверным, и дорогим в поддержке:

- **не поймал главного.** «Быть Или» — публичный канал (3 650 сообщений от
  одного автора) плюс его группа-обсуждение (27 748 сообщений от 1 792
  авторов). Владелец не написал там ни одного сообщения, а фото оттуда
  занимали 196 мест в очереди — четверть всей очереди. Фильтр каналов их не
  ловил: группа-обсуждение это megagroup, то есть `chat_kind == "group"`.
- **жил в трёх копиях**: список в Python, он же условиями SQL в кроне доливки,
  и третий в тестах. Комментарий «держать в синхроне» — признак, что
  синхронизировать нечего.

Замер очереди 2026-08-27 (782 фото): 65% из чатов, где владелец писал; **33%
из чатов, где он не написал ни разу**; 2% где почти не писал. Порог участия
по умолчанию — 5 своих сообщений: «Кайфушники Нячанга» (3 своих из 1735 при
235 авторах) отсекается, «Jakarta sales» (16), «BEER AI Нячанг» (17) и
«JAKARTA <> MARKETING HQ TEAM» (30) проходят.

Порог — настройка в дашборде, 0 означает «распознавать фото из всех групп».

### Два пути загрузки, одна политика

До 2026-08-27 `backfill.py` политику вообще не применял: своя захардкоженная
логика (любое фото → распознавать, статичный webp-стикер → в vision, хотя
политика говорит «стикеры никогда») и ни одного `chat_kind` в метаданных.
Отсюда 679 записей в очереди, про которые нельзя было сказать, из канала они
или из лички. Теперь оба пути — живой юзербот и бэкфилл — зовут одну функцию.

## Re-recognising the backlog

When recognition fails, the event **still lands in the brain** — `_on_failure`
degrades it to `triage_status='pending'` keeping the placeholder
(`[photo]`/`[voice: Ns]`) + metadata, and normal triage embeds/entity-extracts
it. Only the *recognised text* is missing; the message is never lost.

History left a large pile of such degradations (audited 2026-07-19): ~87.7k
events with `metadata.media_recognition='failed'`, ~79.5k of them recoverable
— they failed on transient capacity errors (`vision 503 no provider`,
`whisper 503`, `GEMINI_API_KEY not set`, transient 502s), which are now fixed
by the LLM circuit breaker + broker-hosted whisper. The unrecoverable tail
(~8.1k) is `Could not find the input entity` (peer not in session cache),
`message not found` (deleted), and `too large`.

Разгребает это `scripts/media_requeue.py` — той же политикой, что и загрузка.
Ручной SQL ниже оставлен как справка о том, что считается восстановимым
провалом; сам скрипт делает три шага за прогон (уборка `sweep`, пересмотр
`revisit`, доливка `top_up`):

```sql
UPDATE events e SET
  triage_status='media_pending', triage_error=NULL,
  metadata = (e.metadata - 'media_recognition' - 'media_retry_count' - 'media_next_retry_at')
WHERE e.metadata->>'media_recognition'='failed' AND e.triage_status='done'
  AND COALESCE(e.metadata->>'media_permanent','false') <> 'true';
```

**Почему признак живёт в метаданных, а не в `triage_error` (02.09.2026).**
Раньше фильтр стоял на `triage_error LIKE`, и он не работал ни дня.
Деградированное событие уходит в обычный триаж, а тот на успехе ставит
`triage_error = NULL` ([`brain_triage/worker.py`](../services/brain-triage/src/brain_triage/worker.py)).
К моменту доливки признака уже не было, фильтр видел пустую строку и
пропускал всё. Замер за 12 часов: **468 событий**, файлов которых физически
нет (сообщение удалено, пир не резолвится), брались по три попытки каждые три
часа — бесконечно, съедая около четверти пропускной способности воркера.
Теперь `_on_failure` пишет `metadata.media_permanent` по предикату
`_is_permanent`, а метаданные триаж не трогает. Предикат один на оба
решения — «сдаваться ли сразу» и «возвращать ли потом» — и с 12.09.2026
включает ошибки скачивания (`Could not find the input entity`, `message
not found`, `no media on this message`, `download returned None`). До этого
они намеренно ретраились («разовая осечка Telethon мыслима»): замер за 4
часа после доливки дал 307 попыток и ноль успехов — три круга на каждое
такое фото это чистый простой очереди. Исключение — `Could not find the
input entity`: на холодном кэше Telethon (воркер стартовал, а ингестор ещё
грузился, `warm_entity_cache` best-effort и сдаётся через 5 минут) она
выглядит так же, как на удалённом сообщении, поэтому получает ровно одну
повторную попытку через 2 минуты и только потом становится permanent
(`_is_cold_cache`). Легаси-провалы, у которых текст
ошибки ещё сохранился, помечены одним UPDATE (5 143 строки); 720 без текста
получат ровно один проход и будут помечены на нём.

The bottleneck is **vision-pool capacity** — free-tier vision clears only
~130 photos/day, so the 72k photo tail drains over months. This is safe and
non-disruptive because of the claim order (step 3): voice/audio drain first
through the fast whisper pool (~5.3k clear in days), live media always jumps
the queue within its class, the photo backlog chips away on spare vision
capacity, the circuit breaker paces it under the daily budget cap, and every
download is size-capped (no OOM). Крон на хосте раз в 3 часа гоняет `scripts/media_requeue.py`, и он держит
очередь на ~800 (`VERA_MEDIA_QUEUE_TARGET`):

1. `sweep` — выкидывает из очереди то, что политика больше не пропускает
   (каналы, стикеры, группы без участия). Событие остаётся в мозге с
   заглушкой, освобождается место в дефицитной очереди.
2. `revisit` — возвращает пропущенное по `no_participation`, если владелец в
   этом чате уже пишет: решение принимается по данным, а данные меняются.
3. `top_up` — доливает из восстановимых провалов, голосовые вперёд, дальше
   свежие фото, и только то, что политика пропускает. Политика стоит уже
   **в SQL** кандидатов (тип чата + участие владельца через тот же предикат,
   что `chat_activity.own_message_count`), а `_verdicts` остаётся
   окончательным судьёй. До 12.09.2026 запрос брал самые свежие провалы без
   разбора, а свежие — почти сплошь из чатов, где владелец не пишет: из
   2 184 кандидатов отсеивалось 2 149, доливалось 11–35 вместо 546, очередь
   стояла на ~250 при цели 800, воркер простаивал — при 8 457 подходящих
   провалах глубже окна.
   Успех у фото теперь тоже помечается `media_recognition=ok_broker` (раньше
   только у голосовых): по этой метке `top_up` отличает провал от
   сделанного, а `measure` считает прогресс. Без неё распознанное фото
   навсегда числилось несделанным — сайт показывал 20 612 при настоящих
   10 517. Исторические 9 130 фото с текстом, но без метки, добиты одним
   UPDATE по маркеру `--- recognized photo ---`.
4. `measure` — считает, сколько медиа политика пропускает ВСЕГО и сколько из
   них ещё не распознано, и кладёт оба числа в `app_control`
   (`media_backlog_total` / `media_backlog_left`). Дашборд читает их и только
   их: политика «какие чаты вообще распознаём» живёт здесь, а на странице это
   был бы второй скан `events` каждую минуту. Агрегация идёт по ЧАТУ, а не по
   событию — политика решает по чату, поэтому это сотня строк, а не десятки
   тысяч.

`--dry-run` показывает все четыре шага, ничего не меняя.

**Очередь — это рабочее окно, а не остаток.** Число в `media_pending`
пилит между ~235 и 800, потому что крон доливает его до цели каждые три
часа. Настоящий остаток — `media_backlog_left` (замер 02.09.2026: 19 101
фото при 442 в окне). Дашборд до этой правки показывал именно размер окна и
подписывал его «В очереди на триаж» — см. [`deploy-ops.md`](deploy-ops.md).
