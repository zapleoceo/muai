# Голосовые поручения

Владелец говорит во время звонка «Вера, мне нужна помощь, <поручение>». Вера
выполняет поручение и отвечает ему в Telegram. Как фраза ловится на ноутбуке и
как отсекается чужой голос — в [listener.md](listener.md); контракт входа — в
[api.md](api.md). Здесь — серверная часть: кто за что отвечает.

## Путь поручения

```
слушатель ──POST /v1/voice/command──▶ gateway ──▶ events + voice_command_queue
                                                         │
                                        bot-telegram ◀───┘ (опрос раз в 2 с)
                                              │
                               «Услышала: „…“. Делаю.» → ответ мозга → владельцу
```

Второй шины между сервисами нет: очередь — таблица в той же базе, по образцу
`claude_session_queue`. Строки забираются `FOR UPDATE SKIP LOCKED`.

## Модули

| Где | Что |
|---|---|
| `gateway/voice_command.py` | `accept_voice_command` — вход `POST /v1/voice/command`, тело `VoiceCommand`, ответ `VoiceCommandResult`. Проверяет `X-Internal-Secret`, сам ничего не исполняет |
| `vera_shared/db/models_voice.py` | `VoiceCommandRow` — строка очереди (миграция 035) |
| `vera_shared/voice_commands.py` | операции с очередью, см. ниже |
| `bot_telegram/voice_worker.py` | воркер бота: забирает поручение и отвечает владельцу |
| `bot_telegram/brain.py` | `ask_brain` — вопрос мозгу тем же путём, что и текстовое сообщение боту; ответ — `BrainAnswer`, сбой — `BrainError`. `save_event` пишет ответ Веры событием |

### Очередь (`vera_shared/voice_commands.py`)

- `create_command` — событие (текст — `event_text`) и строка очереди в одной
  транзакции; повтор того же `command_id` — дубль, второго ответа нет.
- `claim_command` — взять одну строку: `pending`, попытки не исчерпаны,
  `next_attempt_at` наступило.
- `mark_acked` — «Услышала» ушло; `mark_answered` — ответ ушёл. После
  перезапуска бот по этим отметкам не шлёт то же второй раз.
- `finish_command` — готово, текст поручения стирается.
- `fail_command` — неудача: пауза `retry_delay` (30 с, потом удвоение) или,
  после трёх попыток, `error`.
- `revive_stale` — строка, зависшая в `processing` дольше 10 минут (бот упал),
  возвращается в очередь или уходит в `error`.
- `pending_notifications` / `mark_notified` — ошибки, о которых владельцу ещё
  не сообщили, и отметка, что сообщили.

### Тексты владельцу (`bot_telegram/voice_worker.py`)

- `ack_text` — «Услышала: „…“. Делаю.»
- `failed_text` — «Не смогла выполнить поручение: „…“.»
- `stale_text` — поручение дошло позже 30 минут (`MAX_AGE`): не исполняется,
  владелец узнаёт об опоздании и может сказать ещё раз.

## Срочная просьба: задача в комнате (10.10.2026)

«Вера, мне нужна помощь, <что случилось>» — не только ответ мозга, но и
срочная задача в комнате агентов с подтверждением владельцу, что её взяли.

```
слушатель ─(kind, confidence, fragment)─▶ gateway ─▶ voice_command_queue.help_state
   ready (уверенность ≥ 0.75, без сомнений) ──────────────▶ open_help_task ─▶ opened
   confirm ─▶ «Это ты сказал: „…“? Да / Нет» ─▶ asked ─«Да»─▶ ready
                                                │ «Нет» → declined · 10 мин → expired
   opened ─claim─▶ «Взял: <агент>» (taken)
          ─5 мин без claim─▶ room_post to=dot, «эскалировала dot» (escalated)
          ─20 мин─▶ напоминание владельцу (reminded)
   reprompt ─▶ «Не расслышала поручение, повтори.»
```

