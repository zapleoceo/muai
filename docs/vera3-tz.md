# Vera 3.0 — Технического задание

**Версия**: draft 1.0
**Дата**: 2026-06-08
**Автор**: Claude + Дима
**Цель документа**: полная перезагрузка проекта Веры на правильной архитектуре, с учётом 17 месяцев накопленного опыта в Vera 1.0/2.0.

---

## 0. Executive Summary

Vera 3.0 — **цифровая копия памяти и понимания одного человека (Димы)**. В отличие от Vera 2.0, которая упёрлась в стоимость обработки (~$200/мес при попытке покрыть всё через Graphiti), Vera 3.0:

- **Видит всё** — любое количество источников, без потолка
- **Помнит всё** — навсегда, без потерь
- **Понимает контекстно** — не по типу источника, а по содержимому в текущей жизни Димы
- **Учится непрерывно** — моделирует поведение Димы, со временем «становится» им
- **Стоит реалистично** — $15-25/мес при полной нагрузке

Главные изменения от 2.0 → 3.0:

| Аспект | Vera 2.0 | Vera 3.0 |
|---|---|---|
| Архитектура | Монолит с Graphiti для всего | 13 независимых модулей, Graphiti — один из инструментов |
| Хранилище | SQLite + Neo4j Aura | Postgres + pgvector + Neo4j (selective) |
| Обработка событий | Все одинаково (15 LLM/event) | Tiered: raw → triage → selective deep |
| Workflows | Ad-hoc Python tasks | Hatchet (durable, observable) |
| Observability | Custom dashboard | Langfuse + OpenTelemetry |
| Деплой | Полная пересборка | Per-module rebuild |
| Стоимость | $200-400/мес | $15-25/мес |
| Время backfill 17 мес | ~1.5 года | ~5 дней, $10-20 |

---

## 1. Ресёрч существующих решений

Прежде чем что-то писать с нуля — посмотрел что уже есть и что Дима потенциально изобретает заново.

### 1.1 Личные AI / Second Brain проекты

#### Khoj (https://khoj.dev)
**Что**: open-source self-hosted personal AI assistant.
**Что умеет**: индексирует документы (MD/PDF/email), ищет, отвечает с LLM, поддерживает Notion/GitHub/Web как источники.
**Лицензия**: AGPL.
**Используем?**: **Частично — UI идеи + структура multi-source интеграции**. Свой код для триажа и моделирования Димы — у них этого нет.

#### mem0 (https://github.com/mem0ai/mem0)
**Что**: "memory layer for AI assistants" — drop-in для добавления долгосрочной памяти любому LLM-приложению.
**Что умеет**: `add_memory()`, `search()`, hybrid vector+graph хранение, multi-LLM через LiteLLM.
**Лицензия**: Apache 2.0.
**Используем?**: **ДА — как базу для слоя памяти**. Заменим часть нашего custom code на их API. Это значительно сократит то что надо писать.

#### Letta (формально MemGPT, https://letta.com)
**Что**: stateful agents с управлением памятью (hierarchical memory: working/archival/recall).
**Что умеет**: симулирует «бесконечный контекст» через memory swap, поддерживает character/persona.
**Лицензия**: Apache 2.0.
**Используем?**: **Идеи для self-model**, не сам код. Letta сфокусирована на агентном character, нам нужен другой акцент.

#### Cognee (https://cognee.ai)
**Что**: AI memory framework — комбинирует graph + vector + relational в одном.
**Что умеет**: pipeline ingestion → knowledge graph extraction → hybrid search.
**Лицензия**: Apache 2.0.
**Используем?**: **ДА — рассмотреть как замену Graphiti** для тяжёлой обработки. Cognee легче и модульнее.

#### R2R by SciPhi (https://r2r-docs.sciphi.ai)
**Что**: production-ready RAG system с multi-tenancy и hybrid retrieval.
**Используем?**: **Нет, overkill для одного пользователя.**

#### Rewind.ai, Pieces, Mem.ai
Коммерческие закрытые. Только для inspiration.

**Вывод по 1.1**: mem0 + Cognee покрывают ~60% того что нам надо в слое памяти. Свой код будет только для domain-specific вещей (модель Димы, reaction patterns, многоисточниковый ingestion с нашими специфичными API).

---

### 1.2 LLM-роутинг и gateway

#### LiteLLM (используем сейчас)
**Плюсы**: 100+ провайдеров, multi-key rotation, fallbacks.
**Минусы**: cost tracking ненадёжен (мы это выявили на $12 burn), сложно настроить per-provider tweaks.
**Решение**: **остаёмся на LiteLLM** + добавляем Langfuse для реального cost tracking.

#### Portkey (https://portkey.ai)
**Плюсы**: 1600+ моделей, лучше observability чем LiteLLM, есть кэширование.
**Минусы**: open-source часть менее полная чем у LiteLLM.
**Решение**: рассмотреть как альтернативу. Если Langfuse + LiteLLM не закроют observability — переход.

#### OpenRouter
**Плюсы**: один аккаунт → много моделей.
**Минусы**: они берут маржу, теряем direct free-tier бенефиты.
**Решение**: **только как один из провайдеров для free moделей** (что уже).

---

### 1.3 Job orchestration

Сейчас у нас ad-hoc Python tasks через `asyncio` + systemd. Это **ненадёжно** (рестарт = потеря в полёте), сложно отлаживать, нет ретраев.

#### Hatchet (https://hatchet.run)
**Что**: distributed task queue с durable execution. Python-first, self-hostable.
**Используем?**: **ДА — основа для всех background workflows** (триаж, консолидация, backfill).

#### Temporal
**Что**: bulletproof workflow engine. Используется Uber, Snap, Coinbase.
**Решение**: overkill для нашего масштаба. Hatchet проще.

#### Celery
**Что**: ветеран task queue, Redis/RabbitMQ broker.
**Решение**: можно, но Hatchet модернее. Celery если у разработчика большой опыт с ним.

#### Inngest
**Что**: event-driven workflows, отлично для «событие → обработка» паттернов.
**Решение**: TypeScript-first, для Python экосистема слабее. Hatchet лучше.

**Вывод**: **Hatchet** для всех async workflows.

---

### 1.4 Observability

#### Langfuse (https://langfuse.com)
**Что**: LLM observability — traces, costs, prompts management, self-hostable.
**Используем?**: **ДА** — критически важно для контроля стоимости.

#### Helicone
**Что**: LLM proxy + logging + caching.
**Решение**: альтернатива, но Langfuse более mature как self-hosted.

#### OpenTelemetry (для не-LLM)
**Что**: стандарт distributed tracing.
**Используем?**: **ДА** для трейсов между микросервисами.

---

### 1.5 Хранилище

#### Текущее: SQLite + Neo4j Aura
**Проблемы**: SQLite не масштабируется на годы, два разных DB для управления.

#### Postgres + pgvector + Neo4j
**Преимущества**:
- Postgres = реляционные данные + JSON + векторы (через pgvector) в одной БД
- Neo4j остаётся только для граф-структурного знания
- Стандартные tools (backup, replication, observability) работают из коробки
**Используем?**: **ДА — миграция на Postgres**.

#### sqlite-vec
**Что**: SQLite extension для векторного поиска.
**Решение**: можно, если хотим оставить SQLite. Но проиграем Postgres'у в гибкости.

#### Qdrant / Weaviate / Chroma
**Решение**: для нашего масштаба (миллионы событий, не миллиарды) — pgvector хватит. Меньше moving parts.

---

### 1.6 Source ingestion

#### n8n / Activepieces
**Что**: open-source Zapier alternatives с 200+ интеграций.
**Используем?**: **ДА — для будущих источников** (FB, IG, LinkedIn, Slack). Свои адаптеры — только для тех где требуется специфика (Telegram userbot).

---

### 1.7 Что Дима НЕ должен изобретать заново

| Что мы сейчас писали custom | Что использовать |
|---|---|
| Token pool с ротацией | LiteLLM router + redis-based bucket |
| Cost tracking | Langfuse traces |
| Job scheduling | Hatchet |
| Background backfill orchestration | Hatchet workflows |
| Wiki хранилище | Markdown файлы в Git-репо (одновременно версионирование) |
| Self-model storage | Markdown в Git |
| Backup стратегия | Postgres pg_dump + automated S3 sync |
| Secrets management | Doppler / Infisical / 1Password Connect |

### 1.8 Что мы должны писать сами (это уникальное)

- **Модель Димы** — кастомные промпты, маппинг его специфики
- **Reaction patterns** — наблюдение за выходящими + контекст
- **Domain entity classification** — Егоров → босс, Маша → жена и т.д.
- **Custom источники** — Telegram MTProto userbot, специфичный Gmail filter
- **Бот-интерфейс Веры** — стиль ответов, проактивные нотификации

---

## 2. Архитектура Vera 3.0

### 2.1 Принципы

1. **Modular monolith → microservices гибрид**: разделение по доменам (ingestion, brain, query) в отдельные контейнеры, но один shared library для общего.
2. **Free first**: каждый AI-вызов сначала через бесплатные провайдеры, paid — fallback с жёсткими caps.
3. **Durable everywhere**: ни одна задача не теряется при рестарте.
4. **Observable everywhere**: каждый LLM-вызов трейсится, каждая стоимость учитывается, каждая ошибка алертится.
5. **One source of truth per concern**: registry для LLM, Postgres для events, Neo4j для graph, Git для wiki.
6. **Independent deploy**: каждый модуль обновляется без пересборки других.
7. **Bring your own data**: всю историю можно загнать ретроактивно, не ждать новых событий.

### 2.2 Высокоуровневая схема

```
┌─────────────────────────────────────────────────────────┐
│                    SOURCES (внешние)                    │
│  Gmail │ Telegram │ Instagram │ FB │ Calendar │ ...    │
└─────────────────┬───────────────────────────────────────┘
                  │ webhook / poll
                  ↓
┌─────────────────────────────────────────────────────────┐
│  INGESTORS (тонкие адаптеры — по контейнеру на источник)│
│  ingestor-gmail │ ingestor-telegram │ ingestor-ig │ ...│
└─────────────────┬───────────────────────────────────────┘
                  │ raw event (JSON)
                  ↓
┌─────────────────────────────────────────────────────────┐
│  GATEWAY (один FastAPI front)                           │
│  - принимает webhook'и                                  │
│  - валидация / dedup                                    │
│  - публикация в Hatchet event bus                       │
└─────────────────┬───────────────────────────────────────┘
                  │ event published
                  ↓
┌─────────────────────────────────────────────────────────┐
│  HATCHET (orchestrator)                                 │
│  - durable execution                                    │
│  - retries, scheduling                                  │
│  - наблюдает за workflow steps                          │
└─────────────────┬───────────────────────────────────────┘
                  │ task spawned
                  ↓
┌──────────────────────────────────────────────────────────┐
│  BRAIN WORKERS (по типу обработки)                       │
│  ┌─────────────┬──────────────┬─────────────────────┐    │
│  │  triage     │  graph       │  consolidation      │    │
│  │  (cheap)    │  (selective) │  (nightly)          │    │
│  └─────┬───────┴──────┬───────┴──────────┬──────────┘    │
└────────┼──────────────┼──────────────────┼───────────────┘
         │              │                  │
         ↓              ↓                  ↓
┌────────────────────────────────────────────────────────┐
│  STORAGE                                               │
│  ┌──────────────┐ ┌────────────┐ ┌────────────────┐    │
│  │  Postgres    │ │  Neo4j     │ │  Git repo      │    │
│  │  + pgvector  │ │  Aura      │ │  (wiki + docs) │    │
│  └──────────────┘ └────────────┘ └────────────────┘    │
└────────────────────────────────────────────────────────┘
         │
         ↓ (для пользовательских запросов)
┌──────────────────────────────────────────────────────────┐
│  QUERY ENGINE (hybrid search + synthesis)                │
└─────────────────┬────────────────────────────────────────┘
                  │
                  ↓
┌──────────────────────────────────────────────────────────┐
│  BOT (Telegram + Web UI)                                 │
│  - отвечает Диме                                         │
│  - проактивные нотификации                               │
│  - дашборд                                               │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  CROSS-CUTTING                                           │
│  - LiteLLM (LLM gateway) ◄── каждый воркер использует    │
│  - Langfuse (observability) ◄── трейсы LLM-вызовов       │
│  - Token registry (Postgres) ◄── ключи провайдеров       │
└──────────────────────────────────────────────────────────┘
```

