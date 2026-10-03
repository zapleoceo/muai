# API

## Gateway (`vera3-gateway`, internal port 8000)

| Path | Method | Auth | Description |
|---|---|---|---|
| `/healthz` | GET | none | Liveness |
| `/event/{source}` | POST | `X-Internal-Secret` | Ingest endpoint — dedupes by `source_event_id` |
| `/webhook/{source}` | POST | source-specific | Webhook receiver (Telegram, etc.) |
| `/v1/claude/remember` | POST | `X-Internal-Secret` | Fact ingest from Claude conversations. Two-layer dedup: exact sha256 of text + semantic similarity (косинус) ≥ 0.92 over last 7 days of claude-source events. Body: `{text, kind: "fact"\|"decision"\|"todo"\|"preference", context?, tags?}`. Returns `{ok, event_id, deduped, dedup_reason: "exact"\|"semantic"\|null, similar_event_id?, similarity?}`. The dedup embedding is written into `event_embeddings` immediately on accept (2026-07-17) — closes the blind window where two similar facts saved minutes apart both passed semantic dedup because triage hadn't embedded the first one yet. Called by the `vera-mcp` MCP server (see `mcp-claude.md`). |
| `/v1/voice/session` | POST | `X-Internal-Secret` | Разговор с ноутбука: расшифровка одной сессии → выжимка в `events` (source=`voice`). В текст события (`content_text`, по нему триаж/поиск/эмбеддинг) идёт **выжимка**; дословная стенограмма с дорожкой и говорящим каждой реплики хранится в `content_extra` (`kind: voice_transcript`) и видна только в карточке `/events/{id}`. Тело: `{started_at, ended_at, app, window_title, device_hint, meeting_id?, part?, utterances:[{at, stream: mic|system, text}]}`. Длинная расшифровка **сворачивается по окнам, а не обрезается** (`gateway/voice_distill.py`): каждое окно осмысляется отдельно, второй проход сливает частичные выжимки в одну. Дедуп по `started_at+app+window_title`, поэтому ретрай из офлайн-очереди не двоит. Сбой брокера не теряет событие — сохраняется факт разговора с метаданными. Клиент: `vera-listener/` |
| `/v1/voice/command` | POST | `X-Internal-Secret` | Голосовое поручение владельца (кодовая фраза «Вера, мне нужна помощь, …», пойманная слушателем на дорожке микрофона). Тело: `{command_id, instruction, spoken_at, app?, window_title?}`. В одной транзакции пишет событие (source=`voice_command`, category=`command`, `triage_status=pending` — видно в истории и в поиске) и строку `voice_command_queue` (миграция 035); отвечает `{ok, event_id, deduped}`. Шлюз сам ничего не исполняет — ответ владельцу шлёт бот (см. ниже). Дедуп по `command_id` (PK очереди): ретрай из офлайн-очереди слушателя второго ответа не даёт. Без секрета — 401 и ни одной записи. Текст поручения в логах только на DEBUG. Срок годности — 30 минут от `spoken_at` (`voice_worker.MAX_AGE`): поручение, дошедшее позже (шлюз лежал, ноутбук без сети), не исполняется — владелец получает «Поручение дошло с опозданием (N мин), не выполняю: „…“». Код: `gateway/voice_command.py`, запись/очередь — `vera_shared/voice_commands.py` |

### Ответ на голосовое поручение (бот)

`bot_telegram/voice_worker.py` живёт в процессе бота рядом с aiogram-поллингом
и раз в 2 с забирает `voice_command_queue` (`FOR UPDATE SKIP LOCKED`, та же
схема, что `claude_session_queue`). На каждое поручение — два сообщения
владельцу: сразу «Услышала: „<поручение>“. Делаю.», потом ответ мозга. Ответ
идёт ровно тем путём, что на текстовое сообщение (`bot_telegram/brain.py`:
`/search` в brain-search с историей чата владельца, ответ пишется событием
`vera_chat`). Первым бот пишет только через `send_to_owner` — адресат
зашит в `OWNER_TELEGRAM_ID`, без него воркер не берёт из очереди ничего
(fail-closed, как `_owner_only`). Действий с внешним эффектом нет: поручение
— это вопрос мозгу.

