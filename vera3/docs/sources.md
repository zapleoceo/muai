# Sources (ingestors)

Each source has its own container and writes to the same `events` table.

## Ядро ингестора — `vera_shared.ingest`

Источник приносит своё: транспорт к API, курсор, разбор полезной нагрузки в
спецификацию события. Всё остальное берёт из общего ядра, а не переписывает.

| Модуль | Что даёт |
|---|---|
| `writer` | `insert_events()` — запись событий с атомарным дедупом (`INSERT … ON CONFLICT (source, source_event_id) DO NOTHING`) и `valid_spec()` для отсева кривых спецификаций |
| `authors` | `sync_author_entities()` — автор события → сущность в графе; дедуп по identifier за прогон, сбой графа не роняет приём |
| `loop` | `poll_forever()` — цикл опроса, который не падает без ключа |
| `authorship` | `resolve_author()` — таблица «источник → автор события» для графа |

Появилось 2026-08-26 по итогам ревизии. До этого в репозитории лежали ТРИ
контракта источника — две абстрактные базы (`shared/vera_shared/sources/base.py`
и `shared/vera_shared/connectors/base.py`) и таблица `sources` — и ни одну из
них не реализовывал ни один ингестор; этот же файл при этом велел новому
источнику реализовать первую. Все три удалены (миграция 023).

**Записывают событие через ядро, а не через шлюз.** `gateway /event/<source>`
остаётся для вебхуков и записей бота (`vera_chat`); ингесторы пишут
`insert_events()`. Раньше каждый носил свою копию дедупа — `SELECT … IN (:ids)`
плюс `s.add()`, то есть check-then-insert. У gmail это была реальная гонка:
`scripts/gmail_backfill.py` вставляет тем же способом и ходит параллельно
поллеру, так что вместо безобидного дубля выходил `IntegrityError` и потеря
всей пачки транзакции.

### Таблица авторства (`ingest.authorship`)

`resolve_author(source, metadata)` отвечает, чей это текст, и у ответа три
состояния: алиас автора, `OWNER` (владелец), либо `None` — «источник знает про
чужое авторство, но автора не достал», и тогда связь скипается.

Раньше это была цепочка `if source == …` в `graph/rel_extract.py` с
`return владелец` в конце: источник без ветки целиком приписывался Диме, тихо,
без ошибки и без лога. Trello так и жил. **Новый источник обязан добавить строку
в `AUTHOR_RESOLVERS`** — иначе весь его входящий поток повиснет на владельце.
Источника нет в таблице = автор всегда владелец; так и должно быть для «своих»
источников (`vera_chat`, `vera_memory`, `perplexity`, `voice`, `claude`).

## telegram

- Container: `vera3-ingestor-telegram`
- Mechanism: Telethon userbot (MTProto), single StringSession for `@zapleosoft` stored encrypted in `telegram_sessions`.
- What it captures: every incoming + outgoing message in every dialog (DM, groups, supergroups, channels where the user is a member).
- Format: every message saved. Pure-text — as-is. Media — placeholder `[photo]` / `[voice:12s]` / `[video]` / `[sticker:😀]` etc. + `metadata.media_kind` + `media_meta`. Photo/voice/audio get `triage_status='media_pending'` so the media-worker (PR2) can download and run vision/whisper, then move to normal triage.
- Tools server: same container exposes `:8000/tools/*` for the agent loop (`list_dialogs`, `get_participants`, `get_chat_info`, etc.).
- Avatar backfill: `avatar_backfill.run_avatar_backfill(client)` runs as a slow background task on the same session — downloads entity profile photos (highest-degree first) into `entity_avatars` for the graph/dedup UI. Anti-ban: ~1 photo / `AVATAR_FETCH_INTERVAL_S` (default 4s), backs off on FloodWait, pauses with the owner's backfill switch (`is_backfill_paused`), and marks unresolvable/photoless entities `missing` so they're never retried. Env knobs: `AVATAR_BACKFILL_ENABLED`, `AVATAR_BATCH`, `AVATAR_FETCH_INTERVAL_S`, `AVATAR_IDLE_SLEEP_S`.
- History backfill: a one-shot queue walked every dialog back to 2025-06-01 (6067 dialogs, ~323k messages), completed 2026-06-29. The `backfill_jobs` queue, its worker, the seeder, and the dashboard `/backfill` page were retired afterwards (migration 009). Live ingestion covers everything since; to backfill again, re-apply migration 007 and restore the worker from git history.

