# Связи событий с людьми (`vera_shared.links`, миграции 042–044)

Цель — мозг универсален: любой вопрос владельца («кто был на созвоне, где обсуждали X»,
«что <человек> просил команду в сентябре», «где его называют по инициалам») агент
собирает из нескольких ОБЩИХ инструментов, а не из специальной функции под вопрос. Для этого
каждое событие связано с людьми одной моделью.

До 042 событие знало автора (алиас отправителя) и подстроку полного имени в `timeline`.
Получатель письма, собеседник лички, участники созвона, упомянутые третьими лицами
(«ДА просил ознакомиться…») нигде не хранились — знание о человеке не липло к его карточке.

## Таблица `event_entities`

Одна строка — одна связь: `event_id`, `entity_id`, `role`, `source_of_link`, `confidence`,
`token`, `span`, `scope_ok`. Ключ — `(event_id, entity_id, role, token)`; ORM —
`EventEntityRow`, данные — `Link` (`links/model.py`).

| role | кто | откуда берётся |
|---|---|---|
| `author` | написал / записал | алиас отправителя (`telegram user:<id>`, адрес gmail); владелец — автор каждого созвона |
| `recipient` | адресат | только однозначный: личка Telegram / Slack im, `To` письма. В группе получателей нет: молчавший не адресат |
| `participant` | был в созвоне | голос опознан слушателем, названный участник выжимки, решение владельца |
| `mentioned` | упомянут третьими лицами | полное имя, характерная фамилия, `@ник`, прозвище в области, искажённое ASR имя |

`source_of_link`: `alias`, `nickname`, `voiceprint` (слушатель назвал голос по отпечатку или
заголовку окна), `name_match` (имя сопоставлено), `manual` (решение владельца или агента).
`scope_ok=false` — прозвище вне области: строка нужна для счёта и аудита, читатели её не берут.

Таблица — производные данные, кроме `manual`-строк карты голосов, которые пересчёт возвращает
из `voice_speaker_map`. Её можно очистить и пересчитать.

## Сборка

Модули `links/`: `context_data` (SELECT'ы и чистые помощники: `EventView`, `addresses_of`,
`sender_key`, `chat_key`, `wanted_aliases`, `alias_map`, `work_chat_keys`, `chat_authors`,
`person_projects`), `context` (`ContextBuilder`, `EventFacts`), `index` (`index_views`,
`index_events`, `links_for`, `run_batch`), `index_store` (курсоры `set_cursor` / `read_cursors`,
`next_batch`, `load_views`, `view_of_row`, `write_links`), `index_resources` (`Resources`,
`load_resources`), `index_voice` (`voice_guesses`, `voice_links_for`, `voice_body`).
Ещё: `owner_entity_id` (сущность владельца), `established_contacts` (пары с устоявшимся общением), `LinkCursorRow`
(курсор потока). Текстовые помощники сопоставления — `matcher_text` (`name_keys`, `text_words`, `same_word`,
`find_word`, `sentence_initial`). Связи по виду: `base_links` (автор, получатели), `mention_links`, `voice_links` (`builders.py`),
`speakers_of`, `is_anonymous`.

- **Выборка и запись.** `next_batch` отдаёт id пачки, `load_views` читает ТОЛЬКО нужные колонки
  (текст события и метаданные; `content_extra` и расшифровка — отдельным запросом и только у созвонов).
  `write_links` заменяет ПРОИЗВОДНЫЕ связи событий пачки одной транзакцией; ручные (`manual`) не
  удаляются, а производная с ключом, занятым ручной, не вставляется. Скрытые события связей не
  получают, их старые производные связи удаляются.
- **Круг не зависит от порядка.** `ContextBuilder.preload` один раз (и раз в час в долгоживущем
  процессе) считает `chat_authors` — кто писал в каждом групповом чате за всю историю (площадки
  с >60 авторов не берутся) — и `person_projects`; backfill от новых к старым видит тот же круг, что
  и обработка по порядку.
- **Плохое событие не стопорит курсор.** Пачка, на которой сборка упала, разбирается по одному
  событию; упавшие пропускаются с записью в лог (`BatchResult.skipped`), курсор двигается. Сбои
  базы и сети (`OperationalError`, `InterfaceError`, `OSError`, `TimeoutError`) — не плохое
  событие, они пробрасываются, пачка повторится.
- **Новые события** — `links_loop` (`run_links_cycle`): раз в минуту (без новых событий — выход до замка; ресурсы кэшируются на 10 минут; замок сессионный, на отдельном AUTOCOMMIT-соединении, без открытой транзакции) до пяти пачек по 200 после
  курсора `forward` под advisory-замком; один `ContextBuilder` на процесс. Переменные:
  `TRIAGE_LINKS_INTERVAL_S`, `TRIAGE_LINKS_BATCH`, `TRIAGE_LINKS_MAX_BATCHES`,
  `TRIAGE_LINKS_START_DELAY_S`.
- **Старые события** — `scripts/backfill_event_links.py`: от больших id к меньшим (`reset_cursors`,
  `max_event_id` — `--reset` начинает заново, `--status` ничего не меняет). Новые события
  скрипт не трогает (их ведёт цикл под замком). Регламент — `deploy-ops.md`.