Надёжность очереди:
- сбой ответа — до 3 попыток **с паузой** 30 с → 1 мин (`next_attempt_at`), не
  подряд; после третьей строка уходит в `error`. В колонку `error` пишется
  только тип/код ошибки — текст исключения может цитировать поручение;
- о каждом `error` владелец узнаёт отдельным проходом (`notify_failed`):
  «Не смогла выполнить поручение: „…“», после чего ставится `notified_at` и
  стирается текст поручения. Упала отправка уведомления — `notified_at` не
  ставится, и следующий виток повторит; одно неудачное не мешает остальным.
  Если ответ уже ушёл (`answered_at`), а упало что-то после, — «Не смогла»
  не шлётся: это было бы ложью поверх полученного ответа;
- поручение, на котором процесс падает (строка висит в `processing` дольше
  10 мин), возвращается в очередь с той же паузой; исчерпав попытки — `error`
  и сообщение владельцу, а не вечный круг;
- «Услышала» и ответ помечаются сразу после отправки (`acked_at`,
  `answered_at`), поэтому перезапуск после отправленного ответа только
  закрывает строку. **Гарантия — at-least-once**: дубль возможен лишь в окне
  между приёмом сообщения Telegram и записью отметки (процесс убит в эти
  миллисекунды, упала БД, либо ответ Telegram потерялся в сети и отправка
  считается неудачной). Потерять ответ хуже, чем прислать его дважды.

The body of `/event/<source>` is a `RawEvent` (`shared/vera_shared/events/schema.py`).
Note that the ingestors do NOT go through this endpoint — they write via
`vera_shared.ingest.insert_events()`; see [sources.md](./sources.md). The
endpoint serves webhooks and the bot's `vera_chat` writes.

```json
{
  "source": "telegram",
  "source_event_id": "tg:<chat>:<msg>",
  "account": "userbot",
  "category": "user|channel|group",
  "content_text": "...",
  "occurred_at": "2026-06-24T07:00:00",
  "metadata": { "chat_id": ..., "direction": "sent|received" }
}
```

### Internal auth (`vera_shared/auth.py::internal_secret_ok`)

Every gateway route that reads or writes data (`/event/*`, `/v1/claude/*`,
`/v1/search`, `/v1/events/*`, `/v1/entity/*`, `/api/events/{id}`) requires
the `X-Internal-Secret` header, checked by
`gateway/auth.py::check_internal_secret()`. `/healthz` is the only
unauthenticated route.

The comparison itself lives in **`vera_shared.auth.internal_secret_ok()`** —
gateway and brain-search each keep a thin `check_internal_secret()` wrapper
that turns `False` into their own `HTTPException(401)`, because the services
don't import each other and `vera_shared` deliberately doesn't depend on
FastAPI. Two properties are the reason it's one function:

- **Fail-closed.** If `INTERNAL_SECRET` is unset/empty, every request is
  rejected rather than let through. `docker-compose.yml` marks the var
  required (`INTERNAL_SECRET:?...`), so a misconfigured non-compose deploy
  locks down instead of exposing event bodies.
- **Constant-time.** `hmac.compare_digest`, not `!=`. Both copies used to
  compare with `!=`, whose runtime depends on the length of the matching
  prefix — the ports are loopback-only so this was never practically
  exploitable, but `dashboard/auth.py` already did it right and there was no
  reason for these two to differ.

## Brain Search (`vera3-brain-search`, internal port 8000)

| Path | Method | Auth | Description |
|---|---|---|---|
| `/healthz` | GET | none | Liveness |
| `/search` | POST | `X-Internal-Secret` | Hybrid retrieval + agent loop |

`/search` requires the same `X-Internal-Secret` header as the gateway,
checked by brain-search's fail-closed `check_internal_secret()` wrapper over
the shared `internal_secret_ok()` (the port is published on the host's
127.0.0.1, so any local process could otherwise query the whole memory).
Callers — bot-telegram, dashboard
`/search-ui`, gateway `/v1/search` proxy — all send the header.

The gateway's `MaxBodySizeMiddleware` also rejects POST/PUT/PATCH without
a `Content-Length` header (HTTP 411): chunked transfer-encoding used to
bypass the 2MB body cap entirely.