### 2.3 Модули (13 контейнеров)

#### Платформенные (3)
1. **postgres** — Postgres 16 + pgvector. БД для всего.
2. **hatchet** — workflow orchestrator. Self-hosted.
3. **langfuse** — LLM observability. Self-hosted.

#### Gateway (1)
4. **gateway** — FastAPI front для webhook'ов от источников + health/admin endpoints.

#### Ingestors (5, расширяемо)
5. **ingestor-gmail** — Gmail API watcher + bulk backfill
6. **ingestor-telegram** — MTProto userbot + bot для DM
7. **ingestor-calendar** — Google Calendar watcher
8. **ingestor-instagram** — ManyChat webhook (когда подключим)
9. **ingestor-facebook** — FB Pages webhook (когда подключим)

(Будущие: ingestor-linkedin, ingestor-whatsapp, ingestor-slack, ingestor-notion — каждый отдельный контейнер по той же шаблону).

#### Brain (3)
10. **brain-triage** — Hatchet worker: триаж новых событий. Lightweight LLM.
11. **brain-graph** — Hatchet worker: deep extraction для важных событий. Graphiti/Cognee.
12. **brain-jobs** — Hatchet worker: nightly consolidation, weekly reflection.

#### User-facing (2)
13. **bot-telegram** — Telegram-бот для общения Димы с Верой
14. **dashboard** — Web UI для дашборда, настроек, ручного запуска задач

### 2.4 Shared library

Один Python пакет `vera_shared` который импортируется во все модули:

```
vera_shared/
├── llm/
│   ├── registry.py       # SSOT для providers, models, prices
│   ├── client.py         # LiteLLM wrapper с Langfuse трейсингом
│   └── prompts/          # все промпты, версионированы
├── db/
│   ├── models.py         # SQLAlchemy/Pydantic модели
│   ├── engine.py
│   └── migrations/
├── events/
│   ├── schema.py         # Event Pydantic model
│   └── publisher.py      # публикация в Hatchet
├── memory/
│   ├── search.py         # hybrid search API
│   └── wiki.py           # доступ к wiki файлам
└── tokens/
    ├── registry.py       # paid/free классификация ключей
    └── pool.py           # ротация
```

**Тяжёлые либы** (litellm, graphiti, cognee) — только в тех модулях где реально нужны. Triage worker не тянет Graphiti.

---

## 3. Стратегия токенов — paid vs free

### 3.1 Принципы

- **Каждый токен в БД помечен**: `tier: 'free' | 'paid'`, `daily_cost_cap_usd: float`
- **Free всегда в приоритете**: роутер пробует free, потом paid
- **Hard cost caps**: на каждый paid токен — daily cap. Достиг — отключение до следующего дня.
- **Глобальный cost cap**: на всю систему — например $1/день. Достиг — стоп всех paid вызовов.
- **Cost tracking через Langfuse**: реальный счёт сверяется с биллинг-API провайдеров раз в час.

### 3.2 Расширение схемы tokens

```sql
ALTER TABLE tokens ADD COLUMN tier TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'paid', 'trial'));
ALTER TABLE tokens ADD COLUMN daily_cost_cap_usd FLOAT;
ALTER TABLE tokens ADD COLUMN monthly_cost_cap_usd FLOAT;
ALTER TABLE tokens ADD COLUMN provider_billing_url TEXT;  -- куда смотреть реальный счёт
ALTER TABLE tokens ADD COLUMN notes TEXT;  -- "забанили 2026-06-04", "карта привязана"
```

### 3.3 Per-provider tier mapping

| Provider | Tier (default) | Notes |
|---|---|---|
| cerebras (free tier) | free | очередь часто загружена |
| groq | free | 8000 TPM на ключ |
| gemini (free) | free | 1500 req/день |
| openrouter (free models) | free | 50 req/день |
| voyage (с картой, в free quota) | free | 200M tokens/мес free |
| nvidia NIM | free | 1000 calls life-time на ключ |
| sambanova | free | ~20 req/min |
| mistral (experimental) | free | 1 req/sec |
| --- | --- | --- |
| gemini (paid) | paid | $0.075/M flash |
| deepseek | paid | $0.27/M chat |
| openai | paid | дорого |
| anthropic | paid | очень дорого |
| voyage (за quota) | paid | $0.06/M |

### 3.4 Routing policy

```python
class RoutingPolicy:
    """SSOT для приоритетов."""
    chat_fast = [
        ("cerebras", "free"),
        ("groq", "free"),
        ("gemini", "free"),
        ("openrouter", "free"),
        ("sambanova", "free"),
        ("nvidia", "free"),
        ("mistral", "free"),
        ("gemini", "paid"),   # ← только после всех free
        ("deepseek", "paid"),
        ("anthropic", "paid"),
    ]
```

При вызове: проходим по списку, первый available (есть ключ + не в cooldown + не превысил cap) — используем.

### 3.5 Cost cap enforcement

Перед каждым **paid** вызовом:
```python
def can_call_paid(token: Token, estimated_cost: float) -> bool:
    if token.tier == 'free':
        return True
    if token.daily_cost_used_usd + estimated_cost > token.daily_cost_cap_usd:
        return False
    if global_daily_cost_used + estimated_cost > GLOBAL_DAILY_CAP:
        return False
    return True
```

### 3.6 Реальное cost tracking

Раз в час job `verify_real_billing`:
- Pull актуальный billing с Google AI Studio API / OpenAI API / etc.
- Сравнивает с нашим внутренним counter
- Если расхождение > 20% — алерт в Telegram + корректирует наши данные

---

## 4. Спецификация модулей

### 4.1 gateway (FastAPI)

**Цель**: единая точка входа для webhook'ов.

**Endpoints**:
- `POST /event/{source}` — приём события от любого ingestor
- `POST /webhook/telegram` — Telegram bot updates
- `POST /webhook/manychat` — ManyChat (IG)
- `GET /healthz` — health
- `GET /api/admin/*` — админ-API (защищён auth)

**Что делает с приходящим event'ом**:
1. Валидация структуры (Pydantic)
2. Дедупликация (по source + source_id)
3. INSERT в `events` table в Postgres
4. Публикация в Hatchet (`event.created` topic)

**Стоимость**: $0 (никаких LLM).

**Hosting**: 1 контейнер, 256MB RAM достаточно.

---

### 4.2 ingestor-gmail

**Цель**: вытягивать письма из Gmail, как real-time так и историю.

**Подкомпоненты**:
1. **Watcher** — Pub/Sub подписка на Gmail (push notifications)
2. **Poller** — раз в 5 минут проверяет на случай если push потеряется
3. **Backfill** — на запрос: историческая выгрузка за период

**API**:
- `POST /ingestor/gmail/start_backfill` — запуск backfill за период
- `GET /ingestor/gmail/status` — состояние

**Зависимости**:
- Google Cloud Pub/Sub
- Gmail API (OAuth2, scope `gmail.readonly`)

**Стоимость**: $0 (Google API бесплатный).

---

### 4.3 ingestor-telegram

**Цель**: захватывать все DM, групповые чаты, каналы которые подписан Дима.

**Подкомпоненты**:
1. **Bot mode** — для общения Димы с Верой
2. **Userbot mode** — MTProto через Pyrogram, читает все диалоги Димы

**Конфликт ролей**: userbot и bot должны быть разными аккаунтами Telegram. Один номер — userbot. Один — бот.

**API**:
- `POST /ingestor/telegram/backfill_dialog` — backfill истории чата
- `POST /ingestor/telegram/sync` — синхронизация всех новых сообщений

---

### 4.4 ingestor-calendar / instagram / facebook

По аналогии. Каждый — отдельный контейнер с минимальной зависимостью.

---

### 4.5 brain-triage

**Цель**: на каждое новое событие — контекстный триаж.

**Триггер**: Hatchet workflow `event.triage` запускается на каждое новое событие.

**Входы**:
- Событие (текст, метаданные)
- Текущий контекст Димы (активные темы, важные люди, активные проекты) — из `wiki/dima.md`

**Промпт**:
```
You are Vera, Dima's personal AI memory.
Dima is currently: {{active_state}}
Important people: {{key_people}}
Active projects: {{active_projects}}

Here is a new event from {{source}}:
---
{{event_content}}
---

Extract structured JSON:
- importance (0-100)
- topics (list)
- people_mentioned (list, normalized to canonical IDs if known)
- signals: list of {type: event|task|news|offer, summary, date?}
- active_topic_matches: which of Dima's active topics this matches
- needs_action: boolean

Free models can handle this — gpt-oss-120b, Llama-3.3-70B.
```

**Provider chain**: cerebras → groq → gemini-free → openrouter → ... (см. 3.4)

**Stored**: результат в `event_metadata` table (per-event JSONB).

**Trigger condition для следующего шага**: если `importance > 75` OR `active_topic_matches` непустой → enqueue `brain-graph`.

**Стоимость**: ~$1-3/мес (в основном free).

---

### 4.6 brain-graph

**Цель**: для важных событий — построение structured knowledge graph.

**Триггер**: только если триаж сказал «важно».

**Backend**: пробуем сначала **Cognee**, fallback на нашу обёртку Graphiti. Cognee — более модульный и legkий.

**Что строит**:
- Entities (Person, Project, Organization, Place, etc.)
- Relations (works_at, married_to, related_to_project, etc.)
- Temporal facts (date-stamped)

**Provider chain** (нужен strict json_schema): groq → openrouter → cerebras → gemini

**Стоимость**: $5-10/мес (только для ~10% событий).

---

### 4.7 brain-jobs (consolidation + reflection)

**Цель**: периодическая обработка — nightly digest, weekly reflection.

**Расписание**:
- 4:00 UTC daily: `consolidate_yesterday` — обновить wiki по людям/проектам
- 5:00 UTC Sunday: `weekly_reflection` — обновить `dima.md` и `vera_self.md`
- 6:00 UTC Monday: `pattern_mining` — поиск новых поведенческих паттернов

**Все jobs** — Hatchet workflows с автоматическими retries.

**Стоимость**: $3-5/мес (использует Gemini Flash/Pro).

---

### 4.8 brain-search (query engine)

**Цель**: отвечать на запросы Димы.

**Гибридный поиск**:
1. FTS5/Postgres FTS — точные совпадения
2. pgvector semantic search — по смыслу
3. Wiki lookup — есть ли готовый ответ
4. Graph traversal (Neo4j) — если запрос про связи

**Synthesis**: один LLM-вызов на Gemini Flash или Claude Haiku.

**API**:
- `POST /search` — запрос Димы → ответ

**Стоимость**: $3-5/мес (~30 запросов/день × $0.005).

---

### 4.9 bot-telegram

**Цель**: общение Димы с Верой через Telegram.

**Особенности**:
- Streaming ответов (как ChatGPT)
- Inline buttons для quick actions
- Voice messages (через Whisper)
- Multimodal: фото → Gemini Vision

**Стоимость**: входит в brain-search.

---

### 4.10 dashboard (web)

**Цель**: Web UI для мониторинга, настроек, ручных операций.

**Stack**: React/Next.js или Streamlit (быстрее для MVP).