## Упоминания: имена, круг, прозвища

`MentionMatcher` (`links/matcher.py`, `PersonNames`, `Mention`) находит людей в тексте.
Фамилия без имени не засчитывается в начале предложения и если то же слово есть в тексте строчным;
два человека с одним полным именем не называют никого (`NameResolver.full`). Уверенность по виду улики: `@ник` 1.0; имя целиком 0.95; имя и отчество 0.9; характерная
фамилия одна (≥5 букв, в графе один носитель, с заглавной) 0.8; одиночное имя 0.6.

**Круг разговора.** Одиночное имя («Дима») указывает на человека ТОЛЬКО внутри круга:
участники чата (кто писал + `memberships` небольших групп), собеседник лички, автор, адресаты
письма и сам владелец — события приходят из его аккаунта, он есть в каждом чате. Имя
сравнивается по формам (`name_forms.py`, `name_group`: Дмитрий = Дима = Дим = Dmitry), и подошедший
должен быть ОДИН: два Димы в круге — связи нет (`NameResolver`: `full`, `short`, `resolve`).
Второй круг (`ChatContext.extended`, сильные контакты автора) берётся, только если в первом
никого. Владелец входит в круг только там, где есть разговор (чат, личка, адресаты письма): в
запросе к поисковику или заметке «Дима» — не обязательно он, связи нет. Тот же круг применяют `circle_of`, `event_circle` (`EventCircle`) и
`resolve_short_name` (`links/circle.py`) при извлечении связей: `rel_extract` резолвит
одиночное имя человека по кругу события, а не «единственного с таким точным именем» —
таким оказывался чужой тёзка (аудит 04.10: сущность «Дима» из публичного чата держала 23
связи из переписки владельца). Организации из одного слова («Acme») резолвятся как раньше.

**Прозвища с областью** (`entity_nicknames`, `EntityNicknameRow`, `links/nicknames.py`:
`active_rules`, `put_nickname`, `add_nickname`, `restore_nickname`, `suggest_nickname`,
`pending_suggestions`, `decide_suggestion`, `NicknameError`). Токен регистрозависим:
«ДА» заглавными — Дмитрий Александрович в рабочем чате, обычное «да» в туристическом.
Область (`links/scope.py`: `NicknameRule`, `in_scope`, `token_pattern`):

| scope | засчитывается |
|---|---|
| `work` | рабочие чаты (`project_membership`) и личка с сильными контактами человека |
| `contacts` | то же + группы, где пишут ≥2 его сильных контактов |
| `chats` | только чаты из `scope_ids` (`telegram:<chat_id>`) |
| `global` | везде |

Рабочую область можно сузить проектом: `scope_ids=['project:itstep']` оставляет чаты этого проекта
(в Veranda — другой бизнес — «ДА» остаётся словом), а чаты без проекта (Slack-пространство) не
исключаются (`ChatContext.project`). В личке сужение тоже действует: прозвище проекта засчитывается, только если сам чат
помечен этим проектом или собеседник писал в чатах проекта (`ChatContext.dm_partner_projects`). Вне области упоминание пишется с `scope_ok=false`.
Прозвище из нескольких слов («Имя Отчество») ищется по основам слов без регистра — падежи не мешают.
В расшифровках созвонов ищутся только прозвища области `global` («да» заглавными в речи — слово,
а фраза «Дмитрий Александрович» — человек). **Предложения кода не применяются сами:**
`suggest_initials` (`links/nickname_suggest.py`, `patronymic_forms`, `initials`, `dominant_project`)
ищет в переписке с человеком обращение «Имя Отчество» (имя — любая форма его имени, ≥2 раза) и
предлагает ДВА токена: инициалы (область `work`, суженная проектом человека) и саму фразу (область
`global`); строка `suggested` ждёт решения владельца (`decide_suggestion`), отвергнутое не
переспрашивается.
`scope_report` считает, сколько раз токен встречается в области и вне её (по сообщениям, до
применения) — для решения владельца. Управление — `scripts/manage_nicknames.py` и MCP
`entity_add_nickname`.

## Созвоны первоклассно

На сервере у созвона есть `metadata.voices` / `counterparts` и реплики `content_extra.utterances[]`
с `speaker`; отпечатки голосов (`voiceprints.json`) остаются на ноутбуке. Поэтому:

- владелец — `author`; говорящие с настоящим именем — `participant` (`voiceprint`, 0.9 при полном
  имени, `name_match` 0.6 при одиночном имени из круга владельца); участники из выжимки — 0.7;
  «Собеседник N» связи не даёт, а попадает в `unresolved_speakers` (`event_participants`);
- **карта голосов** `voice_speaker_map` (`VoiceSpeakerMapRow`, `links/speakers.py`:
  `put_speaker`, `put_voiceprint`, `SpeakerError`): ярлык в одном созвоне (`kind=event`) или id
  отпечатка (`kind=voiceprint`, когда слушатель начнёт присылать его в реплике). Назвать — MCP
  `voice_speaker_set`; связь `manual` появляется сразу, пересчёт её сохраняет, откат — `undo`
  (`undo_speaker`, `undo_nickname`);