`POST /search` body:

```json
{
  "q": "сколько событий за неделю",
  "limit": 15,
  "use_agent": true,
  "max_steps": 6,
  "conversation": { "chat_id": 169510539 }
}
```

Returns `AnswerResponse` with `answer`, `results`, `provider`, `cost_usd`,
`agent_steps`, `agent_trace`.

## Dashboard (`vera3-dashboard`, internal port 8000)

| Path | Method | Auth | Description |
|---|---|---|---|
| `/login` | GET | none | TG Login Widget |
| `/api/tg_login` | GET | TG widget signature | Callback → session cookie |
| `/logout` | GET | none | Clear cookie |
| `/` | GET | owner cookie | «Поиск» — главная, поиск вперёд: статусная строка (точка зелёная/жёлтая/красная из `health.assess`, «N событий · +M за сутки», ссылка на `/sources`) и поле «Спросить Веру» (`POST /search-ui`). Карточек конвейера и живого прогресса здесь больше нет |
| `/events` | GET | owner cookie | «Входящее» — события по дням (Сегодня/Вчера/дата): время · иконка источника · кто (имя автора из `From`/`Author`, под ним приглушённо чат, канал или тема письма — не аккаунт ингеста) · текст (тело без заголовка, одной строкой с многоточием) · точка статуса. Вся строка кликабельна (`tr.row-link`, обработчик в `ui/js_core`) и ведёт на `/events/{id}`; Параметры: `q` (текст, ILIKE), `source`, `status`, `tech=1` (колонки техданных: id, важность, запрос к брокеру, модель, токены, цена — по умолчанию скрыты), `limit` (размер страницы, 50 по умолчанию), `before` (курсор `occurred_at_id` для «Показать ещё»: keyset-страницы по `(occurred_at, id)`, `cursor_of` / `parse_cursor`, без растущего лимита; битый курсор игнорируется; ссылка «← к новым» показывается, когда курсор задан — `cursor_given`). Текстовый поиск идёт под `SET LOCAL statement_timeout` в `SEARCH_TIMEOUT_S` = 5 с: при таймауте страница показывает «Слишком долгий поиск — уточните запрос». Заголовки дней Сегодня/Вчера/дата строит браузер по местному времени из `data-utc` у строк (`DAYS_SCRIPT`); без JS остаются серверные заголовки в UTC (`tr.day-fb`). Пакетно обработанные события в техданных помечены «в пачке» (см. `domain-model.md`). Маршрут прежний, поменялась подпись в меню |
| `/events/{id}` | GET | owner cookie | Карточка события (`event_card`): поля заголовка блоком «название — значение» (`header_pairs`: от, кому, тема, чат, направление; время — в поясе браузера), тело как читаемый текст с сохранёнными переносами строк, стенограмма голоса в сворачиваемом блоке (`transcript_html`), служебные поля — в сворачиваемом «Служебное» (`service_pairs`). Нет события — 404 |
| `/sources` | GET | owner cookie | Список источников — статусная точка (`source_level`), состояние потока, объём, действие; сворачиваемый блок «Конвейер обработки» (`PROGRESS_BLOCK`: живой прогресс `/_progress` раз в 30 с, пауза и лимит разбора). Строится из `source_registry`, не из ручной разметки |
| `/sources/{key}` | GET | owner cookie | Подробности источника: подключение, разбивки от провайдера `source_detail`. Источник без провайдера так и говорит |
| `/api/slack/start` | GET | owner cookie | Форма ввода user-токена Slack (`slack_start_form`) — со списком нужных прав |
| `/api/slack/start` | POST | owner cookie | Проверка токена через `auth.test` и сохранение в `slack_auth` под шифрованием (`slack_start`). Токен не логируется и в ответ не возвращается |
| `/api/sources/{key}/disconnect` | GET | owner cookie | Подтверждение отключения (`disconnect_confirm`): что именно погаснет и что события останутся |
| `/api/sources/{key}/disconnect` | POST | owner cookie | Погасить строки доступа источника (`disconnect_apply`). Секрет НЕ удаляется — шаг обратим |
| `/graph` | GET | owner cookie | «Люди» (`graph_page`, разметка — `graph_body`; `graph_css`, скрипт — `graph_script` из частей `graph_script_core`, `graph_script_panel`, `graph_script_connections`, `graph_script_ui`) — Cytoscape.js 3.30.2 (закреплён, SRI) на всю ширину страницы; поверх холста плавают поиск с подсказками по именам, «Фильтры» (связей не меньше, тип связи, «Раскрасить по темам», «весь граф»), легенда, зум и карточка человека. Наведение подсвечивает узел и соседей, остальное гаснет; клик плавно приближает узел и открывает карточку, двойной клик грузит окружение. В шапке «Дубли (N)» (`dupes_label`) и «Журнал правок». See "Graph visualizer" below. |
| `/api/graph/entity/{id}` | GET | owner cookie | Карточка сущности для боковой панели (`graph_entity` → `vera_shared.graph.panel.entity_panel`): имя, тип, @username / email / алиасы по источникам, счётчики (связей, групп, участников), `connections` — до двенадцати связей-пар, по одной на собеседника (`vera_shared.graph.connections.entity_connections`: `main` и `also` — роли с `weight`, `support`, `manual`, `inferred`, `direction` и русским `label` из `graph_labels.role_label`; `hidden`, `interaction` — дни и личка, `shared_work`, `possible_same`), и последние пять событий человека ссылками на `/events/{id}`. `?raw=true` добавляет `relationships` — записи по одной (`label` из `predicate_label`). 404 — нет сущности. События ищутся по алиасу источника (`recent_events`): telegram по индексу `ix_events_tg_sender`, остальные под таймаутом 2 с — при таймауте панель показывается без событий |
| `/api/graph` | GET | owner cookie | Node/edge JSON for the visualizer (`graph_data`). Params: `min_degree`, `limit` (≤800), `predicate`, `focus` (entity id), `q` (name→focus). Рёбра-факты — одно на пару людей (`connections_among`, `edge_payload`): `predicate` — главная роль, `weight` (толщина линии), `also`, `support`, `inferred`; членство, дублирующее пару со связью, не рисуется. Схема связи как пары — `identity.md`, «Связь как пара». |
| `/api/graph/connection/break` | POST | owner cookie + same-origin | Разорвать роль пары (`break_connection`): тело `{entity_a, entity_b, predicate, rel_ids}` (`BreakRole`; сервер проверяет, что все записи — ОДНА роль пары с этим каноническим предикатом), гасит записи `relationships` (`is_current=false`) через `connection_actions.break_role` — тот же `graph.edit.retire_relationship`, что у MCP, и строка `mcp_audit` с клиентом `dashboard`. Ответ `{ok, audit_ids}`; чужая или уже погашенная запись — 409 |
| `/api/graph/connection/reject` | POST | owner cookie + same-origin | «Это неверно» для выведенного «работает с» (`reject_connection`): тело `Pair` `{entity_a, entity_b}`, `connection_actions.reject_inferred` пишет пару в `connection_suppressions` (миграция 041) и в журнал. Ответ `{ok, audit_ids}` (пусто, если уже отвергнуто) |
| `/api/journal/undo` | POST | owner cookie + same-origin | Вернуть правки по `audit_ids` (`undo_edits`, тело `UndoRequest`): `journal.undo.undo_entry` на каждую, без `force`. Отказ откатa (`UndoRefused`) — 409 и список уже возвращённых |
| `/journal` | GET | owner cookie | «Журнал правок» (`journal_page`, разметка `journal_body`, `entry_html`, `describe_entry`): последние 80 записей `mcp_audit` (дашборд и агенты MCP) с кнопкой «Вернуть»; имена людей — `entity_ids` + `entity_cards`, всё экранируется. Строки читает `journal.audit.recent_rows` |
| `/ui/vera.css`, `/ui/vera.js` | GET | none | Статика дизайн-системы (`vera_css`, `vera_js`): адрес с `?v=<хэш>`, `Cache-Control: immutable`. Данных в них нет, поэтому без входа — ими оформлена и страница входа |
| `/api/instagram/start` | GET | owner cookie | Instagram login form (`instagram_start_form`) |
| `/api/instagram/start` | POST | owner cookie | Submit username/password (`instagram_start`) — may return a 2FA/challenge code form |
| `/api/instagram/verify` | POST | owner cookie | Submit 2FA/challenge code (`instagram_verify`) → saves encrypted session |
| `/tokens` | GET | owner cookie | Now redirects to AIbroker — see `llm-broker.md` |
| `/entities/merge-email-dupes` | POST | owner cookie | Слить дубли по рабочему email (`entities_merge_email_dupes`) — детерминированные пары, группы 3+ не трогаются |
| `/search-ui` | POST | owner cookie | Обработчик «Спросить Веру»: ответ модели через `render_markdown` (безопасное подмножество: жирный, курсив, `код`, списки, ссылки только http(s); всё остальное экранируется до разметки) плюс до пяти источников (`sources_html`: ссылка `/events/{id}`, человеческая строка, дата, фрагмент тела) из поля `results` ответа brain-search. Источники одного события или одной цепочки писем подряд не повторяются (`dedupe_key`) |