**Pages**:
- Overview: токены, события, costs, последние ответы
- Tokens: добавить/убрать/настроить cap
- Sources: настроить источники
- Wiki browser: чтение wiki файлов
- Manual jobs: запустить backfill / consolidation
- Costs: real-time + history

**Hosting**: отдельный контейнер.

---

## 5. План миграции и сохранения данных

### 5.1 Что сохранить ДО сноса

Создаём папку `~/vera2-backup/` на сервере и в моём локальном репо:

#### Базы данных
- `vera.db` (SQLite) — все события 17 месяцев
- Neo4j Aura dump — все узлы и связи
- `/data/wiki/` если что-то накопилось

#### Конфигурация
- `.env` файл сервера
- `docker-compose.yml`
- systemd units

#### Токены и доступы
- Все ключи из `tokens` table (расшифрованные временно для миграции)
- OAuth refresh tokens (Gmail, etc.)
- DEPLOY_SECRET, SESSION_SECRET, и прочие env
- ManyChat API token
- Voyage cards info

#### Код
- Git repo как есть — на ветке `vera2-final`
- Документация что было сделано

### 5.2 Команды экспорта

```bash
# 1. SQLite dump
docker exec vera-vera-core-1 sqlite3 /data/vera.db ".backup '/data/backup.db'"
scp hetzner-root:/var/lib/docker/volumes/vera_vera_data/_data/backup.db ./backups/vera2/

# 2. Neo4j dump (через Aura UI или cypher-shell)
# Aura → Manage → Snapshots → Download

# 3. ENV + configs
ssh hetzner-root "tar czf /tmp/vera2-configs.tar.gz /var/www/vera/.env /var/www/vera/docker-compose.yml /etc/systemd/system/vera-*"
scp hetzner-root:/tmp/vera2-configs.tar.gz ./backups/vera2/

# 4. Tokens table extract (расшифрованный CSV для миграции)
docker exec vera-vera-core-1 python3 -c "
import asyncio, os
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models import Token
from vera_shared.tokens.repository import decrypt
SECRET = os.environ['SESSION_SECRET']
async def main():
    async with get_session() as s:
        rows = (await s.execute(select(Token))).scalars().all()
    for t in rows:
        plain = decrypt(t.token, SECRET) if t.token.startswith('enc') else t.token
        print(f'{t.provider}|{t.label}|{plain}|{t.tier_or_default}|{t.daily_cost_cap_usd or \"\"}')
asyncio.run(main())
" > ./backups/vera2/tokens.csv
```

### 5.3 Перенос в Vera 3.0

Vera 3.0 на старте импортирует:
- События из `vera.db` → новая Postgres `events` table (одноразовый job)
- Токены из CSV → новая `tokens` table с правильной tier-классификацией
- Wiki (если есть) → в Git repo `vera-wiki`

### 5.4 Снос Vera 2.0

После успешного импорта + 1 неделя параллельной работы (Vera 2.0 в read-only, Vera 3.0 в production):

```bash
ssh hetzner-root
cd /var/www/vera
docker compose down -v  # удаляет контейнеры И volumes (!!!)
rm -rf /var/www/vera
docker system prune -af  # удаляет все неиспользуемые images
```

⚠️ **Обязательно**: до этого момента — все бэкапы должны быть проверены восстановлением!

---

## 6. Deploy & CI/CD

### 6.1 Структура репозитория

```
vera/
├── infra/
│   ├── docker-compose.yml          # все 13 сервисов
│   ├── docker-compose.dev.yml      # для local
│   └── nginx.conf
├── shared/                         # Python package vera_shared
│   ├── pyproject.toml
│   └── vera_shared/
├── services/
│   ├── gateway/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── src/
│   ├── ingestor-gmail/
│   ├── ingestor-telegram/
│   ├── brain-triage/
│   ├── brain-graph/
│   ├── brain-jobs/
│   ├── brain-search/
│   ├── bot-telegram/
│   └── dashboard/
├── docs/
└── .github/workflows/
    ├── deploy.yml                  # deploys только изменённые сервисы
    └── tests.yml
```

### 6.2 Independent module deploys

**Каждый сервис — свой Dockerfile + pyproject.toml.** Изменился только `brain-triage/`? Пересобираем **только его**.

GitHub Actions matrix:
```yaml
jobs:
  detect-changes:
    outputs:
      services: ${{ steps.changes.outputs.services }}
    steps:
      - uses: dorny/paths-filter@v3
        with:
          filters: |
            gateway: 'services/gateway/**'
            ingestor-gmail: 'services/ingestor-gmail/**'
            ...

  deploy:
    needs: detect-changes
    strategy:
      matrix:
        service: ${{ fromJSON(needs.detect-changes.outputs.services) }}
    steps:
      - ssh hetzner: "vera-deploy ${{ matrix.service }}"
```

### 6.3 Docker cleanup

#### При build
- **Multi-stage builds**: финальный image не содержит build tools
- **`.dockerignore`** агрессивный

#### При deploy
- Тегирование по commit SHA: `vera-gateway:abc123def`, `:latest` обновляется только после smoke
- Старые images удаляются: keep last 3 per service
- `docker system prune --filter "until=72h" -af` после каждого деплоя

#### Регулярная очистка
```yaml
# В docker-compose добавляем:
services:
  prune:
    image: docker:cli
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: sh -c "while true; do docker system prune -af --filter 'until=24h'; sleep 86400; done"
    restart: unless-stopped
```

### 6.4 Auto-deploy

GitHub Actions при push to master:
1. Detect changed services
2. Build только их
3. Push в Docker Hub / ghcr.io
4. SSH в Hetzner → `vera-deploy <service>` который:
   - `git pull`
   - `docker compose pull <service>`
   - `docker compose up -d <service>`
   - Smoke test (HTTP healthcheck)
   - Если smoke fail — rollback на :previous tag
5. Notify Telegram

### 6.5 Configuration management

**Secrets**: Doppler или Infisical (бесплатные tiers есть). Все ENV → из Doppler в момент деплоя.

**App config**: YAML файлы в Git, переопределяются ENV.

---

## 7. Метрики качества

Как мы поймём что Vera 3.0 реально лучше:

### Технические
- Время backfill 1 события: < 2 секунды (vs 5 минут в 2.0)
- Стоимость на событие: < $0.005 (vs $0.07)
- Latency Telegram-ответа: < 3 секунды
- Uptime: 99.5%
- Стоимость в месяц: < $25 при 3000+ событий/день

### Качественные (UX)
- Дима спрашивает «кто такой X?» — ответ за < 3 секунд с реальными деталями
- Вера сама присылает 2-5 полезных нотификаций в день (не больше)
- Драфты ответов от Веры Дима принимает без правки в > 50% случаев
- Через 2 месяца — Вера правильно угадывает реакцию Димы в 70% случаев

### Cost-discipline
- 0 incidents типа «$25 burn»
- Платные ключи никогда не выходят за daily cap
- Дашборд показывает реальный $ в real-time

---

## 8. Открытые вопросы и риски

### 8.1 Открытые вопросы

1. **Wiki в Git: где хранится?** GitHub приватный repo с auto-sync с сервера? Или просто папка `/data/wiki/` + бэкапы?
2. **Доступ Димы**: только Telegram или нужен веб-чат тоже?
3. **Шаринг с близкими**: должна ли жена видеть некоторые ответы Веры?
4. **Контроль приватности**: Дима может сказать «забудь это сообщение» — Вера удаляет из всех слоёв?
5. **Voice**: Whisper для речи Димы — это входит в MVP?
6. **Multimodal**: фото от Димы (например квитанции) — это MVP или v3.1?
7. **Команда AI агентов**: например агент-исследователь, агент-планировщик — это позже?

### 8.2 Риски

| Риск | Митигация |
|---|---|
| Cognee/mem0 окажутся сырыми → откат к custom коду | MVP: реализуем минимум через них, если не зайдёт — fallback на собственное |
| Hatchet добавит сложности | Backup: APScheduler + Postgres outbox (проще, менее durable) |
| Постгрес расход RAM на Hetzner 2GB | Мониторинг, при необходимости — upgrade на 4GB ($16/мес) |
| Free провайдеры начнут вводить более жёсткие лимиты | Каждые 3 мес — ревью провайдеров, проактивная регистрация новых |
| Большая ёмкость данных за годы | Архивирование старых событий в S3, в pgvector — только последние 12 мес |

---

## 9. Roadmap (поэтапная реализация)

### Phase 0 — Подготовка (1 неделя)
- [ ] Полный бэкап Vera 2.0 (данные, конфиги, токены)
- [ ] Создание нового репо `vera3/`
- [ ] Setup infra: Postgres + Hatchet + Langfuse в Docker Compose
- [ ] CI/CD скелет
- [ ] Doppler secrets

### Phase 1 — Capture layer (2 недели)
- [ ] gateway сервис
- [ ] ingestor-gmail (миграция текущего кода)
- [ ] ingestor-telegram (миграция)
- [ ] ingestor-calendar
- [ ] Postgres schema + миграция событий из SQLite

**К концу Phase 1**: все источники работают, события льются в новый Postgres.

### Phase 2 — Search layer (1 неделя)
- [ ] brain-search с hybrid retrieval
- [ ] bot-telegram (миграция текущего)
- [ ] dashboard (minimum viable)

**К концу Phase 2**: Дима может спрашивать Веру через TG, она ищет в полной памяти.

### Phase 3 — Triage layer (2 недели)
- [ ] brain-triage с context-aware промптом
- [ ] Token registry с paid/free + caps
- [ ] LLM client с Langfuse трейсингом
- [ ] Routing policy с free first

**К концу Phase 3**: каждое событие триажится, теги/важность видны.

### Phase 4 — Deep memory (3 недели)
- [ ] brain-graph (Cognee experiment + fallback)
- [ ] Migration: важные события из старого Graphiti → новый граф
- [ ] brain-jobs: nightly consolidation
- [ ] Wiki генерация

**К концу Phase 4**: Wiki заполняется, граф для важного работает.

### Phase 5 — Personality + Behavior (3 недели)
- [ ] Self-model contour (модель Димы + модель Веры)
- [ ] Reaction observation: фиксация контекста + триггера
- [ ] Pattern mining
- [ ] Proactive notifications

**К концу Phase 5**: Вера предсказывает реакции Димы, пишет в его стиле.

### Phase 6 — Cleanup (1 неделя)
- [ ] Полный снос Vera 2.0
- [ ] Финальная документация
- [ ] Onboarding доки для будущих модулей

**Итого**: **~12 недель** реальной работы (с учётом отладки и багфиксов — 14-16).

---

## 10. Что точно НЕ входит в MVP

Чтобы избежать scope creep:
- Multi-user (только Дима)
- Mobile app (только Telegram + web)
- Voice generation (только text)
- Внешние агенты (поиск в интернете для Веры — позже)
- Plugins/extensions

---

## 11. Команда

Минимум для MVP: 1 разработчик (full-stack Python + DevOps) на 3-4 месяца full-time.

Если Дима сам — то 6-8 месяцев в свободное время.

---

## 12. Бюджет

### Infrastructure
- Hetzner CX22 (2 vCPU, 4GB RAM, 40GB SSD): €6.99/мес
- Neo4j Aura Free: $0
- Domain + Cloudflare: ~$0
- Doppler / Infisical: free tier
- Total infra: **~$8/мес**

### AI tokens
- Триаж + search + consolidation: $15-25/мес (см. секцию 4)
- Реальный peak если все paid задействованы: до $50/мес
- Hard global cap: $50/мес (Дима ставит в admin UI)

### One-time
- Разработка: 12-16 недель (если контрактор — оценка ~$15-30k; если сам Дима — время + кофе)

---

## 13. Заключение

Vera 3.0 — это не доработка Vera 2.0, это **переосмысление с уроками**:

1. ✅ Правильный инструмент для каждой задачи (не Graphiti для всего)
2. ✅ Каждый токен помечен free/paid с приоритетом free
3. ✅ Модульная архитектура — обновление одного модуля не ломает другие
4. ✅ Observable: каждый $ виден в real-time
5. ✅ Использование готовых фреймворков: mem0, Cognee, Hatchet, Langfuse
6. ✅ Уникальный код — только там где специфика (модель Димы, reaction patterns)

**Главное обещание**: за $15-25/мес — Вера видит **всё**, помнит **всё**, понимает **то что важно**, отвечает **за секунды**, и со временем **становится продолжением Димы в цифре**.

---

---

## 14. Backfill глубокой истории — конкретный план

### 14.1 Размер задачи (год+ истории)

Для 1 года с реальными источниками — оценка по personal volume:

| Источник | Объём за год | Доступность исторически |
|---|---|---|
| Gmail | 50k-150k писем | ✅ Полностью (Gmail API) |
| Telegram личные (DM) | 20k-50k сообщений | ✅ Полностью (MTProto userbot) |
| Telegram группы (участник) | 30k-100k | ⚠️ Только то на что подписан |
| Calendar | 1k-3k событий | ✅ Полностью |
| ChatGPT/Claude conversations | 2k-10k сообщений | ✅ Через export ZIP |
| Trello cards + activity | 5k-20k | ✅ Полностью (API) |
| Notion pages | 1k-5k | ✅ Полностью (API) |
| Google Docs | 0.5k-2k | ✅ Через Drive API |
| Perplexity history | 0.5k-3k | ❌ Нет API, только скрейп или manual export |
| Instagram DMs | 5k-15k | ⚠️ Только последние 30-90 дней без App Review |
| Facebook Messenger | 3k-10k | ⚠️ То же |
| Slack workspaces | 10k-30k | ✅ Полностью (Events API + history) |
| WhatsApp | 5k-20k | ❌ Только manual chat export |
| LinkedIn | 0.5k-2k | ⚠️ Через download archive |
| Browser history | 50k-200k entries | ⚠️ Локально |

**Реалистично доступно за год**: **150k-350k событий**.

### 14.2 Стоимость и время полного backfill 1 года

Базируюсь на новой архитектуре (один LLM-вызов на событие для триажа, selective ~10% → deep extraction).

**Сценарий 1: 200k событий, только free пул**

| Фаза | Объём | Free пул capacity | Время |
|---|---|---|---|
| Embedding (Voyage) | 200k × 800 = 160M токенов | 1B/мес free | ~5-10 часов |
| Triage (lightweight) | 200k × 3000 = 600M токенов | ~30M/день из free | **20-25 дней** |
| Deep graph (~10%) | 20k × 50k = 1B токенов | Сложнее, очередь | **30-60 дней** |
| Consolidation | Инкрементально nightly | $3-5 | непрерывно |

Free-only: **2-3 месяца на полный backfill 1 года**. $0.

**Сценарий 2: 200k событий, гибрид (free + paid Gemini Flash для скорости)**

| Фаза | Стоимость paid | Время |
|---|---|---|
| Embedding | $0 (Voyage free) | 5-10 часов |
| Triage | $45 (600M × $0.075/M на Gemini Flash) | **24-48 часов** |
| Deep graph | $30 (через paid Gemini) | **2-4 дня** |
| Consolidation | $5 | непрерывно |
| **Итого** | **~$80** | **~1 неделя** |

**Сценарий 3: 200k событий, paid full pour** (для скорости любой ценой)
- Время: 2-3 дня
- Цена: ~$150

### 14.3 Backfill workflow (Hatchet)

```python
@workflow(name="historical_backfill")
class HistoricalBackfill:
    """
    Запускается одной кнопкой "Загнать историю" из дашборда.
    Прогресс виден real-time.
    Можно паузить/возобновлять.
    """

    @step(timeout="30m")
    async def plan(ctx):
        """1. Прикинуть scope, дать estimate юзеру"""
        sources = ctx.input.sources  # ["gmail", "telegram", "calendar"]
        period = ctx.input.period    # 2025-01-01 to 2026-06-08
        budget_usd = ctx.input.budget_cap

        plan = await estimate_backfill(sources, period)
        # plan = {events: 180k, time_free: 22 days, cost_paid: $72, ...}
        return plan

    @step(timeout="2h", retries=3)
    async def fetch_from_sources(ctx, plan):
        """2. Параллельно тянем raw events из всех источников"""
        results = await parallel([
            fetch_gmail(period),
            fetch_telegram(period),
            fetch_calendar(period),
        ])
        # Все события - в Postgres staging table
        return {fetched: 178k}

    @step(timeout="48h", retries=5)
    async def triage_all(ctx, fetched):
        """3. Триаж каждого события. Использует Hatchet rate limiting"""
        # Hatchet ограничит concurrency через rate_limits
        # Прогресс пишется в БД, юзер видит в real-time
        async for event_id in iter_unprocessed_events():
            await ctx.spawn(triage_event, event_id=event_id)
            # Hatchet сам управляет очередью, retries, budget
        return {triaged: 178k}

    @step(timeout="7d", retries=10)
    async def deep_extract_important(ctx, triaged):
        """4. Только для important > 75: глубокое извлечение"""
        # Аналогично, но фильтр по важности
        ...

    @step(timeout="2h")
    async def consolidate(ctx):
        """5. Финальная wiki-генерация"""
        await generate_all_wikis()
        return {wikis_built: 120}
```

### 14.4 Real-time progress в дашборде

```
┌─────────────────────────────────────────────────────────┐
│  Backfill история — 2025-01-01 to 2026-06-08            │
│  ▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱  46%      [Pause] [Cancel]   │
├─────────────────────────────────────────────────────────┤
│  Источники:                                              │
│    Gmail        ████████████ 67k/67k  ✓ done            │
│    Telegram     █████░░░░░░  19k/42k    ETA 4h           │
│    Calendar     ████████████ 2k/2k    ✓ done            │
│    Trello       ██░░░░░░░░░░  3k/14k    ETA 8h           │
├─────────────────────────────────────────────────────────┤
│  Фазы обработки:                                         │
│    ✓ Embeddings   178k/178k                              │
│    ▶ Triage      82k/178k    rate 4500/h  ETA 21h       │
│    ⧗ Deep graph   waiting                                │
├─────────────────────────────────────────────────────────┤
│  Бюджет: $42 / $100 cap     [по умолчанию paused при]   │
│  Реальные траты:                                         │
│    Voyage     $0    (free pool)                          │
│    Cerebras   $0    (free)                               │
│    Gemini     $38   (paid, гибрид)                       │
│    OpenAI     $0                                         │
└─────────────────────────────────────────────────────────┘
```

### 14.5 Контрольные ручки

- **Pause/Resume**: остановить и продолжить позже
- **Budget cap**: жёсткий потолок $X, при достижении — auto-pause
- **Speed/quality tradeoff**: тумблер «только free (медленно, $0)» vs «гибрид (быстро, $80)» vs «paid full ($150)»
- **Source filter**: backfill только Gmail, или всё, или конкретный период
- **Importance threshold**: только important > X (для быстрого MVP)

### 14.6 Recovery если что-то пошло не так

- Все события сохранены в `events` raw — никогда не теряются
- Если триаж упал — события без `triage_metadata` подберутся повторным запуском workflow
- Если deep_extract упал на N-м из 20k — следующий запуск пропустит обработанные, продолжит с N+1
- **Idempotency**: каждое событие имеет уникальный hash, повторная обработка не создаёт дубликатов

---

## 15. Подключение источников — фреймворк

### 15.1 Проблема жёсткого кодирования

В моём первом draft ТЗ написал «ingestor-gmail, ingestor-telegram, ingestor-instagram...» как отдельные контейнеры. Это **не масштабируется** — каждый новый источник требует кода и деплоя.

Правильно — **Connector Framework** с pluggable adapters.

### 15.2 Стандартный интерфейс

Все коннекторы реализуют один Pydantic-интерфейс:

```python
from abc import ABC, abstractmethod
from datetime import datetime
from typing import AsyncIterator
from vera_shared.events.schema import RawEvent

class SourceConnector(ABC):
    """Каждый источник — реализация этого интерфейса."""

    name: str               # "gmail", "telegram", "trello", "perplexity_import"
    capabilities: list[str] # ["realtime", "backfill", "bulk_import"]

    @abstractmethod
    async def authenticate(self, credentials: dict) -> None:
        """OAuth/API key/token setup."""

    @abstractmethod
    async def fetch_history(
        self, start: datetime, end: datetime, **kwargs
    ) -> AsyncIterator[RawEvent]:
        """Backfill. Yields events one by one."""

    @abstractmethod
    async def subscribe_realtime(self, callback) -> None:
        """Webhook setup или polling."""

    @abstractmethod
    async def parse_bulk_archive(self, file_path: str) -> AsyncIterator[RawEvent]:
        """Импорт из ZIP/CSV/JSON архива (ChatGPT export, WhatsApp chat, etc.)."""

    def health_check(self) -> dict:
        """Status источника для дашборда."""
```

### 15.3 Категории источников и их специфика

#### A. Cloud APIs с polling/push (легко)

| Источник | Auth | Real-time | Backfill | Archive |
|---|---|---|---|---|
| **Gmail** | OAuth | Pub/Sub watch | Messages API | — |
| **Google Calendar** | OAuth | Push notifications | Events API | — |
| **Google Drive/Docs** | OAuth | Changes API | Files API | — |
| **Trello** | API token | Webhooks | Boards/Cards API | — |
| **Notion** | Integration token | Webhooks (new) | Search API | — |
| **Slack** | OAuth | Events API | conversations.history | — |
| **GitHub** | PAT | Webhooks | REST API | — |
| **Outlook** | OAuth | Microsoft Graph | Microsoft Graph | — |

→ **Реализация**: тонкие коннекторы, ~100-200 строк каждый. Можно делать одного в день.

#### B. Сложные APIs (требуют userbot/scraping)

| Источник | Подход | Сложность |
|---|---|---|
| **Telegram (личные)** | MTProto via Pyrogram | Средне (есть готовый код в Vera 2.0) |
| **Instagram personal** | Meta Graph API + App Review | Высоко (недели на ревью) |
| **Facebook personal** | То же | Высоко |
| **WhatsApp** | WhatsApp Business API только | Не подходит для личного |
| **Perplexity** | Скрейпинг web UI | Низко-средне, fragile |
| **LinkedIn** | Только manual archive | Высоко для realtime |

→ **Реализация**: каждый по-своему, плюс manual archive import.

#### C. Cold archives (одноразовый импорт)

Многие сервисы дают экспорт всей истории в ZIP/JSON:

| Источник | Что в архиве | Как достать |
|---|---|---|
| **ChatGPT** | Все диалоги (JSON) | Settings → Data Export → ZIP по email |
| **Claude.ai** | Все диалоги | Settings → Privacy → Export |
| **WhatsApp** | Чат текст + media | Per-chat menu → Export chat |
| **LinkedIn** | Connections, messages, posts | Settings → Get a copy of your data |
| **Twitter/X** | Tweets, DMs | Settings → Download archive |
| **Facebook** | Всё | Settings → Download your information |
| **Google Takeout** | Любые Google сервисы | takeout.google.com |
| **Perplexity** | (Нет официального) | Скрейп или копипаст |

→ **Реализация**: один универсальный модуль `archive_importer/` с парсерами под каждый формат. Юзер заливает ZIP через дашборд, выбирает тип, парсер нормализует в RawEvent и шлёт в gateway.

#### D. Локальные данные (требуют клиента)

| Источник | Где живёт | Подход |
|---|---|---|
| **iMessage** | `~/Library/Messages/chat.db` на Mac | Локальный agent + sync |
| **Browser history** | SQLite в профиле браузера | Browser extension + sync |
| **Apple Notes** | Notes.app sqlite | Локальный exporter |
| **Local files** | Файловая система | File watcher + parse |

