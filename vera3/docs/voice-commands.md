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
срочная задача в комнате агентов, которая открывается СРАЗУ. Кнопки «Это ты
сказал? Да / Нет» нет (решение владельца 11.10.2026): владельцу приходит
только уведомление, а неуверенный источник помечается в самой задаче.

```
слушатель ─(kind, confidence, doubts, fragment)─▶ gateway ─▶ help_state=ready
   ready ─▶ open_help_task ─▶ opened ─▶ «Открыл задачу help-<id>: …»
            владелец подтверждён: «Срочно: …» + ответ мозга
            source_uncertain:     «[источник не подтверждён] …», мозг не зовётся,
                                  вопрос владельцу — удержание до ответа
   строка очереди status=opened ─claim─▶ «Взял: <агент>» (taken), status=done
          ─5 мин без claim─▶ room_post to=dot, «эскалировала dot» (escalated)
          ─20 мин─▶ напоминание владельцу (reminded)
   reprompt ─▶ «Не расслышала поручение, повтори.»
```

**Вход.** `VoiceCommand` получил `kind` (`command` | `reprompt`), `confidence`
0..1, `guard`, `doubts`, `session_id`, `start`/`end` и `fragment` — список
`FragmentLine` (реплика с фразой и не больше трёх следом: поручение и два
продолжения; длиннее шлюз отвергает 422). Без `confidence` (старый слушатель)
поручение идёт прежним путём — только ответ мозга, без задачи. С `confidence`
`help_state_for` всегда даёт `ready`. Подтверждён ли источник, решает
`source_uncertain` (`voice_help/policy.py`) и versioned guard при открытии задачи:
только `guard="own"` с `guard_version=2`, уверенностью не ниже порога и без
сомнений считается подтверждённым. Старый guard без версии остаётся на удержании.
Уверенность ниже `CONFIRM_BELOW` = 0.75 или **любое** непустое `doubts` (`quoted` — похоже на
пересказ, `mid_sentence`, `short`, `unclosed` — пауза после фразы не видна,
`blind` — системная дорожка не писалась, эхо не исключить, и незнакомые
серверу признаки тоже) — источник не подтверждён. `command_id` — до 59 ASCII
(`[A-Za-z0-9_.:-]`).

**Кодовая фраза = владелец (с 27.10.2026).** Слушатель принимает BLIND на
микрофоне как `own` (константа `CODEWORD_IS_OWNER`): кодовую фразу знает только
владелец. `blind` из слушателя больше не приходит; ECHO по-прежнему отбрасывается.

**`guard` и авторство.** `guard` ∈ `own` | `echo` | `blind` — вердикт
`command_guard` слушателя (`echo` слушатель не отправляет, но шлюз его
принимает); иное значение — 422. Шлюз передаёт его в `create_command`, оно
ложится в `metadata.guard` события и в `source.guard` строки очереди.
Подпись события решает `voice_commands.author_for`: `author_role="self"`,
`author_label="Я"` — только при `guard="own"`, `guard_version=2`
(`GUARD_VERSION`: старые слушатели слали литерал `"own"` для любой
команды; нечисло в `guard_version` — None; хранится рядом с `guard`), `confidence` ≥
`CONFIRM_BELOW` и пустых `doubts` (тот же `source_uncertain`). Всё остальное,
включая старого слушателя без `guard`, — `author_role="unverified"`,
`author_label="не подтверждено"` (fail closed). До 15.10.2026 каждое
поручение подписывалось «Я», даже слепое и неуверенное.

**Граница доверия.** `confidence`, `guard`, `guard_version` и `doubts` приходят от клиента —
слушателя на ноутбуке. Защищены они только `X-Internal-Secret` и самим
слушателем; `guard_version` различает ревизии кода, но сам по себе не доказывает личность. Кто знает секрет, заведёт задачу
«Срочно: …» без пометки. Защита от чужого голоса — на стороне слушателя
(mic-only, ECHO отбрасывается); неподтверждённое (BLIND и прочие сомнения)
доходит до комнаты только как помеченное непроверенное входящее.

**Событие.** Событие `source=voice_command`, `category=command` пишется сразу
при приёме и уходит в triage как любая реплика.