### Readable event text (`dashboard/event_text.py`, `dashboard/ui/markdown.py`)

`events.content_text` — это заголовок «Ключ: значение» (`Author`, `From`, `To`,
`Subject`, `Chat`, `Where`, `Date`, `Direction`), строка `---` и тело. Показывать
его сырым нельзя, поэтому `parse_content` делит текст на `ParsedText` (заголовки
и тело; у фактов Claude, памяти агента и голоса заголовка нет — всё тело), а
`describe` строит `EventLine`: кто (`From`/`Author` без `[роль]`, `<mail>` и
`(@ник)` убирает `clean_name`; без заголовка берётся `author_label` из метаданных), где
(чат, канал, тема письма, окно) и тело. Тему цепочки сравнивает
`normalize_subject` (без `Re:`/`Fwd:`). Список источников под ответом
(`headline`, `dedupe_key`), строки «Входящее» (`one_line` — тело в одну строку) и
карточка события (`kv_block` — блок «название — значение») берут поля оттуда,
а не режут сырой префикс.

`render_markdown` — единственное место, где текст модели превращается в HTML:
экранирует всё, затем включает только свои теги.

### Graph visualizer (`dashboard/graph_routes.py`, `graph_page.py`, `graph_script.py`)

`/graph` renders Vera's L1 substrate (entities + relationships) as an
interactive force-directed graph via Cytoscape.js (CDN, same pattern as
htmx). The full graph is ~8k entities / ~7k edges — a hairball if drawn at
once, and ~6k of those entities have no relationships at all — so it never
renders "everything":