## gmail

- Container: `vera3-ingestor-gmail`
- Mechanism: OAuth refresh + Gmail API polling every 5 min per account.
- Identity graph: собеседник каждого нового письма становится person-сущностью
  с alias `(gmail, email)` — см. `docs/identity.md`. Кросс-канальное слияние
  с telegram-сущностями — через LLM-предложения Веры на `/entities/duplicates`.
- Accounts: 3 (`demoniwwwe@gmail.com`, `zaporozec_d@itstep.org`, `zapleosoft@gmail.com`).
- Critical caveat: tokens get revoked by Google if the OAuth app sits in
  "Testing" mode for >7 days idle. See `security.md` for re-auth flow.
- Backlog-safe polling (2026-07-17): `poller.fetch_message_ids()` lists
  the whole backlog id-only (cheap, up to 10×`GMAIL_MAX_PER_RUN`),
  `poller.filter_new_ids()` drops ids already in `events` BEFORE the
  expensive per-message GET, then `poller.fetch_full_messages()` fetches
  at most `GMAIL_MAX_PER_RUN` (default 500) new ones. If more new mail
  remains, the run is *truncated*: `last_polled_at` is NOT advanced, so
  the next run re-lists from the same date and picks up the tail. The old
  code fetched the 500 NEWEST, jumped the cursor to today, and silently
  lost the older tail forever (`after:` is date-granular).
  `poller.fetch_messages()` remains as a thin list+get composition.

## instagram

- Container: `vera3-ingestor-instagram`
- Mechanism: `instagrapi` (unofficial mobile API).
- Auth: **dashboard admin UI** — `/sources` → "🔑 Подключить Instagram"
  button → `dashboard/instagram_login.py` (`instagram_start_form`,
  `instagram_start`, `instagram_verify`). Username/password login with
  inline 2FA/challenge-code handling (same flow shape as Stepan2's
  `ig_client.py`, no proxy — this is a personal account, not multi-tenant).
  On success the session (`cl.get_settings()`) is encrypted and upserted
  into `instagram_sessions`, `is_active=True`. The password is held only
  in an in-memory flow dict for the duration of the 2FA round-trip (TTL
  600s) — never logged, never persisted in plaintext.
- If there's no active session, `load_client()` raises `RuntimeError`;
  `main()` catches it and waits (re-checks every 10 min) instead of
  crashing — `restart:unless-stopped` previously crash-looped the
  container forever (RestartCount climbing unbounded) whenever the
  session was inactive, which is the common case between logins.
- `SessionDead` (raised from `poll_once()` on `LoginRequired`/
  `ChallengeRequired` mid-poll) marks the session `is_active=False` and
  exits the poll loop cleanly, instead of retrying against a dead session
  every `IG_POLL_INTERVAL_S`.
- Dedup batches one query per thread (`_existing_sids`) instead of one
  `SELECT` per message. `IG_MSGS_PER_THREAD` default raised 20→50 — a
  20-message window could miss messages in an active thread between polls.
- Tool: `[shared post]` / `[reel]` / `[voice]` / `[media]` placeholders for non-text.

## trello

- Container: `vera3-ingestor-trello`
- Mechanism: REST-опрос (`api.trello.com/1`), раз в `TRELLO_POLL_S` (по
  умолчанию 300 с). Ключ и токен — личные, из `infra/.env`
  (`TRELLO_API_KEY`, `TRELLO_TOKEN`); в БД секретов Trello нет.
- Доски: **все открытые доски владельца**. Новая доска подхватывается сама
  при ближайшем опросе, закрытая гаснет `is_active=false` — строка с курсором
  остаётся, чтобы возвращение доски не начинало историю заново.