→ **Реализация**: separate "Vera Sync Agent" приложение на Mac (electron или Tauri).

### 15.4 Использование готовых платформ для D-категории

Вместо того чтобы писать 30 коннекторов с нуля — используем **n8n** для лёгкой 80%:

- **n8n** — open-source Zapier с 400+ интеграциями
- Запускаем отдельным контейнером
- Каждая интеграция → workflow «when [trigger] → POST to vera-gateway`
- Юзер настраивает через UI без кода

Для категории B (Telegram MTProto и т.п.) — пишем коннекторы сами потому что n8n их не покрывает.

### 15.5 Регистр источников в системе

```sql
CREATE TABLE sources (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,          -- "gmail_demoniwwwe", "telegram_userbot", "trello_personal"
    connector_type TEXT NOT NULL, -- "gmail", "telegram_mtproto", "trello", ...
    credentials JSONB,            -- зашифровано
    enabled BOOLEAN DEFAULT TRUE,
    last_fetched_at TIMESTAMP,
    last_event_id TEXT,           -- cursor для incremental sync
    config JSONB,                 -- per-source настройки (фильтры, теги, etc.)
    created_at TIMESTAMP DEFAULT NOW()
);
```

Дашборд показывает все источники, состояние, кнопки «backfill», «pause», «remove».

### 15.6 Полный список приоритезированных источников для MVP

**Phase 1 (MVP)** — то что точно надо Диме:
1. Gmail
2. Telegram (DM + groups через userbot)
3. Google Calendar
4. ChatGPT export (одноразовый импорт исторических диалогов)
5. Claude.ai export (то же)

**Phase 2** — добавляются после MVP:
6. Trello
7. Notion
8. Google Drive (docs + sheets)
9. WhatsApp (manual archive)
10. Instagram через ManyChat (когда подключен)

**Phase 3** — продвинутые:
11. Perplexity (скрейп)
12. LinkedIn archive
13. Slack (если есть рабочий workspace)
14. iMessage (через Vera Sync Agent)
15. Browser history (через extension)

**Никогда** (не оправдывает усилий):
- Личный Facebook (API закрыт)
- iMessage без Vera Sync Agent
- Сервисы где меньше 50 событий/мес

### 15.7 Дополнительный модуль: archive_importer

Поскольку cold archives — особый паттерн, выделяем модуль:

```
services/archive-importer/
├── Dockerfile
├── parsers/
│   ├── chatgpt.py          # JSON конверсейшнов ChatGPT
│   ├── claude.py           # Claude.ai export
│   ├── whatsapp.py         # WhatsApp text export
│   ├── linkedin.py         # LinkedIn data download
│   ├── twitter.py
│   └── facebook.py
├── normalizer.py           # raw → RawEvent
└── uploader.py             # POST в gateway
```

UI flow:
1. Дима заходит в дашборд → "Import Archive"
2. Выбирает тип источника (ChatGPT, WhatsApp, etc.)
3. Загружает ZIP
4. Видит preview: «найдено 4200 диалогов, 31k сообщений»
5. Подтверждает → запускается импорт + триаж

### 15.8 Estimate с источниками

Реалистично что Дима подключит для MVP:

| Источник | Events за год | Тип |
|---|---|---|
| Gmail | ~80k | live + backfill |
| Telegram (личные + 5 групп) | ~40k | live + backfill |
| Calendar | ~2k | live + backfill |
| ChatGPT export (вся история с янв 2023) | ~6k | one-time |
| Claude export | ~3k | one-time |
| **Итого** | **~131k** | |

С Phase 2 (Trello, Notion, Drive, IG, WhatsApp): **+50-80k**.

**Final estimate Vera 3.0 со всеми источниками за 1.5 года**: **~200k событий**.

Что подтверждает 14.2 — план в $80 / 1 неделя гибридного backfill валидный.

---

## 16. Обновлённый roadmap с учётом 14-15

### Phase 0 — Подготовка (1 неделя)
Без изменений.

### Phase 1 — Capture layer (3 недели) ⬆️ +1 неделя
- [ ] gateway + Connector Framework (стандартный интерфейс)
- [ ] ingestor-gmail (миграция + backfill mode)
- [ ] ingestor-telegram MTProto (миграция + backfill)
- [ ] ingestor-calendar
- [ ] **archive-importer** (новый модуль для cold archives)
- [ ] Postgres schema + migration со SQLite

### Phase 2 — Search layer (1 неделя)
Без изменений.

### Phase 3 — Triage layer + Backfill workflow (3 недели) ⬆️ +1 неделя
- [ ] brain-triage
- [ ] Token registry с paid/free + caps
- [ ] **Hatchet backfill workflow с дашбордом progress**
- [ ] **Bulk archive import (ChatGPT + Claude + WhatsApp)**
- [ ] Запуск первого реального backfill 1 года

### Phase 4 — Deep memory (3 недели)
Без изменений.

### Phase 5 — Personality + Behavior (3 недели)
Без изменений.

### Phase 6 — More sources (2 недели) NEW
- [ ] n8n setup для лёгких источников
- [ ] Connector: Trello
- [ ] Connector: Notion
- [ ] Connector: Google Drive
- [ ] Backfill этих

### Phase 7 — Cleanup (1 неделя)
Без изменений.

**Итого**: **~16 недель** (было 12).

---

## 17. Что добавлено в бюджете

### One-time backfill 1.5 года истории
- 200k событий, гибрид free+paid
- **~$80** одноразово

### Месячный operating с n8n
- Self-hosted n8n: 0 (на том же сервере)
- Если используем n8n.cloud: $20/мес (есть free tier 5k executions/мес)

### Vera Sync Agent для Mac (Phase 7+)
- Разработка: отдельный проект, ~2 недели
- Hosting: $0 (живёт на Mac у Димы)

---

---

## 18. Дашборд — детальная спецификация

### 18.1 Зачем дашборд (помимо Telegram)

Telegram — для **общения** с Верой. Дашборд — для **управления** ей:
- Видеть **что внутри** (что Вера знает, какие связи строит, какие паттерны нашла)
- **Настраивать** (источники, токены, активные темы, важные люди)
- **Контролировать** ($ траты, backfill прогресс, очереди задач)
- **Анализировать** (поведенческие паттерны, статистика по людям/проектам)
- **Доверять** (видно что и как Вера решила, можно поправить)

Без дашборда Вера = чёрный ящик. С дашбордом = прозрачная управляемая система.

### 18.2 Stack

**Выбор**: **FastAPI + HTMX + Tailwind**

| Почему не другое | |
|---|---|
| React/Next.js | избыточно для одного юзера, лишний стек, отдельная сборка |
| Streamlit | быстро строится но **дёшево выглядит**, плохо с custom UI |
| Reflex | Python-native React, но молодая экосистема, не везде стабильна |
| **HTMX + Tailwind** | минимум JS, server-side render, real-time через SSE, мобильно адаптивно, **тот же FastAPI что и API** |

**Почему хорошо для нас**: дашборд — это `services/dashboard/` контейнер, **рядом с gateway**, шарит auth и shared library. Один Python stack от backend до UI.

**Темы**: dark mode по умолчанию (как сейчас), light mode toggle.

### 18.3 Структура страниц

```
/dashboard
├── /                          # Главная — overview
├── /tokens                    # LLM ключи
├── /sources                   # Источники данных
├── /backfill                  # Загрузка истории
├── /costs                     # Финансы — траты + прогнозы
├── /events                    # Поиск/просмотр событий
├── /wiki                      # Что Вера знает
│   ├── /people                # Досье на людей
│   ├── /projects              # Активные проекты
│   ├── /dima                  # Модель Димы
│   └── /vera                  # Самомодель Веры
├── /patterns                  # Поведенческие паттерны
├── /settings                  # Настройки (активные темы, etc.)
├── /admin                     # Системные (логи, jobs, health)
└── /chat                      # Web-чат с Верой (альтернатива TG)
```

### 18.4 Главная (overview)

Карточки на одном экране, real-time через SSE:

```
┌────────────────────────────────────────────────────────────┐
│  Vera 3.0                                  • Online       │
├────────────────────────────────────────────────────────────┤
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐       │
│  │  События     │ │  В мозге     │ │  Источники   │       │
│  │  сегодня     │ │  всего       │ │  активных    │       │
│  │              │ │              │ │              │       │
│  │  247         │ │  182,431     │ │  6 / 8       │       │
│  │  ↑12% vs ср  │ │  +247 today  │ │  Gmail ✓ ... │       │
│  └──────────────┘ └──────────────┘ └──────────────┘       │
│                                                            │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐       │
│  │  $ сегодня   │ │  $ за месяц  │ │  Кулдауны    │       │
│  │              │ │              │ │              │       │
│  │  $0.42       │ │  $8.13       │ │  3 ключа     │       │
│  │  cap $5/день │ │  cap $50/мес │ │  возврат ~2м │       │
│  └──────────────┘ └──────────────┘ └──────────────┘       │
│                                                            │
│  Последние важные сигналы:                                 │
│  ┌────────────────────────────────────────────────────┐   │
│  │ 🔴 Срочно │ Письмо от Егорова — виза, ответ к 17:00│   │
│  │ 🟡 Важно  │ Аренда квартиры 2BR Семеньяк $800      │   │
│  │ 🟢 Инфо   │ ДР Анны через 11 дней — 12 ноября      │   │
│  └────────────────────────────────────────────────────┘   │
│                                                            │
│  Активные backfill / jobs:                                 │
│  ┌────────────────────────────────────────────────────┐   │
│  │ ▶ Backfill Gmail 2024  ████████░░░░ 67% ETA 4h     │   │
│  │ ✓ Nightly consolidation (завершено 04:12)          │   │
│  └────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────┘
```

### 18.5 Tokens (детальная страница)

Раскрытие текущего дашборда `/api/admin/tokens`. Уже частично есть.

**Что показывает**:
- Группировка по провайдеру
- Per-key статус: live/cooldown/dead, daily_used/daily_limit, daily_cost_used/cap
- **Tier badge**: 🟢 FREE / 🟡 TRIAL / 🔴 PAID
- Real-time мониторинг (SSE подписка на изменения)

**Что позволяет**:
- Добавить новый ключ (форма с auto-detect tier по provider)
- Изменить cap (daily/monthly cost limit)
- Включить/выключить ключ
- Удалить мёртвый ключ
- **Тест** ключа one-click — проверка что provider отвечает

**Расширенный вид** одного ключа (по клику):
```
┌─────────────────────────────────────────┐
│  gemini / Liza                          │
│  🟢 FREE · ✓ live                       │
│                                          │
│  Использование сегодня:                  │
│  ▰▰▰▰▱▱▱▱▱▱  167 / 1500 req            │
│  $0.012 (теория, free на самом деле)    │
│                                          │
│  Использование за месяц:                 │
│  График по дням ▁▃▅▇▆▄▃▅▇█▆▃           │
│                                          │
│  Капы: daily $0 / monthly $0            │
│  [✏ Edit caps]                           │
│                                          │
│  Health: 99.2% за неделю                │
│  Последние ошибки:                       │
│  • 2026-06-08 12:14 — 429 (cooldown)    │
│  • 2026-06-08 11:42 — 429               │
│                                          │
│  Capabilities: chat:fast, prefilter      │
│  [Test now] [Disable] [Delete]          │
└─────────────────────────────────────────┘
```

### 18.6 Sources (управление источниками)

Для каждого источника — карточка:

```
┌──────────────────────────────────────────────┐
│  📧 Gmail                                    │
│  demoniwwwe@gmail.com                        │
│  ✓ Active · last event 2 min ago             │
│                                              │
│  За месяц: 1,847 событий                     │
│  Последний backfill: 2025-01-01 → 2026-06-08│
│  Triage coverage: 97%                       │
│                                              │
│  [Pause] [Re-sync] [Disconnect]              │
│  [Backfill custom period →]                  │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│  📱 Telegram (userbot)                        │
│  +62 815 0000 0000                           │
│  ✓ Active · last event 8s ago                │
│                                              │
│  Диалогов: 47 личных + 12 групп              │
│  За месяц: 3,124 сообщений                   │
│  [Manage dialogs filter]                     │
│  [Pause] [Re-sync]                           │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│  + Add new source                            │
│  ┌────────────────────────────────────────┐  │
│  │  Gmail · Calendar · Drive · Trello     │  │
│  │  Notion · Slack · Outlook · ...        │  │
│  │  [Browse all 30+ connectors]           │  │
│  └────────────────────────────────────────┘  │
│  Or upload archive:                          │
│  [ChatGPT export] [Claude export]            │
│  [WhatsApp chat] [LinkedIn archive]          │
└──────────────────────────────────────────────┘
```

**Add new source flow**:
1. Выбрать тип
2. OAuth flow или ввести credentials
3. Auto-detect: можно ли real-time + есть ли historical
4. Опционально сразу запустить backfill

**Bulk archive flow** (ChatGPT export, WhatsApp, etc.):
1. Загрузить ZIP
2. Preview: «Найдено 4,200 диалогов, 31,000 сообщений за период 2023-01 → 2026-06»
3. Подтверждение → импорт в фоне через Hatchet

### 18.7 Backfill (страница управления загрузкой истории)

Самая важная страница для больших одноразовых операций.

```
┌──────────────────────────────────────────────────────────┐
│  Backfill истории                          [+ New job]   │
├──────────────────────────────────────────────────────────┤
│  Активные jobs:                                          │
│                                                          │
│  📋 Gmail backfill 2025-01 → 2026-06                     │
│  Started: 2026-06-09 14:23                               │
│  ▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱  46% (82k / 178k)                  │
│  ETA: ~21 hours · Rate: 4,500 events/hour               │
│  Budget: $42 / $100 cap   [Pause] [Cancel] [Increase]   │
│                                                          │
│  Sources progress:                                       │
│    Gmail        ████████████ 67k/67k  ✓ done            │
│    Telegram     █████░░░░░░  19k/42k  active            │
│    Calendar     ████████████ 2k/2k    ✓ done            │
│                                                          │
│  Phase progress:                                         │
│    ✓ Embeddings   178k/178k                              │
│    ▶ Triage      82k/178k    rate 4500/h                │
│    ⧗ Deep graph   waiting for triage                    │
│                                                          │
│  Real costs breakdown:                                   │
│    Voyage     $0    (free pool)                          │
│    Cerebras   $0    (free)                               │
│    Gemini     $38   (paid Flash)                         │
│    OpenAI     $0                                         │
│                                                          │
│  [View live event stream] [Download report]              │
├──────────────────────────────────────────────────────────┤
│  History:                                                │
│  ✓ Telegram backfill 2025-Q4   18k events  $14  done    │
│  ✓ ChatGPT archive import       3.4k       $0   done    │
│  ✗ Trello backfill              failed (auth expired)   │
└──────────────────────────────────────────────────────────┘
```

**+ New job** — мастер запуска:
1. **Какие источники?** (чек-боксы)
2. **Период**: from / to (по умолчанию: со старта Vera, +последние 30 дней auto-refresh)
3. **Tradeoff**: радио-кнопки
   - Free only (медленно, $0)
   - Hybrid (рекомендовано, ~$80, неделя)
   - Fast paid ($150, 2-3 дня)
4. **Budget cap** — slider до $200
5. **Importance threshold** — обрабатывать только important > N (для MVP можно `0`)
6. **Estimate** перед запуском:
   ```
   Estimated:
     Events: ~178,000
     Time: 5-7 days (hybrid mode)
     Cost: $72-95
   ```
7. **Confirm → Start**

### 18.8 Costs (финансовая страница)

```
┌──────────────────────────────────────────────────────────┐
│  Costs                                                   │
├──────────────────────────────────────────────────────────┤
│  Today:    $0.42                                         │
│  This week: $4.18                                        │
│  This month: $8.13                                       │
│  Cap month: $50  ▰▰▰░░░░░░░ 16%                        │
│                                                          │
│  Real billing (verified from providers):                 │
│    Voyage AI:    $0    (free tier in use)               │
│    Gemini:       $5.40 (Google AI Studio)               │
│    DeepSeek:     $0.20                                  │
│    Cerebras:     $0    (free)                            │
│    OpenAI:       $0    (no key)                          │
│    Anthropic:    $0    (trial)                          │
│                                                          │
│  Internal tracking vs real:                              │
│  Внутренний счётчик: $7.89                              │
│  Реальный по провайдерам: $8.13                         │
│  Расхождение: 3% ✓ OK                                   │
│                                                          │
│  By workflow:                                            │
│    Triage:          ███ $2.40 (30%)                     │
│    Deep extraction: █████ $4.10 (50%)                   │
│    Consolidation:   █ $0.90 (11%)                       │
│    Search:          █ $0.55 (7%)                        │
│    Reflection:      $0.18 (2%)                          │
│                                                          │
│  Cost over month chart:                                  │
│  $0.50 ┤                              ╭─                 │
│  $0.40 ┤              ╭────╮         │                  │
│  $0.30 ┤    ╭──╮     │    │  ╭──╮  │                  │
│  $0.20 ┤───╯  ╰─────╯    ╰──╯  ╰──╯                   │
│  $0.10 ┤                                                 │
│        └─────────────────────────────────────            │
│         1   5   10   15   20   25   30                  │
│                                                          │
│  [Set caps] [Export CSV] [Alert me if >$X/day]          │
└──────────────────────────────────────────────────────────┘
```

### 18.9 Wiki browser

Файлы из `/data/wiki/` или git-репо. Markdown rendering.

```
┌────────────────────────────────────────────────────────────┐
│  Wiki                                                       │
├──────────────────┬─────────────────────────────────────────┤
│  📂 People (47)  │  # Дмитрий Егоров                       │
│   • Дмитрий      │                                          │
│     Егоров   ●   │  Email: yegorov@itstep.org              │
│   • Маша     ●   │  Роль: руководитель IT STEP в Азии      │
│   • Лиза     ●   │                                          │
│   • Анна П.      │  ## История взаимодействий              │
│   • ...          │  - 47 писем за период янв 2025 — июнь   │
│                  │  - Средний lag ответа: 2.3 часа          │
│  📂 Projects (12)│  - Стиль общения: короткий по делу       │
│   • Переезд  ●   │                                          │
│   • Виза     ●   │  ## Последние темы                       │
│   • Veranda  ●   │  - Виза Джакарта (4 раза за неделю)     │
│   • IT STEP  ●   │  - KPI Лизы                              │
│                  │  - Покупка ноутбука                      │
│  📂 Dima         │                                          │
│   • dima.md      │  ## Паттерны                             │
│                  │  - Часто ставит еженедельные оценки 8/10│
│  📂 Vera         │  - Корректирует, но мягко               │
│   • self.md      │                                          │
│   • patterns.md  │  [Edit] [History] [Last updated 4h ago] │
└──────────────────┴─────────────────────────────────────────┘
```

**Edit**: вручную можно поправить если Вера что-то не так поняла. Git tracks все изменения.

**History**: видна эволюция документа — как Вера со временем переосмысливала.

### 18.10 Events (поиск + просмотр)

Та же страница что в `/api/events` сейчас, но с UI:

- **Поиск**: full-text + semantic + filter (source, period, person, importance)
- **List view**: компактный список с превью
- **Detail view**: полное содержимое + extracted metadata + связанные события
- **Reaction analysis**: «реакция Димы на это: ответил через 2 часа, drafted 3 раза»

### 18.11 Patterns (поведенческие паттерны)

```
┌────────────────────────────────────────────────────────────┐
│  Patterns of Dima                                          │
├────────────────────────────────────────────────────────────┤
│  📊 Decision patterns (32 detected)                        │
│                                                            │
│  💬 С Егоровым: короткий стиль, lag <2ч                   │
│    Confidence: 95%  |  Based on 47 reactions               │
│    [View examples]                                         │
│                                                            │
│  📅 Финансовые письма: откладывает на пятницу             │
│    Confidence: 78%  |  Based on 23 reactions               │
│    Last updated: 2026-06-08                                │
│    [View examples]                                         │
│                                                            │
│  ⚠ Anomaly: 4 раза за неделю отложил ответ Маше          │
│    Не похоже на твой обычный паттерн.                     │
│    [Investigate] [Dismiss]                                │
│                                                            │
│  ⏰ Timing patterns:                                       │
│  - Маме всегда вечером, 19-21                             │
│  - Босс: утром следующего дня если after-hours             │
│                                                            │
│  📈 Style patterns:                                        │
│  - Длина сообщений vs получатель: график                  │
│  - Эмоциональный тон vs день недели: график              │
└────────────────────────────────────────────────────────────┘
```

### 18.12 Settings

Управление **активным контекстом** Веры:

```
┌────────────────────────────────────────────────────────────┐
│  Active context                                            │
├────────────────────────────────────────────────────────────┤
│  🎯 Активные темы (Вера использует для триажа)            │
│  ┌──────────────────────────────────────┐                 │
│  │ Переезд в Индонезию      [×]         │                 │
│  │ Виза Джакарта             [×]         │                 │
│  │ KPI команды IT STEP       [×]         │                 │
│  │ Veranda бар               [×]         │                 │
│  │ + Add topic                            │                 │
│  └──────────────────────────────────────┘                 │
│                                                            │
│  👤 Важные люди (auto-detected, can edit)                  │
│  ┌──────────────────────────────────────┐                 │
│  │ Дмитрий Егоров           босс  [⚙]   │                 │
│  │ Маша                     жена  [⚙]   │                 │
│  │ Лиза                     дочь  [⚙]   │                 │
│  │ Дарья Степаненко         коллега[⚙]  │                 │
│  │ + Add person                          │                 │
│  └──────────────────────────────────────┘                 │
│                                                            │
│  🔔 Уведомления:                                          │
│  [✓] Срочные сигналы (importance > 90)                    │
│  [✓] Активные темы (любой match)                          │
│  [✓] Аномалии в паттернах                                 │
│  [ ] Daily digest в 8:00                                  │
│                                                            │
│  Channel: [Telegram ▼] [Email] [Web push]                │
└────────────────────────────────────────────────────────────┘
```

### 18.13 Admin (системные)

Для отладки. Не показывается обычно.

- Health всех контейнеров
- Hatchet jobs queue
- Postgres slow queries
- Logs viewer
- Manual trigger jobs (run reflection now, rebuild a wiki, etc.)
- DB dump button
- Restart service button

### 18.14 Chat (web версия)

Альтернатива Telegram-боту. Полезно если:
- Хочешь увидеть Вере фото с компьютера (drag-drop)
- Нужны длинные ответы которые в TG неудобно читать
- Работаешь с дашбордом и одновременно спрашиваешь

```
┌────────────────────────────────────────────────────────────┐
│  Чат с Верой                                               │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  Дима 14:23                                                │
│  Что Егоров писал на этой неделе?                          │
│                                                            │
│  Вера 14:23                                                │
│  За эту неделю 4 письма от Егорова:                       │
│  • Понедельник: согласование KPI Лизы — одобрено          │
│  • Среда: запрос отчёта по конверсии воронки               │
│  • Четверг: «ок» по аренде ноутбука                       │
│  • Сегодня утром: виза, срок до 17:00                     │
│                                                            │
│  Хочешь развёрнуто по любому из них? [Виза] [KPI] [Все]   │
│                                                            │
│  ──────────────────────────────                            │
│  [📎] [🎤] Спросить...                          [Send]    │
└────────────────────────────────────────────────────────────┘
```

### 18.15 Auth

**Текущий подход в Vera 2.0**: HMAC-signed cookies + owner Telegram ID.

**Для 3.0** — то же, плюс:
- **Optional**: shared view для жены (по приглашению, ограниченные права)
- **Read-only mode** для просмотра без возможности менять настройки
- **API token** для бота (если хочется других интеграций)

### 18.16 Mobile responsiveness

Tailwind responsive по умолчанию. Все страницы должны быть **читаемы и работаемы с iPhone**:
- Карточки в один столбец
- Графики горизонтально-скроллабельные
- Меню в hamburger
- Touch-friendly кнопки

### 18.17 Real-time updates

**Server-Sent Events (SSE)** для:
- Live counters (события сегодня, $ сейчас)
- Backfill прогресс
- Новые сигналы в overview
- Job статусы в admin

Это лучше WebSocket'ов для односторонних обновлений: проще, не теряется при reconnect, работает через CORS легко.

### 18.18 Тех. реализация и timeline

Phase 2 → MVP версия дашборда (overview + tokens + sources + events). 1 неделя.

Phase 4-5 → расширенная (wiki browser + patterns + settings + costs). 1 неделя.

Phase 6 → polish и chat web версия. 0.5 недели.

**Итого на дашборд**: ~2.5 недели разработки.

---

---

## 19. Тестирование — всё должно быть покрыто

### 19.1 Принципы

1. **Каждый модуль покрыт тестами перед merge в master** — CI блокирует PR без тестов на новый код.
2. **Минимальное покрытие** 80% на каждый сервис, **критичные пути** (auth, cost guard, payment caps) — 100%.
3. **AI-вызовы НЕ дёргают реальные провайдеры в тестах** — всё мокается. Реальные вызовы — только в отдельной evaluation suite.
4. **Стоимость регрессии — тоже тестируется** — есть тесты что «триаж не должен стоить больше $0.005 на событие».
5. **Поведенческие свойства Веры — оцениваются метриками** не bool'ом — точность, полнота, latency.

### 19.2 Test Pyramid

```
                          ┌─────────────┐
                          │   E2E (5%)  │  Playwright, real-stack
                          └─────────────┘
                      ┌─────────────────────┐
                      │  Integration (20%)  │  docker-compose + Postgres
                      └─────────────────────┘
                  ┌─────────────────────────────┐
                  │  Service tests (25%)        │  per-service, mocked DB+LLM
                  └─────────────────────────────┘
              ┌──────────────────────────────────────┐
              │       Unit tests (50%)               │  pure functions, fast
              └──────────────────────────────────────┘

         ┌──────────────────────────────────────────────────┐
         │  LLM Evaluation (отдельный pipeline, не CI)      │
         │  Real API, реальные данные, метрики качества      │
         └──────────────────────────────────────────────────┘
