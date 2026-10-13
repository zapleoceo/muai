# Security

## Owner gate

`OWNER_TELEGRAM_ID=169510539` is the single privileged identity. Every
admin path checks against it:

- Dashboard `/login`: TG widget verifies HMAC; only user_id=OWNER passes.
- Bot DM: `bot.py` rejects any other sender.
- No "additional admin" concept anywhere. Don't add one without revisiting
  the threat model.

## Secrets at rest

| Asset | Storage | Encryption |
|---|---|---|
| Gmail refresh tokens | `gmail_accounts.refresh_token_enc` | Fernet (key from `TOKEN_SECRET`) |
| TG MTProto session | `telegram_sessions.session_string_enc` | Fernet |
| Instagram sessionid | `instagram_sessions.session_json_enc` | Fernet |
| Service-to-service | `INTERNAL_SECRET` env on every container | none (intra-host) |

`TOKEN_SECRET` itself lives in `.env` (mode 600). If it leaks, ALL of the
above need rotation.

## Gmail OAuth permanence

Stable refresh tokens need the OAuth app published in Google Cloud
Console (mode = Production). In "Testing" mode, Google invalidates
refresh tokens after 7 days of idle.

If revoked:
1. `scripts/gmail_oauth_helper.py` running in a container exposing
   `dima.veranda.my/start` route.
2. Owner opens that URL in their Chrome (already logged into all three Gmail).
3. Three clicks, three accounts, done.
4. Ingestor picks up the new refresh tokens next poll cycle (~5 min).

## Telegram userbot session

Don't share an `auth_key` between two clients (e.g. MCP client + Vera
userbot). Telegram silently routes updates to one of them, the other goes
deaf. Each client should be its own device — separate auth via SMS.

## Instagram session

instagrapi sessions are ban-prone (datacenter IP = suspicious). Mitigation:
keep request rate gentle (90s poll), reuse device fingerprint across
restarts (saved in session_json_enc), don't burst.

## Audit

`events` is append-only. No `UPDATE` or `DELETE` from outside
`triage_metadata`-related code. We don't have a dedicated audit_log table
yet — every important state change is observable via `events` of
source=`monitor` or `vera_memory`.

## Encryption key rotation

Never rotate `TOKEN_SECRET` without re-encrypting every encrypted column.
Procedure:

1. Stop all ingestors.
2. Decrypt every encrypted column with the OLD secret.
3. Re-encrypt with the NEW secret.
4. Update `.env`, restart.

There's no automated migration for this. If you ever need to do it, write
the script and commit it.

## Redaction of search output (`redact_view`)

`vera_shared/redact_view.py` hides secret *values* in what Vera hands to a
client or an LLM; stored events are never modified. `redact_view(text)`
replaces only the value span with `[скрыто]` (anchored patterns: `gh[pousr]_`,
`github_pat_`, `glpat-`, `xox[abprs]-`, `xapp-`, `sk-`, Stripe `sk_/rk_ live|test_`,
`AIza…` (Google), Telegram bot tokens `<id>:AA…` (also in `bot<id>:AA…` URLs),
`AKIA`/`ASIA`, JWT, PEM private keys (real or literal `
` newlines, `Proc-Type:`/
`DEK-Info:` headers, long base64 lines; without END only headers and base64 lines
are masked), `Authorization:` values, `Bearer <opaque>=16+`, env-style
`SECRET_KEY=`/`passwd=` (pure-digit values kept)/`aws_secret_access_key=`/`*_SECRET|TOKEN|PASSWORD|API_KEY=`,
label phrases `token|secret|password|пароль|токен|секрет|api_key` with up to 3
words before the colon (value 4+ chars; `null/none/true/false`, and with extra words also pure digits, URLs and lowercase words up to 6 letters, are kept), secret URL
query/fragment params incl. `X-Amz-*`, `key=` only when the value is `AIza…` or
20+ chars of `[A-Za-z0-9_-]` (`?key=page2` stays), OAuth `code` only next to
`state=`/`redirect_uri=` or in a fragment, `scheme://[user]:PASS@` userinfo incl.
empty user). UUIDs, bare hex, long digits, base64 without a label and ordinary URLs
with ids stay intact. `redact_tree(obj)` does the same for JSON-like values and masks
the whole value of a key that, normalized (camelCase to snake, `-` to `_`, lowercase),
equals or ends on a `_` boundary with: `secret`, `client_secret`, `token`,
`access_token`, `refresh_token`, `id_token`, `api_key`, `x_api_key`, `apikey`,
`password`, `passwd`, `private_key`, `authorization`, `cookie`, `credentials`
(`x-api-key`, `accessToken`, `Set-Cookie` yes; `token_count`, `max_tokens`,
`secret_santa` no; pagination keys `page_token`, `next_page_token`, `prev_page_token`, `continuation_token`, `cursor` are kept).