- Курсор — `last_action_id`, **id действия, а не дата**. Trello отдаёт
  действия от новых к старым, поэтому date-курсор терял бы хвост при
  всплеске активности (та же грабля, что чинили у gmail с date-granular
  `after:`). Первый прогон по доске берёт `TRELLO_BOOTSTRAP_DAYS` (7) назад.
- Бэклог глубже одной страницы (1000 действий) разбирается пагинацией
  `before=<самое старое с прошлой страницы>` в том же прогоне, до
  `TRELLO_MAX_PAGES` (20). Если и этого не хватило — курсор **не двигается**,
  прогон помечается неполным и добирается в следующий раз. Молча пропустить
  середину нельзя.
- Что становится событием: `createCard`, `updateCard` (переезд между
  списками, срок, архив, переименование, описание), `deleteCard`,
  `moveCardToBoard`, `commentCard`, `addMemberToCard`/`removeMemberFromCard`,
  `updateCheckItemStateOnCard`, `addChecklistToCard`. Всё остальное — включая
  `updateCard` с одной лишь сменой `pos` — не событие: это служебный шум
  Trello, который раздул бы очередь триажа ни за чем.
- `source_event_id` = id действия Trello (глобально уникален), поэтому дедуп
  бесплатный и повторный обход безопасен.
- Авторство: `idMemberCreator == members/me.id` → `author_role=self`, иначе
  `counterparty` с `author_label` = полное имя участника.
- Identity: участник Trello → person-сущность с alias `(trello, username)`.
  Слияние с telegram/gmail-двойниками — существующим `/entities/duplicates`,
  своего дедупа у источника нет.
- Суточный дайджест: раз в сутки одно событие `digest:<дата>` — открытые
  карточки со сроками, ближайшие первыми, просроченные помечены. Снапшот
  карточек отдельными событиями сознательно не делается: правки и так
  приходят через actions-фид, а снапшот перезаписывал бы память шумом.
  Отметка «за сегодня собрано» живёт в `app_control` (`trello_digest_date`).

### Модули ingestor-trello

| Файл | Что делает |
|---|---|
| `client.py` | `TrelloClient` — только транспорт: ключи, пагинация, ретраи; `TrelloAuthError` на 401/403 (ретраить бессмысленно) |
| `describe.py` | `describe()` — действие → человекочитаемая строка (и `None` для шума); `category()` — грубая категория события (card / comment / member / checklist) |
| `mapper.py` | `action_to_event()` — упаковка в контракт `events` с авторством и хинтами; `parse_date()` — ISO Trello → наивный UTC |
| `store.py` | Весь SQL источника: `upsert_boards()`, `save_cursor()`, `save_events()` (событие + person-сущность автора через `vera_shared.ingest`) |
| `digest.py` | `build_digest()` и `digest_event()` — текст и событие суточного дайджеста; `due_today()` / `mark_done()` — отметка «за сегодня собрано» в `app_control` |
| `poller.py` | `fetch_new_actions()` — обход бэклога вглубь через `before`; `poll_board()` — прогон по одной доске и решение о курсоре; `run_digest()` — суточный дайджест; `main_loop()` — цикл опроса |

Состояние обхода — `TrelloBoardRow` (`shared/vera_shared/db/models_sources.py`,
таблица `trello_boards`, миграция 022).

## slack

- Container: `vera3-ingestor-slack`
- Mechanism: опрос Web API (`slack.com/api`) раз в `SLACK_POLL_S` (300 с).
  Токен **пользовательский** (`xoxp-`), не бот: Вера должна видеть то, что
  видит Дима, включая личку и приватные каналы, где бота нет.
- **Подключение — из дашборда**, а не правкой файла на сервере:
  `/sources/slack` → «Подключить» → `/api/slack/start`. Токен проверяется
  через `auth.test` ДО сохранения (иначе опечатка молча легла бы в БД, а
  поллер раз в десять минут писал бы «нет доступа» в лог) и хранится в
  `slack_auth` под `crypto` — как сессии telegram и instagram. Повторное
  подключение обновляет строку по `team_id`, а не плодит вторую: курсоры
  каналов при этом целы, история заново не поедет.
  Токен не логируется и в HTML не возвращается никогда.