**Порядок выкатки — сначала сервер, потом слушатель.** Старый шлюз требует
непустое `instruction` и не знает `kind`: `kind="reprompt"` с пустым
поручением получит 422, и слушатель отложит его в `commands-failed/`.
Новый шлюз со старым слушателем работает: без `confidence` — прежний путь.
Формула и обоснование порога — в [listener.md](listener.md#срочная-просьба-и-уверенность).

**Задача** (`vera_shared/voice_help/room_intake.py`, `open_help_task`):
`room=help_room()` (`VOICE_HELP_ROOM`, по умолчанию `main`), `task_id=help-<command_id>`,
`project=project_for(app, window)` (правило в `voice_help/policy.py`,
`PROJECT_RULES`, иначе `Vera`), `priority=0`, `auto_pickup=True` только для
подтверждённого источника (задачу выдаёт `room_task_next`). Неподтверждённая
задача создаётся с `auto_pickup=False` до вопроса; явное «да, это я» включает
`auto_pickup` под блокировкой строки вместе с `source_confirmed`.
`responsible="Claude"`, `refs` — `voice_event`, `voice_command`, `source`
(`source_ref`: `session@start-end`). Тексты собирает `help_texts`:
владелец подтверждён — заголовок «Срочно: <текст>», `next_action` —
поручение и `SAFETY_NOTE` («Срочность не снимает подтверждений…») —
`normal_action`. Поручение про dot/ChatGPT (`routes_to_dot`, список
`DOT_NEEDLES` рядом с `PROJECT_RULES`, без регистра, после вычистки) —
`responsible` прежний, в `refs` `source` — `RESULT_RECIPIENT_REF`
(`result_recipient: dot`), в `next_action` — `DOT_RESULT_NOTE` («результат —
room_post to=dot с task_id; закрывать только по receipt»).
Источник не подтверждён — заголовок «[источник не подтверждён] <текст>»
(`UNCERTAIN_TITLE`), первая строка `next_action` и поста — ровно
`UNCERTAIN_NOTE` («Источник не подтверждён — проверь авторство и полномочия
до любых действий. Это непроверенное входящее, не поручение владельца.»),
затем `UNCERTAIN_HOLD`, `SAFETY_NOTE`, список сомнений (`listed_doubts`) и услышанный текст;
в `refs` ещё один `source` — `source_uncertain: <сомнения>` (схема задачи не
менялась, миграции нет); `next_action` режется до `NEXT_ACTION_CHARS`.

**Удержание** (`voice_help/hold.py`): `ask_owner_once` — вопрос владельцу
`UNCERTAIN_QUESTION` («Это ты сказал? Ответь „да, это я“ или „нет, не я“.»)
в `room_task_questions` от `vera`, без аренды (иначе первым держателем в
журнале стала бы Вера), один на задачу при повторе и рестарте. Задача
остаётся `open`: открытый вопрос не пускает её в `room_task_next`.
`lift_hold` (на каждом проходе `track_help`) читает последний ответ дашборда
с `answered_by="owner"`. Явное «да, это я» добавляет ref `SOURCE_CONFIRMED`,
включает `auto_pickup=True` и возвращает обычный `next_action`; «нет, не я»
отменяет задачу и инвалидирует lease через fencing. Двусмысленный или чужой
ответ оставляет удержание. Прямой claim и подбор через `room_task_next`
не проходят для `source_uncertain` без `SOURCE_CONFIRMED`, даже если старое
значение `auto_pickup` было true или статус вопроса уже `answered`.
`is_confirmed` не даёт снять удержание повторно.
Плюс пост `status="request"` с тем же `task_id`.
Дедуп — только `task_id` и `message_id` поста (`help_task_id`): повтор
слушателя, ретрай бота, перезапуск — одна задача и один пост. Текст в
комнату — только через
`redact_secrets` (`vera_shared/redact.py`): ключи `sk-…`, `sk_live_…`/`sk_test_…`,
`ghp_…`, `AKIA…`, токен бота, JWT, `api_key=…`/`token=…`, пароль в URL
(`postgres://user:<скрыто>@host`), длинные hex/base64, IBAN, пароль после
«пароль/password» (до шести слов, не дальше знака конца фразы — «Qwerty!23»
целиком), «пин/пин-код», «код из смс», любая группа 13–19 цифр (без
проверки Луна: распознавание путает цифры) → `[скрыто]`. Телефоны с «+»,
суммы, даты и номера короче 13 цифр не трогаются. Через вычистку идут
title, next_action, refs (`source_ref`, сомнения) и пост. Событие и ответ владельцу — без
вычистки: это его же слова в его памяти.

**Состояние в очереди** (`voice_help/queue_state.py`, миграция 052): колонки
`kind`, `confidence`, `source`, `help_state`, `task_id`, `task_opened_at`,
`taken_by`/`taken_at`, `escalated_at`, `reminded_at` (`confirm_asked_at`
остался от версии с кнопкой и больше не пишется). Переходы: `mark_opened`, `hold_command` (`status=HELD`=`opened` — не
`pending`, бот её снова не возьмёт, и не `done`, пока задачу не взяли),
`release_held` (на taken/closed → `done`, текст стирается),
`watched_help`, `mark_taken`, `mark_escalated`, `mark_reminded`,
`mark_closed` (задачу отменили, не взяв). Строки `help_state='confirm'` от
прежней версии бот открывает как `ready`; `asked` (ждали «Да») остаются в
`waiting` и не исполняются.

**Кто взял** (`voice_help/tracking.py`): `task_holder` — только
ТЕКУЩИЙ держатель: `room_tasks.lease_holder` не `vera`, `lease_until` в будущем и в
`room_task_events` есть его `handoff_accept` на текущем `fencing_token` после последнего
`handoff_offer`. Для аттестованной задачи (передача запрещена) достаточно живого `claimed` держателя на текущем токене. Квитанция (ack), старый `claimed` и истёкшая аренда — не «Взял»; `next_step` — чистая функция, сроки
`ESCALATE_AFTER` = 5 мин и `REMIND_AFTER` = 20 мин. Двух исполнителей
исключает `claim` с `fencing_token` под `FOR UPDATE` — проверяется на
Postgres (`tests/integration/test_voice_help_pg.py`): на SQLite `FOR UPDATE`
пуст, и два одновременных `claim` там проходят оба.

**Бот** (`bot_telegram/voice_worker.py` → `open_and_notify`, затем
`bot_telegram/help_worker.py`, тот же цикл `voice_worker.run_forever`, второй
шины нет): открыть задачу, отметить `opened`, сообщить владельцу
`opened_text` — «Открыл задачу help-<id>: <текст>», для неподтверждённого с
префиксом «Источник не подтверждён.» — через `say`, без клавиатуры
(`send_to_owner` не передаёт `reply_markup`). Неподтверждённый источник
на этом останавливается: мозг не зовётся, ничего не исполняется. Лог INFO по
`command_id`, без текста: «очередь→задача N мс, →уведомление M мс». Дальше
`track_help` (часы — аргументом `now`): `taken_text`, `ESCALATED_TEXT`,
`REMIND_TEXT`, `REPROMPT_TEXT`. Старые кнопки «Да / Нет» (`vh:`) в чате
остались: `bot.on_help_confirmation` отвечает владельцу `STALE_BUTTON_TEXT`
и снимает кнопки, чужому — пустой ответ; ничего не меняет. Каждый шаг —
сначала сообщение, потом отметка: at-least-once, как у ответа.

**Результат исполнителя** (`help_worker.deliver_result`, `result_text`,
`vera_shared.voice_help.result_state`): когда задача закрыта со статусом done и в
событии `done` есть текст, бот шлёт владельцу «Результат по задаче <id> (<агент>): …»
через тот же `send_to_owner` (при `OWNER_TELEGRAM_ID=0` он бросает — fail-closed,
отправки нет). Ответ мозга уже ушёл раньше и не повторяется. Без миграции: этап — в
`voice_command_queue.help_state` (`result_pending` → `closed`; `result_error`,
`result_unknown`), квитанция (`result_message_id`, `result_sent_at`,
`result_errors`) — в `source`. Намерение (`result_pending`) пишется до отправки;
исключение отправки или пустой message_id → `result_error`, повтор до
`RESULT_MAX_ERRORS`; намерение без квитанции на следующем проходе (падение между
отправкой и записью) → `result_unknown`, автоповтора нет. Строка закрывается только
после квитанции; пустой результат закрывает как раньше. Результаты старше
команды, созданные раньше `VOICE_RESULT_DELIVERY_SINCE` (ISO UTC; деплой ставит время
выкатки), не обрабатываются; переменная не задана — функция выключена, ничего не шлётся,
строки закрываются как раньше. Ответ мозга и результат исполнителя делят одну заявку
на доставку (`delivery_claim`, ключ `delivery` в `source`): кто взял — шлёт, второй нет;
ответ мозга считается доставкой, поэтому после него результат отдельным сообщением
не уходит. Неясный исход (упали после отправки) не повторяется.

**Диспетчер** (`bot_telegram/dispatcher.py`, `dispatch_one()`; отдельный цикл
`run_forever` в том же процессе бота, без LLM). Он не ждёт `ask_brain` в
голосовом цикле: ожидание ответа не задерживает доставку новых задач. Один
проход: сначала ожидающие квитанции, затем новая задача. Новая:
`task_queue.next_task(project="Vera",
skip_ref_prefix=DISPATCHED_PREFIX)` берёт от имени `AGENT` (`vera`) первую
`auto_pickup`-задачу без ref `dispatched:` и без открытого вопроса (фильтр по refs
— в SQL, поэтому 50+ уже обработанных задач не прячут новые) → `room_post`
получателю (`recipient_of`: ref `result_recipient: X`, иначе `dot`) с
`message_id=dispatch-<task_id>`, `task_id`, `origin` и очищенным заголовком
(`dispatch_body`).
Перед проектом `Vera` диспетчер проверяет узкий маршрут `Veranda`: только
`created_by=veranda_bot` с точным ref `owner_dispatch: dot`; источник этого
маркера описан в `office-intake.md`.
Перед проектом `Vera` диспетчер проверяет узкий маршрут `Veranda`: только
`created_by=veranda_bot` с точным ref `owner_dispatch: dot`; источник этого
маркера описан в `office-intake.md`.

**Успешный `room_post` — не квитанция.** Задача остаётся за `vera`
(`in_progress`) в `room_task_wait` на `RECEIPT_WAIT_S` (10 мин), refs:
`dispatched: dispatch-<id>`, `dispatch_attempts: N`, `dispatch_next_at: <iso>`.
Квитанция доставки — ответ именно получателя на именно это сообщение:
`room_post(status="ack", in_reply_to="dispatch-<task_id>")`. Только она ведёт к
штатной передаче `handoff.offer(vera → получатель)` (`dispatcher._offer`, ref
`handoff_offered: <агент>`): аренда `vera` живёт, пока получатель не сделает
`handoff_accept` (токен+1, «Взял» только тогда). Предложение не принято за
`RECEIPT_WAIT_S` или отклонено — `dispatcher._offer_lapsed`: `handoff.cancel`,
ref `offer_retry`, пауза `backoff_s`, новое предложение; общий счётчик с доставкой,
после `MAX_ATTEMPTS` — `blocked` с `OFFER_BLOCKED_NOTE`. Рестарт при висящем
предложении второго не шлёт (`pending_handoff_to`); accept после cancel отвергается
(`HandoffError`), cancel после accept — `StaleLease`/`HandoffError`, диспетчер
отступает. Задача под аттестацией передаче не подлежит — для неё прежний release в
`open`. Неподтверждённые/удержанные задачи `next_task` пропускает — их не предлагают. Это отдельно от
квитанции исполнения (`done`/`blocked` по задаче, другое сообщение): такой ответ,
ack от другого агента или ack на другое сообщение доставкой не считаются.

Нет квитанции к `dispatch_next_at` — повтор того же сообщения (тот же id и тело,
дедуп), пауза удваивается (`RECEIPT_WAIT_S * 2^(N-1)`, джиттер ±20%, `backoff_s`).
Сбой самого поста — та же попытка и та же пауза, без горячего цикла. После
`MAX_ATTEMPTS` (3) без квитанции — release `blocked` с `BLOCKED_NOTE` («получатель
не подтвердил получение»): задача видна, не потеряна. Аренда продлевается на
паузу; истекла (процесс лежал) — следующий проход берёт её заново и продолжает без
дублей. Потерянная аренда (`StaleLease`) — пропуск. `task_holder` не считает
`AGENT` держателем, поэтому «Взял: vera» не бывает. Тесты: `tests/unit/test_office_dispatch.py`.

**Тесты** — комната `voice-test` (фикстура ставит `VOICE_HELP_ROOM`), боевая
`main` не появляется: `tests/unit/test_voice_help_intake.py`,
`test_voice_help_tracking.py`, `test_voice_help_hold.py`, `test_redact.py`.

## Гарантии

- **Адресат — только владелец** (`OWNER_TELEGRAM_ID`, fail-closed): без него
  воркер не берёт из очереди ничего.
- **Без внешних действий.** Поручение исполняется мозгом так же, как текстовое
  сообщение: поиск, чтение Telegram, запись в память Веры. Отправить что-то
  другим людям или изменить данные вне памяти оно не может. Срочная задача
  в комнате — тоже внутреннее действие; исполнители связаны `SAFETY_NOTE`.
- **Неподтверждённое не исполняется.** Пограничная уверенность или любое
  сомнение: задача с пометкой «источник не подтверждён» и уведомление
  владельцу — и всё; ответа мозга нет.
- **Ответ не теряется: at-least-once.** Дубль возможен в одном узком окне:
  Telegram принял сообщение, а отметка не успела записаться. Если ответ так и не
  получен — владелец получает «Не смогла выполнить».
- **Текст поручения в логах — только DEBUG.** В колонку `error` пишется лишь
  тип исключения.

Проверка получения — `has_reply` (`vera_shared/room/messages.py`): есть ли от получателя сообщение со статусом `ack` и `in_reply_to` на `dispatch-<task_id>`.