- **ASR-имена** (`links/asr.py`, `asr_matches`, `AsrMatch`): «Арчагин» → «Корчагин» только среди
  участников и сильных контактов владельца, слово ≥7 букв, сходство ≥0.8 и заметный отрыв от
  второго кандидата; результат — `mentioned`/`name_match` с уверенностью не выше 0.7 и
  `span={asr: true}`. Стенограмма НЕ переписывается. Сравнение квадратично, поэтому перед `SequenceMatcher.ratio` стоят отсевы
  (разница длин, `real_quick_ratio`, `quick_ratio`), а из цикла событий зовётся `asr_matches_async` (отдельный поток).

## Чтение и фильтры

`links/read.py`: `event_participants` (люди события с источником и уверенностью каждой связи,
`unresolved_speakers`), `co_occurrence` (события, где были оба), `filtered_events`,
`entity_events` (события человека по ролям), `mentioning_events` и `mention_counts` («Обсуждения»).
Фильтр `EventFilter` (`links/filters.py`; `build_where`, `and_clause`, `to_dict`, `from_dict`,
`FilterError`): `participant_ids` (были ВСЕ), `mentioned_ids`, `author_ids`, `with_owner`, `source`,
`kind` (`call` / `message` / `email`), период, `project`, `account`, `min_confidence`. Условия —
`EXISTS` по `event_entities` через AND; один фильтр у поиска, MCP и чтения.

- **brain-search** `/search` принимает `filters` (`links_clause`, `LinkScope`): кандидаты сужаются
  ДО ранжирования во всех режимах (FTS, проект, окно времени, ANN). Неверный фильтр — 422.
- **MCP**: `search`, `recent_events`, `timeline` получили фильтры по людям; новые `event_participants`
  и `co_occurrence` (`link_tools.py`, `link_filter`); `entity_context(include_mentions=true)` отдаёт
  упоминания. Записи — `voice_speaker_set`, `entity_add_nickname` (`link_write_tools.py`).
- **Карточка человека** (`/api/graph/entity/{id}`) — поле `mentions`: события, где его упомянули, с
  чатом, фрагментом, `via` и уверенностью — отдельно от его собственных сообщений.

## Вопросы владельца как проверка универсальности

`links/eval_questions.py`: 12 вопросов разной формы (кто был на созвоне про тему; что человек
спрашивал у команды за месяц; когда я последний раз говорил с X про Y; в каких чатах X называют по
инициалам; когда последний звонок с X и кто ещё был; письма, где X — получатель; что обо мне говорили
в рабочих чатах; кто такой «Собеседник 2»; что X поручал; о чём мы говорили с A и B вместе; когда
впервые упоминали тему рядом с A; какая у меня роль с X и на чём основана). Каждый — цепочка шагов
`Step` по общим функциям (`filtered_events`, `entity_events`, `event_participants`,
`co_occurrence`, `mentioning_events`); `Case` хранит форму, разбор «что было до» (`before`:
`yes` / `partial` / `no`, с причиной) и флаг `needs_owner` (суть показана, ответ даёт владелец).
`run_case` исполняет цепочку (результат — `CaseResult`), `after_level` оценивает его, `render_question` подставляет
имена. CI гоняет набор на синтетическом мире (`test_links_eval.py`): после связей отвечаются все 12
(11 yes + 1 partial — голос называет владелец); до — 2 yes, 6 partial, 4 no. На своих данных:
`scripts/eval_owner_questions.py --lisa <id> --director <id> --oleg <id> --topic "<тема>"` (только чтение).

## Чистка связей, ушедших к тёзке

`links/namesake_plan.py` (`load_rows`, `build_namesake_plan`, `namesake_document`): для
извлечённых связей с концом-одиночным именем берётся круг события-источника. Конец в круге —
остаётся; вне круга и в круге ровно один подходящий — `namesake_repoint` (конец переставляется);
иначе `namesake_retire` (`is_current=false`). Ручные связи не трогаются. План в формате
`rel_cleanup`, применение и откат — `rel_cleanup_apply` (отчёт на диск ДО коммита): скрипт
`scripts/clean_namesakes.py` (`--plan`, `--apply --report`, `--undo`, `--entity`).

## Откат и права

**Слияние сущностей** (`graph/merge_links.py`: `merge_nicknames`, `merge_voice_map`, `merge_event_entities`) переносит
прицелы к победителю: прозвища и карту голосов — поштучно с записью в `MergeReport` (`unmerge` вернёт),
ручные связи — поштучно (`MergeReport.created` — строки, созданные слиянием, откат их удалит), производные —
одним UPDATE без поштучного следа (точность возвращает `backfill_event_links.py --reset`).

Правки `voice_speaker_set` и `entity_add_nickname` идут в `mcp_audit` (`target_kind` `speaker` /
`nickname`) одной транзакцией с изменением. Таблицы `event_entities`, `entity_nicknames`,
`voice_speaker_map` роли `vera_ro` не выданы: связи раскрывают личную переписку пар.