- Отзыв токена гасит строку (`is_active=false` + `last_error`) — иначе дашборд
  показывал бы «подключено», пока в логе контейнера каждые десять минут «нет
  доступа». Порядок источников токена: активная строка `slack_auth`, иначе
  `SLACK_USER_TOKEN` из окружения (запасной путь, чтобы уже подключённый
  источник не отвалился от появления таблицы). Если таблицы ещё нет — деплой
  привозит код раньше, чем накатывается миграция, и это окно повторится с
  каждым новым источником — в лог идёт одна строка «накати миграцию 025», а не
  трейсбек asyncpg, и поллер берёт токен из окружения.
- **Лимиты — почему схема вообще жива.** С 29.05.2025 Slack срезал
  `conversations.history` и `conversations.replies` до 1 запроса в минуту и
  15 объектов за запрос — но только у приложений, распространяемых ВНЕ
  Marketplace. Внутренние (custom) приложения своего же воркспейса под это не
  попадают: им остаются 50+ req/min и `limit=1000`. Приложение внутреннее и
  **не публикуется**; если это изменится, опросная схема станет негодной и
  переходить придётся на экспорт.
- Права user-токена: `channels:history`, `groups:history`, `im:history`,
  `mpim:history`, `channels:read`, `groups:read`, `im:read`, `mpim:read`,
  `users:read`, `users:read.email`, `reactions:read`, `files:read`.
- Каналы: **все, где владелец состоит** (`users.conversations` —
  public/private/im/mpim). Новый канал подхватывается сам, покинутый гаснет
  `is_active=false`; строка с курсором остаётся, чтобы возвращение в канал не
  начинало историю заново. У лички своего имени нет — берётся имя собеседника.
- Курсор — `last_ts`, **`ts` последнего разобранного сообщения**: у Slack это
  одновременно время и идентификатор сообщения в канале, поэтому хвост не
  теряется при всплеске активности (та же грабля, что у gmail с date-granular
  `after:`). Первый прогон берёт `SLACK_BOOTSTRAP_DAYS` (7) назад.
- Бэклог глубже страницы разбирается пагинацией по `next_cursor` в том же
  прогоне, до `SLACK_MAX_PAGES` (20). Не хватило — курсор **не двигается**,
  прогон помечается неполным и добирается в следующий раз.

### Треды — обязательная часть, а не тонкость

`conversations.history` **не отдаёт ответы в тредах**: приходит только корневое
сообщение с `reply_count` и `latest_reply`. Больше того — тред, чьё корневое
сообщение старше курсора, в истории **не появится вовсе**, сколько бы новых
ответов в нём ни было. Поллер, который читает только историю, не увидит ни
одного обсуждения, а в Slack решения принимаются именно в тредах: в мозг попали
бы заголовки без содержания.

Поэтому корневые сообщения тредов берутся под наблюдение в `slack_threads`
(миграция 024), и ветки опрашиваются отдельно через `conversations.replies` со
своим курсором `last_reply_ts`. Два потолка, оба про расход вызовов, а не про
полноту: `SLACK_THREADS_PER_RUN` (20 тредов на канал за прогон, первыми — те,
что дольше всех не проверялись) и `SLACK_THREAD_WATCH_DAYS` (21 день с
последней активности). Ответ в старом треде приходит с задержкой, а не теряется.

### Что становится событием

- `source_event_id` = `<channel_id>:<ts>` — глобально уникален, дедуп бесплатный.
- Разметка Slack разворачивается в читаемый вид (`mapper.unwrap`): `<@U123>` →
  `@Имя Фамилия`, `<#C1|general>` → `#general`, `<https://…|текст>` →
  `текст (url)`. Без этого и выжимка, и поиск по мозгу работали бы по мусору.
- Не событие: служебные записи канала (`channel_join`, `channel_topic`,
  `pinned_item`, `huddle_thread` и прочие из `mapper.NOISE_SUBTYPES`) и всё, у
  чего есть `bot_id` либо `subtype=bot_message`. Это тот же класс шума, что
  `updateCard` с одной сменой `pos` у Trello.
