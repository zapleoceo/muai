# Office intake (приём задач от внешнего источника)

Минимальный общий API для внешнего источника (бот Veranda) поверх задач комнаты. Новых очередей и
планировщиков нет: источник кладёт задачу в комнату (`vera_shared.room.tasks.open_task`), исполнители
берут её как обычно, источник опрашивает свои задачи и отвечает на вопросы. HTTP-фасад живёт в
`vera3-mcp` под `/office/v1`; миграция `054_office_intake.sql`.

## Принципал и ACL

- Реестр — env `OFFICE_SERVICE_TOKENS` (`имя:проект:токен,…`, токен ≥ 32 символов), отдельный от
  `MCP_TOKENS` и `ROOM_TOKENS`. Разбор — `acl.parse_registry`, пересечение с другими наборами —
  `acl.check_disjoint`, поиск по токену — `acl.resolve`. При старте `auth.validate_office_tokens`
  отвергает битую запись, короткий токен и токен, совпавший с MCP/комнатой; на лету
  `auth.load_office_registry` при битом env отдаёт пустой реестр (все запросы 401).
- Принципал `OfficePrincipal(name, project, type="service", own_tasks=True)` определяется только
  аутентификацией; поля тела запроса его не задают. Проект задачи берётся из принципала.
- `acl.authorize` — deny-by-default: разрешены лишь действия `create`, `answer`, `list`, `delivered`.
  `acl.require_own` сверяет создателя и проект задачи; чужая задача отдаётся как «не найдена»
  (`IntakeNotFound`, 404), `OfficeForbidden` (403) — отказ по действию или флагам принципала.
- Сервисный токен на `/mcp` не совпадает ни с одним набором → 401; `_RealmDispatch` дополнительно
  отдаёт 403 для мира `office`. Инструментов `claim`/`post`/`room_*` у принципала нет.

## Модель данных

`room_task_origin` (`RoomTaskOriginRow`): ключ `(room, task_id)`, `UNIQUE(creator_principal,
client_task_id)`, `payload_hash`, `origin`, `result_recipient`, `context`, `attachments`,
`correlation_id`, `causation_id`, `delivered_revision`. Поля источника пишутся один раз
(`repository.insert_origin`, пути обновления нет); меняется только `delivered_revision`
(`repository.raise_delivered`, монотонно). `office_answer_keys` (`OfficeAnswerKeyRow`):
`UNIQUE(task_id, idempotency_key)`.

`task_id` = `vb-<uuid4>`, генерирует сервер. Ревизия задачи (`repository.revision_of`) — id последней
строки `room_task_events` этой задачи, тот же источник, что у MCP Events.

## Операции (`vera_shared.office.intake`)

| Функция | HTTP | Смысл |
|---|---|---|
| `create_task` | `POST /office/v1/tasks` | 201 новая, 200 повтор с тем же содержимым, 409 `payload_mismatch` при другом |
| `answer` | `POST /office/v1/tasks/{id}/answers` | только открытый вопрос своей незакрытой задачи; `expected_revision` + `idempotency_key` |
| `list_tasks` | `GET /office/v1/tasks?updated_since=&cursor=&limit=` | только свои; порядок и курсор `(updated_at, task_id)`, `limit` ≤ 100 |
| `mark_delivered` | `POST /office/v1/tasks/{id}/delivered` | монотонная отметка ревизии, повтор идемпотентен |

`create_task`: параллельный создатель того же `client_task_id` ловится `IntegrityError` по UNIQUE и
разрешается чтением (то же содержимое — существующая задача, иное — 409), дублей нет; если строка ещё
не видна — `IntakeBusy` (503, `retryable`). Повтор не создаёт сообщение и не трогает комнату
(`open_task` вызывается заново, только если задачи комнаты нет после сбоя). Флаг `OFFICE_AUTO_PICKUP`
(`intake.auto_pickup`, по умолчанию выключен) задаёт `auto_pickup` новых задач; комната — `OFFICE_ROOM`
(`intake.office_room`, по умолчанию `main`).

`answer`: повтор ключа с тем же ответом — тот же результат (`replayed`), с другим — 409
`payload_mismatch`. Отклонённая попытка (409 `revision_mismatch`) ключ не расходует: повтор с тем же
ключом и новой ревизией разрешён. В списке `list_tasks` у задачи есть `status`, открытые вопросы,
`result`, `revision`, `delivered_revision` и `lease_live` (задача `in_progress` с живой арендой).

Известное ограничение: ревизия сверяется до записи ответа, а не под блокировкой; два одновременных
ответа с разными ключами на один вопрос теоретически оба проходят проверку (вопрос после первого
уже не `open`, второй дописывается как дополнительный ответ).

## Поля запроса `create`

`client_task_id` (`tg-<chat>-<msg>`, `[A-Za-z0-9._:-]`, ≤ 128), `title` (≤ 200), `body` (≤ 8000),
`origin` {channel, chat, thread, message, from_id}, `result_recipient` {channel, chat, reply_to, thread},
`context` {reply_to {id, user, text, media}}, `attachments` (≤ 10: type, file_id, file_unique_id, mime,
size, duration, w, h, url, url_expires_at), `causation_id`. Значение `null` отвергается (422): клиент
опускает отсутствующий ключ. `payload_hash` считается по каноническому JSON присутствующих ключей
(`intake_input.payload_hash`) без `url`, `url_expires_at` вложений (`intake_input.stable_attachments`)
и без `causation_id`. Текст ответа-цитаты вычищается `redact_secrets` и режется до 2000 символов
(`intake_input.clean_context`). Проверки: `clean_ids`, `clean_text`, `clean_map`, `clean_attachments`,
`clean_limit`, `parse_since`, курсор — `encode_cursor`/`decode_cursor`, хеш ответа — `text_hash`;
ошибки проверки — `InvalidInput`.

## Медиа

Хранятся только метаданные вложений и необязательная короткоживущая подписанная ссылка с сайта.
Кода загрузки файлов нет: исходящий доступ из `vera3-mcp` к сайту требует отдельного согласования.

## Коды ответов

Все ошибки — `{error, retryable}` (`office_routes.error_response`):
400 `malformed_request` (не JSON), 401 `unauthorized`, 403 `forbidden`, 404 `not_found`, 409
(`payload_mismatch`, `revision_mismatch`, `task_closed`, `revision_ahead`), 422 `invalid_request`,
503 `in_progress` (`retryable`, `Retry-After: 1`), 500 `internal_error` (`retryable`). Лимита 429 нет.
Токен и тексты задач не пишутся в журнал: логируется только класс исключения.

Остальное: `repository.origin_by_client`, `origin_of`, `drop_origin`, `answer_key`, `insert_answer_key`,
`drop_answer_key`, `set_key_revision`, `result_text_of`, `own_tasks_page`; исключения `IntakeNotFound`,
`IntakeConflict`, `IntakeBusy`.