```

### 19.3 Технологический stack

| Тип | Инструмент | Зачем |
|---|---|---|
| Unit / Service | **pytest + pytest-asyncio** | стандарт Python |
| Coverage | **pytest-cov + coverage.py** | измерение coverage |
| Mocking | **pytest-mock + respx** (HTTP) | моки внешних API |
| HTTP recording | **VCR.py** | запись ответов реальных API для replay в CI |
| DB testing | **pytest-postgresql** или **testcontainers** | реальный Postgres в Docker для integration |
| Fixtures / factories | **factory-boy** + **mimesis** | генерация тестовых данных |
| Property-based | **hypothesis** | проверка инвариантов |
| LLM evaluation | **promptfoo** + **DeepEval** + **RAGAS** | оценка качества AI |
| Load testing | **Locust** | стресс gateway |
| E2E dashboard | **Playwright** (Python) | проверка UI |
| Mutation testing | **mutmut** (опционально) | проверка качества тестов |
| Snapshot testing | **syrupy** | для сложных Pydantic-structures |
| Type checking | **mypy + pyright** | static analysis |
| Linting | **ruff + black** | стиль и quality |

### 19.4 Уровни тестов с примерами

#### 19.4.1 Unit tests (50% от всех)

**Что**: чистые функции без I/O. Быстрые (<10ms). Без сети, без БД, без LLM.

**Примеры**:

```python
# tests/unit/test_registry.py
def test_cost_calculation_known_model():
    cost = cost_usd("gemini-2.5-flash", tokens_in=1000, tokens_out=200)
    assert cost == pytest.approx(0.075 / 1000 * 1 + 0.30 / 1000 * 0.2)

