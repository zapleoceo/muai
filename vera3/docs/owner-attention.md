# Тихий режим владельца (owner attention)

Состояние внимания владельца хранится в БД (миграция `054_owner_attention.sql`, модели
`OwnerAttentionRow` и `OwnerDeferredRow` в `vera_shared.db.models_attention`) и переживает рестарт бота.
Флаг **`OWNER_QUIET_ENABLED`** (по умолчанию `0`): при `0` политика реализована, но не действует,
бот шлёт всё сразу. Время считается в Asia/Ho_Chi_Minh (UTC+7, без летнего времени); в БД, как везде, наивный UTC.

## Состояния

| Состояние | Что значит |
|---|---|
| `active` | всё отправляется сразу, ничего не копится |
| `wait10` | владельцу задан вопрос; срок = отправка + 10 минут (`WAIT_FOR`) |
| `quiet` | проактивные уведомления и вопросы откладываются (`owner_deferred`), работа агентов идёт |

Переходы (`vera_shared.owner_attention.gate`):

- `active -> wait10` только при отправке **вопроса** (`deliver(..., kind="question")`: переспрос голосового
  поручения). Статусы и тревоги (`status`, `alert`) таймер не запускают. Вопрос ставит таймер после успешной отправки.
- `wait10 -> active`: любое сообщение или ответ владельца (`on_owner_message`: текст в Telegram, голосовое
  поручение, ответ в дашборде).
- `wait10 -> quiet`: срок истёк без ответа, и `timeout_goes_quiet(deadline)` истинно; `quiet_until = срок + 8 часов` (`QUIET_FOR`).
- `wait10 -> active`: срок истёк, но `timeout_goes_quiet` ложно.
- `quiet -> active` сразу, когда владелец написал или ответил (`on_owner_message`). Если передан `send` (бот), тут же уходит одна актуальная сводка (`_flush`); из дашборда (`send` нет) состояние становится `active`, а отложенное уходит одной сводкой на ближайшем `tick`: при `active` и неотправленных записях `tick` шлёт сводку.
- `quiet -> active` автоматически при `now >= quiet_until`: `tick` шлёт **одну** сводку (`_flush`) и помечает строки отправленными, повторный `tick` ничего не шлёт.
  Сводка доставляется как минимум один раз: сбой процесса между отправкой и `mark_sent` может дать дубль сводки (принято).

### Окно «после полуночи»

`timeout_goes_quiet`: срок истёк не раньше 00:00 местного дня отправки вопроса, то есть окно тянется до конца этого дня.
Необязательная верхняя граница - **`OWNER_QUIET_WINDOW_END`** (`HH:MM` местное; пусто = границы нет, срок в 14:00 уходит в тишину;
при `08:00` дневной срок возвращает `active`). Срок, перешедший через полночь (вопрос 23:55, срок 00:05), тихим считается только при
**`OWNER_QUIET_COUNT_CROSS_MIDNIGHT=1`** (предложенное правило, не утверждено, по умолчанию `0`). `quiet_until = срок + 8 часов`.

### Сводка

`_flush` берёт неотправленные записи, оставляет по одной (последней) на задачу (`task_id`, иначе по `dedup_key`),
отбрасывает записи, у которых вопрос уже не `open` или задача `done`/`cancelled` (`store.still_relevant`),
шлёт одно сообщение «Пока было тихо, накопилось:» и возвращает `active`.

## Точка включения

`gate.deliver(send, html, plain, kind, key, task_id, room, question_id)` - единственный путь проактивных
сообщений бота: `help_worker.say` (статусы/эскалации/напоминания), `voice_worker.notify_failed` (тревога),
переспрос (вопрос). Цикл `voice_worker.run_forever` вызывает `gate.tick` каждый проход (нового планировщика нет).
`gate.QUESTION` - вид «вопрос». Ответ на собственное слово владельца (ответ мозга) идёт напрямую: владелец только что был активен.
Вопросы владельцу из комнаты (`room_task_ask`, вопрос удержания `voice_help.hold.ask_owner_once`) пересылает `gate.forward_questions` из цикла `voice_worker.run_forever`: берёт открытые вопросы с `room_task_questions.delivered_at IS NULL` (колонка из миграции 054; существующие вопросы миграция помечает доставленными) и отправляет через `deliver(kind=QUESTION, key="q:<qid>")`, затем ставит `delivered_at` (повтор не шлёт). Таймер wait10 стартует только при реальной отправке; в тишине вопрос уходит в очередь отложенного без таймера. Пока вопрос не передан, `/tasks` показывает «ожидает доставки». При `OWNER_QUIET_ENABLED=0` вопросы в Telegram не пересылаются (поведение прежнее).

## Хранилище (`vera_shared.owner_attention.store`)

`Attention` (состояние), `Deferred` (отложенная запись), `load_state`, `save_state`, `defer`, `pending`,
`mark_sent`, `still_relevant`, `QuestionOut`, `undelivered_questions`, `mark_delivered`. Политика - `vera_shared.owner_attention.policy`: `quiet_enabled`,
`count_cross_midnight`, `window_end`, `to_local`, `hhmm`, `timeout_goes_quiet`, `WAIT_FOR`, `QUIET_FOR`, `LOCAL_TZ`.

## Дашборд `/tasks`

- Панель `dashboard.attention_view.attention_panel`: `active` / `wait10 (до HH:MM)` / `quiet до HH:MM`, флаг вкл/выкл, список отложенного.
- Таблица `dashboard.tasks_table.tasks_table`: ответственный, статус, последний реальный прогресс (`last_progress_at`, не heartbeat аренды),
  следующий результат (`next_action`), блокер (`waiting_reason` у `blocked`/`waiting`), контрольная точка (`next_checkpoint_at`).
- Доступ только владельцу (общий `owner_or_redirect`). Ответ на вопрос в дашборде вызывает `on_owner_message`.


## Не утверждено владельцем

`OWNER_QUIET_COUNT_CROSS_MIDNIGHT` и `OWNER_QUIET_WINDOW_END` - предложения, не утверждены; по умолчанию не действуют (выкл / не задано). В панели `/tasks` помечены «не утверждено». `gate.forward_questions` описан выше.