- Файлы попадают строкой `[файлы] имя, имя`; реакции — в `metadata.reactions`.
- Авторство: `message.user == auth.test.user_id` → `author_role=self`, иначе
  `counterparty` с `author_label` = `real_name` (кэш имён в поллере, иначе
  `users.info` звался бы на каждое сообщение).
- Identity: автор → person-сущность с alias `(slack, user:<U…>)`. `sender_id` в
  метаданных — общая с telegram/instagram форма, на неё смотрит
  `ingest.authorship`.
- Денай-лист каналов: `ingest_policy.is_ignored_slack_channel()` — базовый
  список служебных названий (`alerts`, `ci`, `deploys`, `sentry`, …) плюс
  `SLACK_DENY_CHANNELS` под конкретный воркспейс. Личку денай-лист не касается.
  Нужен с первого дня: Slack — самая ботовая среда из подключённых, и без
  фильтра повторилась бы история с `@leomatchbot` (см. «Ingest denylist»),
  только объёмом больше.
- Хаддлы: аудио через API не отдаётся. Разговор в хаддле попадает в мозг
  слушателем как голосовая сессия (`slack.exe` уже в `VERA_ALLOW_APPS`), то
  есть текст Slack и голос Slack приходят двумя разными источниками и между
  собой не связаны.

### Модули ingestor-slack

| Файл | Что делает |
|---|---|
| `client.py` | `SlackClient` — только транспорт: токен, ретраи, 429 по `Retry-After`, пагинация. `SlackAuthError` на `invalid_auth`/`token_revoked`/`missing_scope` (ретраить бессмысленно), `SlackApiError` на остальное. Slack отвечает 200 даже на ошибку, поэтому разбирается тело, а не статус |
| `mapper.py` | `message_to_event()` — упаковка в контракт `events`; `unwrap()` — разметка Slack → текст; `is_noise()` — служебное и ботовое; `parse_ts()` — `ts` → наивный UTC |
| `store.py` | Состояние обхода: `upsert_conversations()`, `save_cursor()`, `watch_thread()`, `due_threads()`, `save_thread_cursor()`, `save_events()`; `kind_of()` — тип канала |
| `poller.py` | `poll_conversation()` — прогон по каналу и решение о курсоре; `poll_threads()` — догон ответов в наблюдаемых тредах; `Names` — кэш имён; `bootstrap_ts()`, `newest_ts()`; `main_loop()` |

Состояние обхода — `SlackConversationRow` и `SlackThreadRow`
(`shared/vera_shared/db/models_sources.py`, таблицы `slack_conversations` и
`slack_threads`, миграция 024).

## voice (ноутбук)

- Контейнера нет: приём в gateway — `POST /v1/voice/session` (см. `api.md`).
  Клиент — `vera-listener/` на ноутбуке (подробно: [listener.md](./listener.md)),
  слушает микрофон и системный вывод
  (WASAPI loopback), распознаёт локально и шлёт одну сессию под
  `X-Internal-Secret`.
- В `events` уходит **выжимка** (кто, через что, о чём, ход разговора, решения,
  договорённости, цифры, ключевые цитаты), а не дословная расшифровка.
  Осмысление делается на приёме (`chat:smart`, strict json_schema); сырой текст
  на сервере не хранится и удаляется на ноутбуке после отправки.
- **Длинная расшифровка сворачивается, а не обрезается** (`gateway/voice_distill.py`).
  До 2026-08-26 здесь стоял срез `[:60_000]` без лога и без отметки: по
  арифметике ≈12.6 символа на секунду речи это ≈80 минут суммарной речи по обеим
  дорожкам, тогда как предохранитель на ноутбуке отдаёт сессии до 120 минут.
  Терялся ХВОСТ — та часть, где «значит, договорились так», сроки и суммы.
  Теперь `windows()` режет расшифровку по границам реплик на окна по
  `WINDOW_CHARS` (35 тыс.), `distill()` осмысляет каждое и вторым проходом
  сливает частичные выжимки в одну. Не удалось слить моделью — склейка
  механическая (`_stitch`), событие всё равно полное. Аварийный потолок
  `MAX_WINDOWS` (12 окон ≈ 9 часов речи) ставит `truncated=true` в метаданные и
  предупреждение в лог: молчаливой потери больше нет.