- Default = the **connected core**: `repo.graph_snapshot(min_degree, limit)`
  returns the top-`limit` (≤`GRAPH_MAX_NODES`=800) entities by degree with
  degree ≥ `min_degree`, plus every edge whose both endpoints are in that
  set (so the client never references a missing node).
- Tap a node (or search by name) → **ego network**: `graph_snapshot(focus_id)`
  returns that entity + its 1-hop neighbours. Name search resolves via the
  fuzzy `find_entity_by_name`.
- `predicate` filter narrows to one relationship type. The dropdown shows
  Russian labels («работает с», «состоит в», …) with a hint tooltip, sorted
  alphabetically by label (a fixed list of ~13 is easier to scan by name than
  by shifting frequency); the option value stays the raw code. The mapping
  lives in one place, `dashboard/graph_labels.py`: `PREDICATE_LABELS`,
  `predicate_label` (unknown codes fall back to the code without `_of` and
  with underscores as spaces — never blank), `predicate_hint`,
  `predicate_options_html`, `predicate_labels_json` (the same labels are
  injected into the page JS, so tapping an edge shows «A — label — B»).
  Node colour = entity
  type (person / group / channel), size ∝ degree.

**Промахи мышью (исправлено 2026-10-04).** Cytoscape считает позицию указателя от
КЭШИРОВАННОГО положения контейнера на странице (`containerBB`) и сбрасывает кэш
только на `resize`/`scroll` окна. Любой сдвиг вёрстки выше холста (строка-подсказка,
легенда, раскрытые «Фильтры») оставлял кэш устаревшим: на проде кэш держал top=282 px
при реальных 391 px, и курсор «видел» узлы на 109 px выше. Вторая причина — боковая
панель отнимала у холста ширину (flex) без `cy.resize()`: контейнер 740 px, холст 1198 px,
граф рисовался обрезанным. Лечение: холст абсолютный внутри фиксированной по высоте
сцены, панель/поиск/легенда плавают поверх и размер не меняют; `ResizeObserver` зовёт
`cy.resize()`; кэш сбрасывается на `pointerenter`/`pointerdown`/`wheel`/`touchstart`/`scroll`
(`invalidateContainerClientCoordsCache`). Проверка в консоли страницы: для каждого
узла без соседей в радиусе послать `pointerenter` + `mousemove` в его экранные
координаты (`rect.left + node.renderedPosition().x`) и сравнить id из события
`mouseover` с ожидаемым; до правки на проде 0 из 44, после правки локально 68 из 68, в том
числе после искусственного сдвига страницы на 109 px и с открытой панелью.