Presentation only, hooked at every MCP read tool (`search`, `recent_events`,
`get_event`, `list_sources`, `entity_find`, `entity_context`, `graph_neighbours`,
`timeline`, `event_participants`, `co_occurrence`, `audit_log`, and `sql_query`;
each SQL cell is redacted using its column name); gateway `/v1/search`
(`answer`, `results[].content_preview`,
`agent_trace`) and `/v1/events/recent`; brain-search `build_context` (before
the LLM), the final `answer`, `agent_trace`, `content_preview` and the agent
`search_events` excerpt. `identifier_hits` still runs on the raw text, so exact
identifier search keeps working. These views do not change stored events or
audit records. SQL redaction applies only to the returned rows; aliases that
hide a secret column name require the value to match a text pattern.

### Выдача: дашборд, комната, gateway

Та же `redact_view`/`redact_tree`, только на выходе; хранилище и аудит не меняются.

- **gateway** `GET /api/events/{id}`: `content_text` и `triage_metadata` (`redact_tree`);
  id, время, источник, статус не трогаются.
- **Дашборд**: карточка события (`event_card`: тело, заголовки, стенограмма
  `content_extra`, `metadata`, `triage_error`) и список/поиск «Входящее» (`event_row`)
  маскируют строку через `redact_event_row` до разбора и до `esc`. Панель графа
  (`panel_events.recent_events_status`: `snippet`, `subject`) и `event_links.timeline_events`
  (`content_preview`) маскируют текст до обрезки, чтобы обрезка не оставила половину ключа.
- **«Показать как есть»**: `?raw=1` на `/events/{id}` учитывает `owner_wants_raw` только при
  действующей сессии владельца (проверка `require_owner` повторяется внутри, а не
  берётся из маршрута); без сессии или с чужой cookie, а также при любом другом значении
  параметра отдаётся маска. Ответ карточки идёт с `Cache-Control: no-store`; сырой текст
  не логируется.
- **Комната агентов** (`room_inbox`, `room_history`, `room_tasks`, `room_task_history`,
  `room_task_questions`; модуль `vera_mcp/room_redact.py`: `messages_view`, `tasks_view`,
  `events_view`, `questions_view`) маскируют только свободный текст: `body`; `title`, `note`,
  `next_action`, `last_progress_text`, `waiting_reason` и `refs[].excerpt`; `text` события
  журнала; `question` и `answers[].text`. `message_id`, `task_id`, `id`, `from`/`to`,
  `status`, `fencing_token`, поля аренды, время, `refs.kind/ref` остаются байт в байт:
  хранится и сверяется сырое, поэтому повтор `message_id` по-прежнему схлопывается, а
  `room_post` возвращает сообщение через `message_view`. Так же через `task_view`/`question_view`
  маскируют ответ все инструменты, возвращающие задачу или вопрос (open, claim, update,
  progress, state, wait, next, release, metadata_correct, handoff*, attested_complete,
  ask/answer_ack). В `event.data` маскируются `before`/`after`, `next_action`, `title`, `note`,
  `reason`, `result`, `question`, `text` и `refs[].excerpt`. Задачные инструменты живут в
  `room_task_tools.py`, общие типы в `room_common.py`; имена инструментов и порядок
  `ROOM_TOOLS` не менялись, `room_task_cancel` добавлен в конец. Ответ `room_task_cancel`
  идёт через `task_view`, причина (`note`, `event.text`, `event.data.reason`) маскируется как
  свободный текст. Отменить может только держатель живой аренды либо создатель задачи без
  живой аренды, с текущим `fencing_token`; права owner и dot не расширены.

## Граница LLM (`redact_input`)

Всё, что Vera отправляет внешнему провайдеру, маскируется `redact_view` на
единственной границе — `vera_shared.llm.client`: `chat`, `chat_async` и `embed`
получают `redact_input: bool = True`. Покрыты без правок вызывающих: brain-search
(история, синтез, агент), triage (одиночный и батч), `rel_extract`, `pair_roles`,
`identity`, `clusters`, `fold` (в нём же голосовая выжимка `voice_distill` и
`claude_distill` — второй проход по транскрипту не нужен, он тот же), vision.
Помощники `redact_messages(messages)` и `redact_embed_input(text)` лежат в
`vera_shared.llm.redact_boundary`: маскируют строковый `content` и части
`{"type": "text"}`, возвращают копии (вход не мутируется), части `image_url`/audio
и `data:` URI не трогают. `redact_input=False` — только осознанный opt-out.

Хранимые события остаются сырыми: FTS, `identifier_hits` и точные совпадения
считаются по ним до вызова, маскируется лишь исходящий текст (в том числе вход
embedding-эндпоинта). Лог тела ответа апстрима (`bot-telegram` `save_event`,
dashboard `search_routes` и `entities_routes`, `_short` в `broker_client`)
проходит через `redact_view`. Предел: эвристика ловит только помеченные формы
(`token=...`, префиксы ключей); пароль «в свободной форме» не маскируется.
Тесты: `tests/unit/test_llm_redact_boundary.py` (синтетический CANARY).

Следствия. Ответы LLM, записываемые обратно в БД (саммари triage, голосовая
выжимка), теперь содержат маску вместо секрета — так задумано. Проверка цитат
`pair_roles` сравнивает с сырым текстом, поэтому цитата с замаскированным
секретом не подтвердится. Аудио-загрузка whisper обходит текстовое маскирование
по замыслу. Также маскируются `tool_calls[].function.arguments` и части
`input_text`.
