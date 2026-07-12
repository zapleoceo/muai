# Identity resolution — один человек сквозь каналы

Как Вера понимает, что «Маша» в Telegram, `maria@corp.com` в почте и
«Matia Ivanova» — один человек. Три слоя, финальное решение всегда за
владельцем (ничего не сливается автоматически).

## 1. Кросс-канальные сущности при инжесте

Каждый источник создаёт person-сущности с alias `(source, identifier)`:

| Источник | Кто становится сущностью | Где в коде |
|---|---|---|
| telegram | отправитель каждого сообщения + чат + membership | `ingestor-telegram/entity_sync.py` |
| gmail | собеседник письма: `correspondent_of()` выбирает From (received) / первый To (sent), свои ящики и self-notes пропускаются; `sync_correspondent_entity()` пишет entity+alias `(gmail, email)` | `ingestor-gmail/poller.py` |
| instagram | собеседник DM (только received) → alias `(instagram, user:<pk>)` | `ingestor-instagram/__main__.py` |

До 2026-07-19 сущности создавал только telegram — почта и IG шли в события,
но не в граф идентичности.

## 2. Кандидаты и LLM-судья (`vera_shared/graph/identity.py`)

- `canonical_name_parts(name)` — (имя, фамилия) в канонической форме:
  lower, транслит LAT→RU по звучанию, уменьшительные → полные
  (Маша/Masha → мария; Оля/Olga → ольга). Эмодзи и мусор отбрасываются.
- `find_identity_candidates(limit)` — пары person-сущностей с одинаковым
  каноническим именем И подкрепляющим сигналом, по приоритету:
  разные каналы (gmail↔telegram — кросс-источник), общие чаты
  (memberships), совпадающая фамилия. Уже осуждённые пары (любой статус
  в `merge_suggestions`) не переспрашиваются.
- `judge_pair(a, b, signal)` — один вызов LLM (`chat:smart`,
  `IDENTITY_JSON_SCHEMA` strict json_schema): вердикт same/different/unsure,
  confidence, reason на русском. Досье обеих сущностей (проект, чаты,
  примеры сообщений) идут в промпт из `get_entity_dossiers`.
- `save_suggestion` / `run_identity_analysis(max_pairs)` — прогон раунда;
  вердикт different сохраняется сразу со status=rejected (не показывается,
  не переспрашивается). Сбои брокера скипают пару, не роняя раунд.
- `list_pending_suggestions` / `set_suggestion_status` — чтение/решение.

Хранение: таблица `merge_suggestions` (миграция 016, ORM
`MergeSuggestionRow`): entity_a<entity_b, verdict, confidence, reason,
status pending|accepted|rejected, UNIQUE(entity_a, entity_b).

## 3. UI (`/entities/duplicates`)

Фиолетовая секция «🧠 Вера предлагает объединить»: кнопка
`entities_analyze` (POST `/entities/analyze`) запускает раунд в фоне
процесса дашборда; каждое предложение — два досье + улика Веры + кнопки
`entities_suggestion` (POST `/entities/suggestion`): «оставить A» /
«оставить B» (→ `merge_entities`) / «разные люди».

## «Я» — это автор сообщения

Первое лицо в тексте резолвится НЕ по имени: `rel_extract` держит
`SELF_TOKENS` («я», i, me…), промпт велит LLM писать ровно "Я" для
самореференции автора, а `author_entity_of_event(event_id)` возвращает
сущность автора: telegram/instagram received → отправитель по alias,
gmail received → адрес From, любые sent и «свои» источники → владелец
(alias `user:OWNER_TG_ID`). Автора нет в графе → self-связь пропускается.

История бага (2026-07-19): «Я» резолвилось по имени и совпадало с чужим
аккаунтом, у которого first_name = «Я», — 221 ребро от 6+ разных авторов
висело на постороннем человеке, а прежний промпт («я» → пиши "Дима")
раскидывал связи владельца по 21 тёзке. Существующие рёбра перевешаны
одноразовым SQL-ремонтом на истинных авторов их исходных событий.

## Точность против мусора: строгий резолв тёзок

`repo.resolve_entity_exact` теперь возвращает id ТОЛЬКО при однозначном
совпадении имени (или alias display_name). Раньше `.limit(1)` без ORDER BY
вешал rel_extract-связи на произвольного из 21 «Дима» — недетерминизм и
склейка разных людей в один узел. Неоднозначное имя → None → rel_extract
пропускает связь.

## Membership-рёбра: коллеги связаны через группу

`graph_snapshot` отдаёт рёбрами не только `relationships` (LLM-факты), но и
`memberships` как синтетический predicate `member_of` (пунктир на /graph,
фильтр в дропдауне). Люди одного проекта соединяются через узел своей
группы — Дарья, Марина и все из «Jakarta sales» видимо связаны, даже когда
явных «coworker_of»-фактов в переписке не было. Degree узла учитывает
членства (группы становятся хабами), кластеризация тоже идёт по этим рёбрам.

История графа добэкфилена одноразовым SQL из `events` (entity_sync работал
только live с середины июня): +207 людей, писавших лишь до этого, их
членства, имена — username-фолбэк. `upsert_entity` теперь обновляет
фолбэк-имя (username / tg_user_N) на настоящее, как только live-путь его
видит; настоящее имя фолбэком не перетирается.

## Тематические кластеры графа (`vera_shared/graph/clusters.py`)

- `label_propagation(node_ids, edges)` — детерминированное выделение
  сообществ по структуре связей (кто с кем тесно связан).
- `split_hubs(degrees, percentile)` / `attach_hubs(assign, hubs, edges)` —
  сверх-хабы (узел владельца связан со всеми и склеивает граф в одно
  сообщество) исключаются из propagation по перцентилю степени (настройка
  `graph_hub_percentile`, /settings) и потом приписываются к сообществу
  большинства своих соседей.
- `name_clusters_llm(assign, nodes)` — Вера подписывает крупные сообщества
  (`CLUSTER_LABEL_SCHEMA`, `chat:fast`): «Команда IT STEP», «Семья»…
  Сбой брокера → фолбэк «кластер N».
- `recompute_clusters(limit)` — снапшот ядра → сообщества → ярлыки →
  кэш JSON в `app_control['graph_clusters']` (`get_clusters` читает).
  Пересчёт только кнопкой на `/graph` (`graph_recluster`,
  POST `/graph/recluster`, фон) — граф дрейфует медленно, жечь LLM на
  каждый просмотр незачем.
- `/api/graph` подмешивает `cluster` в узлы и `cluster_labels` в ответ;
  страница красит ободки узлов цветом кластера и рисует легенду.