- В метаданных видно работу осмысления: `transcript_chars`, `windows`,
  `truncated`, `distilled`, `merged` (`llm` либо `mechanical`), `parts`.
- Тело события собирает `voice.body_text()`: сводка, участники, темы, решения,
  договорённости, числа, ход разговора (`outline`) и цитаты. Именно этот текст
  и увидит поиск по мозгу.
- **Части одной встречи связаны.** Предохранитель по длительности
  (`VERA_MAX_SESSION_S`, 2 ч) режет трёхчасовую встречу, но следующая сессия
  продолжает ту же встречу: `meeting_id` общий, `part` растёт. Разрез по тишине
  или смене приложения — новая встреча. Без этого две половины лежали бы в
  мозге как два независимых события.
- Дедуп по `started_at+app+window_title`, поэтому повтор из офлайн-очереди
  не двоит событие.
- `nature` для источника задан детерминированно — `conversation_with_me`
  (`brain_triage/postprocess.py`): разговор у ноутбука ничем другим быть
  не может, гадать модели не о чем.

## vera_chat

- Not an external source. Bot writes user prompts AND Vera's replies here.
- Used by `brain-search` to retrieve the last N pairs as conversation context.

## vera_memory

- Not an external source. The agent loop's `memory.remember(fact)` tool writes here when it derives a non-obvious truth.

## perplexity (one-shot)

- `scripts/import_perplexity.py` — imports Perplexity MD exports as events.
- Source name = `perplexity`. Run once when there's a new bundle.

## Ingest denylist (в мозг не пишем)

`vera_shared.ingest_policy` — два уровня, оба применяются в
`userbot.save_message()` ДО записи события (в отличие от `media_policy`,
где событие сохраняется, но не распознаётся картинка):

- `is_ignored_sender(username)` — по автору сообщения;
- `is_ignored_chat(chat_username, chat_title)` — **весь чат целиком**, обе
  стороны переписки.

Матчинг по username (стабилен, в отличие от имени), регистр и ведущий `@`
не важны; для чата дополнительно по началу названия — у ботов username
есть не всегда.

**Почему нужен уровень чата.** 2026-08-02 в очередь распознавания хлынул
`@leomatchbot` (Дайвінчик, бот знакомств): ~800 анкетных фото в сутки, при
том что vision столько не переваривает в принципе — очередь выросла до 1366.
Чат приватный, поэтому ни правило про вещательные каналы, ни денай-лист
шумных групп его не ловили. Фильтра по отправителю тоже мало: исходящие в
таком чате — 👎-свайпы самого владельца, у них `sender_username` его
собственный. Дима: «весь чат в игнор поставь». Вычищено 4037 событий
(+3259 эмбеддингов каскадом), очередь 1366 → 561.

Сейчас в списке: `verandamybot`. Добавлен 2026-07-31 (Дима: «@VerandamyBot
исключи из мозга вообще») — служебный бот сыпал машинными уведомлениями в
рабочие чаты: 6050 событий («Веранда сотрудники» 5132, «VerandaBot» 736,
«Старшие и отчеты» 182). Для личной памяти это шум: раздувает базу, ломает
поиск, жжёт бюджет триажа, а сами факты есть в системе-источнике. Историю
вычистили (события + эмбеддинги каскадом + сущность #8604 с алиасами и
членствами); 2 сообщения ДРУГИХ авторов, где бот лишь упомянут, сохранены.

Чтобы добавить ещё источник:

- шумит только бот → username в `_IGNORED_SENDER_USERNAMES`, чистка
  `DELETE FROM events WHERE metadata->>'sender_username' ILIKE '<username>'`;
- мусорят обе стороны → `_IGNORED_CHAT_USERNAMES` (или
  `_IGNORED_CHAT_TITLE_PREFIXES`), чистка
  `DELETE FROM events WHERE metadata->>'chat_title' ILIKE '%<название>%'`.

`event_embeddings` уйдут по каскаду, `relationships.derived_from_event_id`
обнулится.