Рендерер остался Cytoscape (canvas). На 300–800 узлах он держит 60 fps, а нативные
картинки-аватарки узлов, подсветка соседей и пунктир работают из коробки; WebGL
(sigma.js + graphology + force-layout) добавил бы три библиотеки без выигрыша на таком
размере. Подписи не сталкиваются за счёт `min-zoomed-font-size`: на дальнем плане
подписаны только «хабы» (верхние 12% по degree), остальные — при приближении и при наведении.

**Разрыв связи.** В карточке у каждой роли пары (главной и «также») есть «✕ Разорвать»
(если у роли есть записи `rel_ids`) и «Это неверно» (если роль выведена из общения).
Действие идёт через диалог подтверждения (`VeraUI.confirm`: «Связь Ли — супруг(а) — Дима
будет погашена; вернуть можно в журнале»), затем POST из таблицы выше; тост «Вернуть»
вызывает `/api/journal/undo`, те же записи видны на `/journal`. Кнопки несут только индексы
(`data-conn`, `data-role`) массива связей карточки, поэтому подмена разметки не подставит
чужие id. Защита от CSRF: старые формы дашборда держатся лишь на `SameSite=Lax`
сессионной cookie; новые POST дополнительно требуют `Sec-Fetch-Site: same-origin` либо
`Origin`, равный `Host` (nginx дашборда `X-Forwarded-Host` не ставит, ему не доверяем) (`csrf.same_origin_or_403`) — чужая страница
подделать эти заголовки не может.

Two different degrees are in play, deliberately: node **selection** uses the
degree *within the active predicate filter* (the `degree` CTE), while the
degree **shown on a node** is its total — every relationship plus every
current membership, both sides, unfiltered. The displayed number answers
"how connected is this person", not "how many edges survived the filter".

That total is one grouped query over the returned id set. It used to be two
correlated subqueries per row, i.e. up to 800 × 2 per page render, and half
of them keyed on `memberships.child_entity_id`, which had no index at all —
`ix_membership_child`, migration 028. Both sides of `relationships` were
already indexed; memberships only had the parent side, and `uq_membership`
couldn't stand in for it because `parent_entity_id` leads that constraint.

All graph SQL lives in the `vera_shared.graph` package; **no service
reaches past it**. `gateway/query.py` used to join `relationships` with
`entities` inside the route function and `ingestor_telegram/roster_sync.py`
joined `entity_aliases` with `entities` in the worker — both now call
`repo.list_relationships()` / `repo.find_project_chats()`, and
`tests/unit/test_graph_boundary.py` fails the build if a service grows raw
SQL against a graph table again. Routes only shape JSON / HTML.

Within the `graph/` package itself raw SQL is fine and deliberate:
`merge_entities`, collision handling and dossiers are not expressible as
repository CRUD, and wrapping them would hide transactional logic. The
point of the boundary is that swapping the store is an edit to one package. `IN` clauses use expanding bindparams
so the queries run on both Postgres (prod) and SQLite (tests).

### Stats caching (`dashboard/stats.py`)