**Вход.** `VoiceCommand` получил `kind` (`command` | `reprompt`), `confidence`
0..1, `guard`, `doubts`, `session_id`, `start`/`end` и `fragment` — список
`FragmentLine` (реплика с фразой и не больше трёх следом: поручение и два
продолжения; длиннее шлюз отвергает 422). Без `confidence` (старый слушатель)
поручение идёт прежним путём — только ответ мозга, без задачи. Куда идёт
поручение, решает `help_state_for` по `needs_confirmation`: ниже порога
`CONFIRM_BELOW` = 0.75 или сомнение `mid_sentence`/`short` — сначала вопрос.
Формула и обоснование порога — в [listener.md](listener.md#срочная-просьба-и-уверенность).

**Задача** (`vera_shared/voice_help/room_intake.py`, `open_help_task`):
`room=help_room()` (`VOICE_HELP_ROOM`, по умолчанию `main`), `task_id=help-<command_id>`,
`project=project_for(app, window)` (правило в `voice_help/policy.py`,
`PROJECT_RULES`, иначе `Vera`), `priority=0`, `auto_pickup=False`,
`responsible="Claude"`, `next_action` — поручение и `SAFETY_NOTE`
(«Срочность не снимает подтверждений…»), `refs` — `voice_event`,
`voice_command`, `source` (`source_ref`: `session@start-end`). Плюс пост
`status="request"` с тем же `task_id`. Дедуп — только `task_id` и
`message_id` поста (`help_task_id`): повтор слушателя, ретрай бота, перезапуск
— одна задача и один пост. Текст в комнату — только через
`redact_secrets` (`vera_shared/redact.py`): ключи `sk-…`, `ghp_…`, `AKIA…`,
токен бота, JWT, длинные hex/base64, слова после «пароль/password», номер
карты (с проверкой Луна) → `[скрыто]`. Событие и ответ владельцу — без
вычистки: это его же слова в его памяти.

**Состояние в очереди** (`voice_help/queue_state.py`, миграция 052): колонки
`kind`, `confidence`, `source`, `help_state`, `task_id`, `task_opened_at`,
`confirm_asked_at`, `taken_by`/`taken_at`, `escalated_at`, `reminded_at`.
Переходы: `mark_asked` (строка уходит в `status='waiting'` — `claim_command`
её не берёт), `answer_confirmation` (под блокировкой: `confirmed` |
`declined` | `expired` | `unknown`), `expired_asks` / `mark_expired`,
`mark_opened`, `watched_help`, `mark_taken`, `mark_escalated`,
`mark_reminded`, `mark_closed` (задачу отменили, не взяв).

**Кто взял** (`voice_help/tracking.py`): `task_holder` — первый
`claimed`/`handoff_accept` в `room_task_events` (а не текущий держатель:
мог взять и уже отпустить); `next_step` — чистая функция, сроки
`ESCALATE_AFTER` = 5 мин и `REMIND_AFTER` = 20 мин. Двух исполнителей
исключает `claim` с `fencing_token` под `FOR UPDATE` — проверяется на
Postgres (`tests/integration/test_voice_help_pg.py`): на SQLite `FOR UPDATE`
пуст, и два одновременных `claim` там проходят оба.

**Бот** (`bot_telegram/help_worker.py`, тот же цикл `voice_worker.run_forever`,
второй шины нет): `ask_confirmation` (кнопки — `bot.ask_owner`, нажатие —
`bot.on_help_confirmation`, только от владельца, разбор —
`help_confirm.parse_callback`, переход — `help_confirm.handle_confirmation`),
`expire_confirmations` (10 мин без ответа → отмена, без исполнения),
`track_help` (часы — аргументом `now`). Тексты: `confirm_text`,
`opened_ack_text`, `taken_text`, `REPROMPT_TEXT`, `ESCALATED_TEXT`,
`REMIND_TEXT`, `EXPIRED_TEXT`. Каждый шаг — сначала сообщение, потом отметка:
at-least-once, как у ответа.

**Тесты** — комната `voice-test` (фикстура ставит `VOICE_HELP_ROOM`), боевая
`main` не появляется: `tests/unit/test_voice_help_intake.py`,
`test_voice_help_tracking.py`, `test_redact.py`.

## Гарантии

- **Адресат — только владелец** (`OWNER_TELEGRAM_ID`, fail-closed): без него
  воркер не берёт из очереди ничего.
- **Без внешних действий.** Поручение исполняется мозгом так же, как текстовое
  сообщение: поиск, чтение Telegram, запись в память Веры. Отправить что-то
  другим людям или изменить данные вне памяти оно не может. Срочная задача
  в комнате — тоже внутреннее действие; исполнители связаны `SAFETY_NOTE`.
- **Без «Да» — ничего.** Пограничная уверенность: ни задачи, ни ответа мозга,
  пока владелец не нажал «Да»; 10 мин тишины — отмена.
- **Ответ не теряется: at-least-once.** Дубль возможен в одном узком окне:
  Telegram принял сообщение, а отметка не успела записаться. Если ответ так и не
  получен — владелец получает «Не смогла выполнить».
- **Текст поручения в логах — только DEBUG.** В колонку `error` пишется лишь
  тип исключения.