def test_routing_policy_free_first():
    policy = RoutingPolicy.chat_fast
    free_providers = [p for p, tier in policy if tier == "free"]
    paid_providers = [p for p, tier in policy if tier == "paid"]
    # all free должны быть раньше любого paid
    assert policy.index(("cerebras", "free")) < policy.index(("gemini", "paid"))

def test_token_is_available_cooldown():
    token = TokenRecord(cooldown_until=datetime.utcnow() + timedelta(seconds=60))
    assert not token.is_available()

def test_token_cost_cap_blocks():
    token = TokenRecord(
        tier="paid",
        daily_cost_cap_usd=5.0,
        daily_cost_used_usd=4.99,
    )
    assert can_call_paid(token, estimated_cost=0.05) is False  # 5.04 > 5.0

# Property-based
@given(text=text(min_size=10, max_size=10000))
def test_embed_input_normalization_idempotent(text):
    assert normalize_for_embed(normalize_for_embed(text)) == normalize_for_embed(text)
```

**Coverage target**: 90%+.

#### 19.4.2 Service tests (25%)

**Что**: тест одного сервиса с моками внешних зависимостей. Реальный код самого сервиса.

**Примеры**:

```python
# tests/service/test_brain_triage.py
@pytest.mark.asyncio
async def test_triage_extracts_signals(respx_mock, postgres_test):
    # Мокаем LLM
    respx_mock.post("https://api.cerebras.ai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "importance": 85,
                "topics": ["виза"],
                "people_mentioned": ["Дмитрий Егоров"],
                "signals": [{"type": "task", "summary": "Срок завтра"}]
            })}}]
        })
    )

    event = await create_test_event(
        source="gmail",
        content="От Егорова: виза готова, забери завтра до 12:00",
    )

    result = await triage_event(event.id)

    assert result.importance == 85
    assert "Егоров" in result.people_mentioned[0]
    assert len(result.signals) == 1

@pytest.mark.asyncio
async def test_triage_falls_through_to_next_provider(respx_mock, postgres_test):
    # Cerebras падает с 429
    respx_mock.post("https://api.cerebras.ai/v1/chat/completions").mock(
        return_value=httpx.Response(429, json={"error": "rate_limit"})
    )
    # Groq отвечает успешно
    respx_mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={...})
    )

    result = await triage_event(...)

    assert result.provider_used == "groq"

@pytest.mark.asyncio
async def test_triage_respects_paid_cap(respx_mock, postgres_test):
    # Установить $5 cap на платный Gemini
    await set_daily_cap(token_id=18, cap_usd=5.0, used_usd=4.99)

    # Free провайдеры заблокированы (cooldown)
    await mark_all_free_in_cooldown()

    # Дорогостоящая попытка должна заблокироваться
    with pytest.raises(DailyBudgetExceeded):
        await triage_event(event_id_that_would_cost_0_05)
```

#### 19.4.3 Integration tests (20%)

**Что**: несколько сервисов вместе. Реальный Postgres, реальный Hatchet, моки только для внешних API.

**Примеры**:

```python
# tests/integration/test_event_flow.py
@pytest.mark.asyncio
async def test_event_arrives_gets_triaged_then_stored(integration_stack):
    """От webhook до записи в БД с триаж-метаданными."""
    stack = integration_stack  # запускает gateway + triage worker + postgres + hatchet

    # POST через gateway
    response = await stack.gateway_client.post("/event/gmail", json={
        "source_event_id": "msg_123",
        "content_text": "Письмо от Егорова про визу",
        "occurred_at": "2026-06-08T10:00:00Z",
    })
    assert response.status_code == 200

    # Ждём пока Hatchet обработает (timeout 30s)
    event = await wait_for_triage(stack.db, "msg_123", timeout=30)

    assert event.triage_metadata is not None
    assert event.triage_metadata["importance"] is not None
    assert event.embedding is not None  # эмбеддинг тоже создан

@pytest.mark.asyncio
async def test_important_event_triggers_deep_extraction(integration_stack):
    # Запускаем событие с importance=85 → должно дойти до brain-graph
    ...
```

#### 19.4.4 E2E tests (5%)

**Что**: полный путь через UI или Telegram-бота. Самые медленные, самые хрупкие, но самые ценные.

**Примеры**:

```python
# tests/e2e/test_dashboard.py (Playwright)
async def test_dashboard_shows_real_token_status(page, full_stack):
    await page.goto("https://localhost/dashboard")
    await page.fill("input[name=password]", "test_password")
    await page.click("button[type=submit]")

    # Должны увидеть карточки с количеством ключей
    cerebras_card = page.locator("[data-provider=cerebras]")
    await expect(cerebras_card).to_be_visible()
    await expect(cerebras_card.locator(".count")).to_contain_text("5")

async def test_backfill_wizard_estimates_cost(page, full_stack):
    await page.goto("/dashboard/backfill")
    await page.click("text=+ New job")
    await page.check("input[name=source][value=gmail]")
    await page.fill("input[name=period_from]", "2025-01-01")
    await page.click("input[value=hybrid]")

    # Estimate должен появиться
    estimate = page.locator(".estimate-cost")
    await expect(estimate).to_contain_text("$")
    cost_value = await estimate.inner_text()
    assert "$" in cost_value
```

```python
# tests/e2e/test_bot.py
async def test_dima_asks_about_person_gets_answer(telegram_test_client):
    """Эмуляция: Дима пишет боту, получает ответ."""
    await seed_test_events([
        {"source": "gmail", "from": "yegorov@itstep.org", "content": "..."},
        # ещё 10 событий
    ])

    response = await telegram_test_client.send_message("кто такой Егоров?")

    assert "Егоров" in response.text
    assert any(word in response.text.lower() for word in ["работает", "руководит", "босс"])
```

### 19.5 LLM Evaluation — отдельный pipeline

**Не часть CI**, потому что:
- Дорого (реальные LLM-вызовы)
- Медленно (минуты)
- Недетерминированно (нужна устойчивость к вариации)

**Запускается**: вручную или раз в неделю в отдельном GitHub Actions workflow.

**Инструменты**: **promptfoo** для prompt-level evaluation, **DeepEval** для семантической оценки, **RAGAS** для качества retrieval.

#### 19.5.1 Триаж evaluation

**Dataset**: 100 реальных событий с ручной разметкой (importance + topics + people).

```yaml
# evals/triage.yaml
description: Triage accuracy
prompts:
  - file://prompts/triage_v3.txt
providers:
  - cerebras/gpt-oss-120b
  - groq/openai/gpt-oss-120b
  - gemini/2.5-flash
tests:
  - vars:
      content: "От Егорова: виза готова..."
      expected_importance: 85
      expected_topics: ["виза", "сроки"]
      expected_people: ["Дмитрий Егоров"]
    assert:
      - type: javascript
        value: output.importance >= 75 && output.importance <= 95
      - type: contains-any
        value: ["виза", "срок"]
        target: output.topics
      - type: semantic-similarity
        threshold: 0.8
        value: expected_people
