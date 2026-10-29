# Office intake (приём задач от внешнего источника)

Общий протокол ответственности, решений и проверки результата:
[coordination-protocol.md](coordination-protocol.md). `delivered_revision` подтверждает
получение ревизии источником; факт отправки пользователю проверяется отдельно.

Минимальный общий API для внешнего источника (бот Veranda) поверх задач комнаты. Новых очередей и
планировщиков нет: источник кладёт задачу в комнату (`vera_shared.room.tasks.open_task`), исполнители
берут её как обычно, источник опрашивает свои задачи и отвечает на вопросы. HTTP-фасад живёт в
`vera3-mcp` под `/office/v1`; миграция `055_office_intake.sql`.

## Принципал и ACL

- Реестр — env `OFFICE_SERVICE_TOKENS` (`имя:проект:токен,…`, токен ≥ 32 символов), отдельный от
  `MCP_TOKENS` и `ROOM_TOKENS`. Разбор — `acl.parse_registry`, пересечение с другими наборами —
  `acl.check_disjoint`, поиск по токену — `acl.resolve`. При старте `auth.validate_office_tokens`
  отвергает битую запись, короткий токен и токен, совпавший с MCP/комнатой; на лету
  `auth.load_office_registry` при битом env отдаёт пустой реестр (все запросы 401).
- Проект сервисного принципала бота Veranda — ровно `Veranda` (с заглавной, конвенция комнаты),
  т.е. запись реестра `имя:Veranda:токен`. Доказательство: prod `room_tasks.project` (read-only
  SELECT) содержит `Vera`, `Orchestra`, `Lamas`, `Sintegrum`, `SinHRM` и т.д. — все с заглавной,
  значения `Veranda` пока нет. Регистр значим (проект задачи берётся из принципала), `veranda`
  создал бы отдельный проект. Замечание: `PROJECT_RULES` в `vera_shared/voice_help/policy.py`
  использует нижний регистр (`veranda`, `itstep`) — расхождение с конвенцией комнаты, здесь не правится.
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

Для Telegram сообщения владельца от авторизованного `veranda_bot` (`Veranda`),
когда `from_id` совпадает с ненулевым `OWNER_TELEGRAM_ID`, чат ответа совпадает
с исходным, а исходный чат — личный чат владельца либо включён в точный список
`OFFICE_OWNER_DISPATCH_CHATS`, задача получает `responsible=dot`,
`auto_pickup=true` и маркер `owner_dispatch: dot`. По умолчанию в compose
разрешена только группа `-1002884053196`, где был подтверждён тест владельца.
MCP получает `OWNER_TELEGRAM_ID` из существующей настройки compose; при пустом
или нулевом значении маршрут выключен.
Диспетчер выбирает из проекта `Veranda` только задачи с этим маркером и
`created_by=veranda_bot`; перед отправкой `verified_office_origin` повторно
проверяет неизменяемую запись `room_task_origin` через `is_owner_message`.
Захват задачи и изменение `refs`/`auto_pickup` не создают полномочия владельца:
неподтверждённая запись остаётся заблокированной без отправки dot.
Источник идентичности — цепочка Veranda: Telegram webhook с secret header,
проверки чата/автора/пересылки в `OfficeTaskService`, сохранённый
`origin_user_id` и сервисный токен `veranda_bot` при отправке в Office. Office
принимает `from_id` только как утверждение этого авторизованного сервиса; поле
само по себе не доказательство личности. Доставка в Room сама по себе не будит
неподключённый клиент: для живого исполнения нужна действующая подписка.

**Завершение и доставка.** Исполнитель ставит `done` только после проверенного
результата и записывает короткий результат в Room. Существующий Veranda
`OfficePoller` при `done` посылает этот текст ответом на исходное сообщение в
исходный чат/тему. Успешный Telegram send ведёт к локальной отметке
`result_delivered_at`; затем sender вызывает `/delivered` с ревизией, а Office
монотонно сохраняет `delivered_revision`. Повтор sender проверяет локальную
отметку и не посылает результат заново. `mark_delivered` без фактического send
не является подтверждением доставки; при ошибке send задача остаётся для
повтора, а блокер нельзя превращать в `done`.

`answer`: повтор ключа с тем же ответом — тот же результат (`replayed`), с другим — 409
`payload_mismatch`. Отклонённая попытка (409 `revision_mismatch`) ключ не расходует: повтор с тем же
ключом и новой ревизией разрешён. В списке `list_tasks` у задачи есть `status`, открытые вопросы,
`result`, `revision`, `delivered_revision` и `lease_live` (задача `in_progress` с живой арендой).

Ответ, событие журнала, outbox, сообщение комнаты, `updated_at` и ключ идемпотентности
фиксируются одной транзакцией (`questions.answer(..., s=сессия)` пишет в сессии вызывающего и не
фиксирует сам); сбой до коммита не оставляет ничего. Параллельные ответы сериализуются блокировкой строки задачи (`SELECT … FOR UPDATE`) до проверки
ревизии: второй получает 409 `revision_mismatch`. `answer` и `mark_delivered` (когда отметка выросла)
обновляют `room_tasks.updated_at`, чтобы `updated_since` их показал.

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

HTTP-фасад собирается в `office_app` (`vera_mcp/office_routes.py`) и монтируется рядом с `/mcp`.

## Передача токена бота Veranda (owner-run)

`infra/scripts/office_token_handoff.sh` запускает сам владелец из Git Bash на ноутбуке; агенты токен
и его хэш не читают и скрипт не запускают. Функции (для тестов) лежат в
`infra/scripts/office_token_handoff_lib.sh`, тесты — `tests/unit/test_office_token_handoff.py`.

- Токен создаётся один раз в `~/.vera_handoff/veranda_bot.token` (каталог 700, файл 600, >= 32 символов);
  повторный запуск переиспользует тот же файл.
- Шаг 1, Vera (`ssh hetzner-root`): в `/var/www/vera3/infra/.env` в `OFFICE_SERVICE_TOKENS` добавляется
  `veranda_bot:Veranda:<токен>`; остальные записи сохраняются; тот же токен — no-op; другой токен у
  `veranda_bot` — ошибка без перезаписи; токен не должен совпадать с `MCP_TOKENS`/`ROOM_TOKENS`
  (сравнение на сервере, печатается только yes/no). Затем `docker compose up -d --no-deps mcp` и проверка
  в контейнере (печатает `set`/`unset`).
- Шаг 2, сайт (`veranda_my_usr@5.101.179.132`): `OFFICE_API_URL` и `OFFICE_SERVICE_TOKEN` в
  `/var/www/veranda_my_usr/data/www/veranda.my/.env` одной атомарной заменой (обе пустые — добавляются; обе равны — no-op; любое отличающееся значение — отказ без изменения файла), затем `GET /office/v1/tasks` с хоста сайта;
  печатается только HTTP-код, ожидается 200. Cron и `aibot_office` не затрагиваются.
- Каждая правка `.env` атомарна: бэкап `.env.bak-<UTC>`, temp-файл в том же каталоге, права и владелец
  как у оригинала, `mv`. Секрет идёт только через stdin и нигде не печатается. Сбой любого шага — выход с
  понятным сообщением (fail closed); повтор безопасен.