## Authorship contract (telegram / gmail / instagram / trello / slack / voice)

Every event from a conversational source MUST encode author unambiguously:

- `content_text` first line: `Author: <label> [<self|counterparty>]`
- `metadata.author_role` = `self` | `counterparty`
- `metadata.author_label` = `Я` (for self) | `@username` | from-address | fallback chat_title

This exists because `chat_title` in a personal chat = the *other* party, but a
`direction=sent` message in that chat is authored by the owner, not by the
counterparty. Consumers (the agent loop, dashboards, ad-hoc SQL) must look at
`author_role`, never at `chat_title`, to decide who wrote a message.

Migration that backfills both fields + the content_text prefix:
`infra/migrations/005_author_role.sql` — idempotent (guarded by
`content_text NOT LIKE 'Author:%'`).

## Adding a new source

Мера — последний добавленный источник: Trello (коммит `6a6bcf1e`) стоил 20
файлов и 1098 строк, из них ~250 строк были копипастой. Ядро
(`vera_shared.ingest`) эту часть забрало; ниже — то, что осталось.

**Своё:**

1. Новый сервис `services/ingestor-<name>/` по образцу `ingestor-trello` или
   `ingestor-slack`: `client.py` (только транспорт: ключи, ретраи, пагинация,
   свой `*AuthError` на отказ в доступе), `mapper.py` (чистая функция
   «полезная нагрузка → спецификация события», без сети и БД), `store.py`
   (только состояние обхода + тонкий `save_events()`), `poller.py` (курсор и
   `poll_forever()`).
2. Строка курсора в `shared/vera_shared/db/models_sources.py` + миграция.
3. Тесты: маппер отдельно (чистая логика), обход — на `sqlite_db`.

**Из ядра, не переписывать:** `insert_events()` вместо своего дедупа,
`sync_author_entities()` вместо своего «автор → сущность», `poll_forever()`
вместо своего `while True`.

**Обязательные правки в общих файлах** (ничто о них не напомнит):

4. `shared/vera_shared/ingest/authorship.py` — строка в `AUTHOR_RESOLVERS`.
   **Пропустишь — весь входящий поток источника повиснет на владельце**, тихо.
5. `infra/docker-compose.yml` — блок сервиса. Переменные объявлять со значением
   по умолчанию (`${VAR:-}`), а не как обязательные: `${VAR:?}` уронил бы весь
   `compose up`, то есть всю Веру, из-за одного отсутствующего ключа.
6. `.github/workflows/deploy.yml` и `vera3-tests.yml` — `pip install -e` и
   `PYTHONPATH`. Забудешь — тесты источника не запустятся, а гейт покрытия при
   этом пройдёт.
7. `docs/sources.md` (этот файл), `architecture.md`, `domain-model.md` —
   иначе docs-гейт блокирует деплой.

**Контракт события:** `author_role` + `author_label` в metadata и префикс
`Author:` первой строкой `content_text` (см. «Authorship contract» выше);
`source_event_id`, уникальный в пределах источника; `occurred_at` наивным UTC.

### Дашборд: источник появляется сам

Страница `/sources` больше не набирается вручную (до 2026-08-26 она держала
блок HTML на источник, и Trello своего блока так и не получил). Чтобы новый
источник появился в интерфейсе, хватит одной записи в
`dashboard/source_registry.py` — `CATALOG`: ключ, название, как получаем
данные, порог свежести, ссылка на подключение.

- Разбивки по источнику — необязательная функция в `dashboard/source_detail.py`
  плюс её имя в поле `detail`. Провайдер возвращает **блоки данных**
  (`rows_block` / `table_block`), а не разметку.
- **Экранирование — по умолчанию.** Страница экранирует каждую ячейку; готовая
  разметка помечается типом `Html` (`state_pill`, `dt`). Обратное правило
  («провайдер сам не забудет») дало бы XSS на первом же чате с названием
  `<script>…</script>` — названия чатов приходят из БД как есть.
- Источник, которого нет в каталоге, но события от него в базе есть, всё равно
  показывается строкой: скрыть его — значит соврать про содержимое мозга. Такая
  строка и есть сигнал «пора добавить запись».