```

Запускаем `promptfoo eval` — получаем матрицу: provider × test case.

#### 19.5.2 Search relevance (RAG quality)

**RAGAS metrics**:
- **Faithfulness**: ответ Веры основан на найденных событиях (не галлюцинация)
- **Answer relevance**: ответ соответствует вопросу
- **Context precision**: найденные события релевантны
- **Context recall**: все релевантные события найдены

```python
# evals/test_search_quality.py
@pytest.mark.evaluation
async def test_search_finds_egorov_in_history():
    # Подготовленные данные: 500 событий с разметкой
    query = "кто такой Дмитрий Егоров?"
    expected_event_ids = [12, 45, 89, ...]  # ручная разметка релевантных

    result = await search_engine.search(query)

    # Recall: сколько релевантных найдено
    found = set(result.event_ids) & set(expected_event_ids)
    recall = len(found) / len(expected_event_ids)
    assert recall > 0.85

    # Precision
    precision = len(found) / len(result.event_ids)
    assert precision > 0.7

    # Faithfulness через DeepEval
    from deepeval.metrics import FaithfulnessMetric
    metric = FaithfulnessMetric()
    score = metric.measure(
        actual_output=result.answer,
        context=[e.content for e in result.events]
    )
    assert score > 0.85
```

#### 19.5.3 Behavioral pattern accuracy

**Test set**: 20 пар (контекст → реальная реакция Димы). Проверяем что Вера предсказывает похожий ответ.

```python
@pytest.mark.evaluation
async def test_drafted_reply_matches_dima_style():
    incoming_email = load_test_event("egorov_visa_request")
    actual_dima_reply = load_dima_reply_for(incoming_email.id)

    drafted = await vera.draft_reply(incoming_email.id)

    # Semantic similarity
    similarity = cosine_similarity(
        embed(drafted),
        embed(actual_dima_reply)
    )
    assert similarity > 0.75

    # Style metrics
    assert len(drafted) <= len(actual_dima_reply) * 1.5  # не сильно длиннее
    assert language_detect(drafted) == language_detect(actual_dima_reply)
```

#### 19.5.4 Cost regression tests

**Бюджет per workflow** — должен оставаться в рамках.

```python
@pytest.mark.cost
async def test_triage_cost_per_event_below_threshold():
    """Триаж одного события не должен стоить больше $0.005."""
    with cost_tracker() as costs:
        for i in range(10):
            event = create_realistic_event()
            await triage_event(event.id)

    avg_cost = costs.total_usd / 10
    assert avg_cost < 0.005, f"Got ${avg_cost:.5f}, exceeded budget"

@pytest.mark.cost
async def test_deep_extraction_cost_below_threshold():
    """Deep extract одного важного события: < $0.10."""
    with cost_tracker() as costs:
        await deep_extract(important_event_id)
    assert costs.total_usd < 0.10
```

### 19.6 Test data

**Источники тестовых данных**:

1. **Faker / mimesis**: синтетические события для unit/service тестов
2. **Реальные anonymized данные**: ~500 событий, ID и имена обфусцированы — для integration и evaluation
3. **Hand-crafted edge cases**: пустые тексты, длинные тексты (>100k токенов), не-UTF8, мульти-язык, тексты с инжекциями

**Хранение**: `tests/data/` в репо, для больших — Git LFS.

### 19.7 Тестирование cost guard / paid token gating

Критично важно — здесь были burn'ы в Vera 2.0.

```python
@pytest.mark.asyncio
class TestCostGuard:
    """100% coverage обязательно."""

    async def test_paid_blocked_when_cap_reached(self):
        ...
    async def test_global_daily_cap_blocks_all_paid(self):
        ...
    async def test_internal_counter_vs_real_billing_alert(self):
        # Если расхождение > 20% — alert
        ...
    async def test_paid_disabled_token_never_used(self):
        ...
    async def test_paid_only_after_all_free_exhausted(self):
        ...
    async def test_zero_cap_makes_token_unusable(self):
        ...
```

### 19.8 Тестирование Connector Framework

Каждый коннектор должен пройти **стандартный тест-сюит**:

```python
# tests/connectors/base.py
class ConnectorTestSuite:
    """Каждый коннектор наследуется и реализует fixtures."""

    @abstractmethod
    def get_connector(self) -> SourceConnector: ...

    async def test_authenticate_with_valid_credentials(self, valid_creds):
        connector = self.get_connector()
        await connector.authenticate(valid_creds)
        assert connector.is_authenticated()

    async def test_fetch_history_yields_normalized_events(self, mocked_api):
        connector = self.get_connector()
        events = [e async for e in connector.fetch_history(start, end)]
        assert all(isinstance(e, RawEvent) for e in events)
        assert all(e.source == connector.name for e in events)

    async def test_fetch_history_handles_pagination(self, mocked_paginated_api):
        ...

    async def test_realtime_subscription_callbacks(self, mock_push):
        ...

    async def test_idempotent_dedup_on_repeated_fetch(self, mocked_api):
        """Повторный fetch не создаёт дубликатов."""
        ...

# tests/connectors/test_gmail.py
class TestGmailConnector(ConnectorTestSuite):
    def get_connector(self):
        return GmailConnector()
    # ... custom Gmail-specific tests
```

### 19.9 Тесты на безопасность

- Auth: все admin endpoints должны require auth
- SQL injection: pytest fixtures, входы fuzz'ятся
- API rate limiting на public endpoints
- Secrets в логах: не должны попадать (тесты с regex'ами)
- Encryption at rest для токенов

```python
def test_token_never_appears_in_logs(caplog):
    with caplog.at_level("DEBUG"):
        await save_token("test", "key-secret-12345")
    for record in caplog.records:
        assert "key-secret-12345" not in record.message
```

### 19.10 Тестирование UI (Playwright)

E2E на каждый ключевой user flow:

| Flow | Тест |
|---|---|
| Add token | Login → /tokens → + Add → fill form → see in list |
| Start backfill | Login → /backfill → Wizard → estimate → confirm → see progress |
| Search wiki | Login → /wiki → search «Егоров» → click → see profile |
| View costs | Login → /costs → see breakdown |
| Configure source | Login → /sources → + Add Gmail → OAuth flow (mocked) |

### 19.11 Load testing (Locust)

Цели:
- Gateway должен выдерживать 1000 req/min от ingestor'ов
- Search должен отвечать < 3s даже под нагрузкой
- DB не должна стать bottleneck'ом

```python
# tests/load/test_gateway_load.py
from locust import HttpUser, task, between

class GatewayLoadUser(HttpUser):
    wait_time = between(0.1, 1)

    @task(10)
    def post_event(self):
        self.client.post("/event/gmail", json={...})

    @task(1)
    def search_query(self):
        self.client.post("/search", json={"q": "test"})
```

Запускается раз в спринт, не в каждом PR.

### 19.12 CI/CD интеграция

```yaml
# .github/workflows/tests.yml
name: Tests

on: [push, pull_request]

jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install -e shared/[dev]
      - run: pytest tests/unit/ --cov=vera_shared --cov-fail-under=90

  service:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env: { POSTGRES_PASSWORD: test }
    steps:
      - uses: actions/checkout@v4
      - run: pytest tests/service/ --cov-fail-under=80

  integration:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker compose -f docker-compose.test.yml up -d
      - run: pytest tests/integration/
      - run: docker compose down

  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker compose up -d
      - run: playwright install
      - run: pytest tests/e2e/

  type-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: mypy services/ shared/

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ruff check .
      - run: black --check .

# .github/workflows/evaluation.yml — отдельный, ручной
name: LLM Evaluation
on: workflow_dispatch
jobs:
  triage-eval:
    steps:
      - run: promptfoo eval -c evals/triage.yaml
      - run: pytest tests/evaluation/ -m evaluation

  cost-regression:
    steps:
      - run: pytest tests/cost/ -m cost
```

### 19.13 Coverage targets per service

| Сервис | Coverage min | Что обязательно 100% |
|---|---|---|
| shared/llm/ | 90% | cost_guard, routing_policy |
| shared/tokens/ | 95% | pool.get, model.is_available |
| shared/events/ | 85% | schema validation |
| gateway/ | 85% | auth, dedup |
| ingestor-* (each) | 80% | normalization to RawEvent |
| brain-triage/ | 85% | prompt building, response parsing |
| brain-graph/ | 80% | retry logic, fallback chain |
| brain-jobs/ | 80% | scheduling, idempotency |
| brain-search/ | 80% | hybrid search ranking |
| bot-telegram/ | 75% | message handling |
| dashboard/ | 70% | сложно поднять выше с UI |
| archive-importer/ | 90% | парсеры — критично |

Глобально: **80%+** обязательно для merge в master.

### 19.14 Mutation testing (опционально)

`mutmut` мутирует код (заменяет `+` на `-`, `>` на `<`, etc.) и смотрит, ловят ли тесты эти изменения. Если нет — тесты слабые.

Запускается раз в спринт по `shared/`:
```bash
mutmut run --paths-to-mutate shared/llm/
mutmut results
```

### 19.15 Тестирование Vera 3.0 при миграции

Особый случай: при первом импорте данных из Vera 2.0 нужно проверить что **ничего не потерялось**.

```python
@pytest.mark.migration
def test_all_v2_events_present_in_v3():
    v2_count = sqlite_conn("vera2.db").execute("SELECT COUNT(*) FROM events").fetchone()[0]
    v3_count = postgres_conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert v3_count >= v2_count, f"Lost events: {v2_count - v3_count}"

@pytest.mark.migration
def test_v2_event_data_preserved():
    sample_v2 = random.sample(v2_event_ids, 100)
    for v2_id in sample_v2:
        v2_event = get_v2_event(v2_id)
        v3_event = get_v3_event(v2_event.source, v2_event.source_event_id)
        assert v3_event.content_text == v2_event.content_text
        assert v3_event.occurred_at == v2_event.occurred_at
```

### 19.16 Production smoke tests

После каждого деплоя автоматически дёргаются smoke тесты прямо на прод:

```python
# tests/smoke/test_production.py
async def test_gateway_health():
    r = await httpx.get("https://vera.dima.example/healthz")
    assert r.status_code == 200

async def test_telegram_bot_responds():
    # Через test bot account
    response = await test_bot.send_message("ping")
    assert "pong" in response.text.lower()

async def test_token_pool_has_capacity():
    r = await admin_client.get("/api/admin/tokens")
    free_active = sum(1 for t in r.json() if t["tier"] == "free" and t["state"] == "live")
    assert free_active >= 3, "Less than 3 free providers available"
```

Запускаются после `vera-deploy` через GitHub Actions. Fail = автоматический rollback.

### 19.17 Documentation tests

Все примеры кода в README / docs автоматически тестируются:

```python
# pytest-doctest
def example_register_token():
    """
    >>> await register_token("groq", "test_label", "test_key_value")
    Token(id=..., provider='groq', label='test_label', is_active=True)
    """
```

### 19.18 Test data privacy

Анонимизированные реальные данные для test fixtures:
- Имена → fake names (mimesis)
- Email'ы → fake.com domain
- Даты сохраняются (для temporal тестов)
- Контент через GPT-обфускацию (сохраняем смысл, меняем детали)

**Никогда** в репо: реальные emails Димы, реальные TG сообщения, реальные токены провайдеров.

### 19.19 Test execution time targets

| Suite | Max время |
|---|---|
| Unit | < 30s |
| Service | < 2min |
| Integration | < 5min |
| E2E | < 10min |
| Evaluation | < 30min (отдельный workflow) |
| Load | < 1h (раз в спринт) |

Если unit ≥ 30s — рефакторим, выносим slow тесты в service-level.

### 19.20 Quality gates

PR не мержится если:
- ❌ Любой unit/service/integration падает
- ❌ Coverage упал ниже threshold
- ❌ mypy / ruff / black жалуются
- ❌ Production smoke test упал на staging
- ⚠️ Evaluation метрики ухудшились более чем на 5% (warning, не block)
- ⚠️ Cost regression > 10% (warning)

---

**Подпись разработчиков**: пусто. **Подпись заказчика**: пусто.
**Статус**: DRAFT v1.3, требует ревью Димы.
Изменения v1.2 → v1.3: добавлена секция 19 (тестирование).