`/` and `/sources` used to run ~15-17 heavy `COUNT`/`GROUP BY` scans per
page load (and again every `/_progress` poll) — the main cause of slow
page loads before this. `get_stats()` collapses
those into ~2 scans total via `FILTER` aggregates, cached for
`TTL_S=60` with stale-while-revalidate (`_serve_cached`): a stale value
is returned instantly while a background refresh (`_bg_refresh`) runs, so
the heavy scan almost never blocks a request. `cache_age_s()` reports how
old the cached value is, for the "updated N sec ago" note in the UI.

Страница источников разделена на два уровня, и ни один не знает имён
источников: `get_sources_overview()` — один `GROUP BY source` на весь список,
`get_source_detail(key)` — разбивки одного источника, по требованию и с
отдельным кэшем на каждый (скан по 400 тыс. строк telegram незачем повторять
на каждый показ). `drop_detail_cache(key)` сбрасывает кэш после
переподключения, иначе страница ещё минуту показывала бы «не подключено».
Сами разбивки собирает `blocks_for()` из `source_detail`.

## Ingestor-telegram tools (`vera3-ingestor-telegram`, port 8000)

X-Internal-Secret required on all `/tools/*`.

| Path | Method | Description |
|---|---|---|
| `/healthz` | GET | Liveness |
| `/tools/spec` | GET | JSON-Schema list (consumed by agent loop) |
| `/tools/list_dialogs` | POST | `{q?, limit?}` |
| `/tools/get_chat_info` | POST | `{chat_query}` |
| `/tools/get_participants` | POST | `{chat_query, limit?}` |
| `/tools/get_dialog_history` | POST | `{chat_query, limit?}` |
| `/tools/find_user` | POST | `{q}` |

### `/actions/send_message` — отправка ОТ ИМЕНИ ДИМЫ (не tool)

`POST {chat_id, text}` → `{ok, chat_id, message_id}`. Единственный путь, которым
юзербот пишет в чат как владелец. Сейчас один вызывающий — ежемесячный отчёт
бани (`scripts/banya_monthly_report.sh`, см. deploy-ops.md). Три независимых
замка (`ingestor_telegram/send_guard.py`):

1. Путь `/actions/*`, а не `/tools/*`: агент brain-search ходит только на
   `/tools/{name}` и в `/tools/spec` эндпоинта нет — LLM его не достанет даже
   угадав имя (тест `test_not_exposed_to_the_agent`).
2. Свой секрет `X-Send-Secret` == `TG_SEND_SECRET`, а не `INTERNAL_SECRET`
   (тот есть у всех сервисов). Пустой секрет — отказ всем (401).
3. Allowlist чатов `TG_SEND_ALLOWED_CHATS` (marked id через запятую). Чужой
   чат — 403; пустой список — отказ всем.

`check_send_request()` — проверки секрета/чата/текста (до 4000 символов),
`allowed_chats()` — разбор allowlist из env, `send_to_chat()` — отправка с
фолбэком: StringSession не хранит кэш сущностей, поэтому при «Could not find
the input entity» чат ищется обходом диалогов. Сбой отправки — 502.

## External (via Cloudflare → nginx :80 → :8003 dashboard)

Production URL: `https://dima.veranda.my`

nginx проксирует наружу не только дашборд: `/` → dashboard:8003, а `/event/`,
`/v1/` и `/webhook/` → gateway:8001. Именно поэтому ноутбук может слать
события и голосовые сессии по HTTPS — под `X-Internal-Secret`, без VPN и
туннелей. Всё остальное (brain-search, postgres) слушает только 127.0.0.1.

### Связь как пара в графе и карточке (2026-10-04)

Ребро `/api/graph` и строка карточки — не отдельный факт, а ПАРА людей целиком: главная
роль (с наибольшим весом), остальные роли выше порога («также»), скрытые считаются в
`hidden`. Вес роли складывается из числа подтверждающих записей, плотности общения
пары (`pair_stats`) и ручных правок; «работает с» может быть выведено из рабочих
контактов без единой фразы. Подписи — `graph_labels.role_label`: в карточке они
называют, КЕМ другой приходится смотрящему («начальник» / «подчинённый»), на ребре —
по направлению от «над» к «под». `graph_snapshot(raw_edges=True)` и `?raw=true` у
карточки отдают прежние записи. Модель, пороги и замеры — `identity.md`, «Связь как
пара»; накат таблицы и задача — `deploy-ops.md`.
