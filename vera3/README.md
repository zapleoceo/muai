# Vera 3.0

**Цифровая копия памяти и понимания Димы.**

Модульная архитектура, free-first приоритеты токенов, полное покрытие тестами.
Спецификация: [`../docs/vera3-tz.md`](../docs/vera3-tz.md)

## Структура

```
vera3/
├── shared/           # vera_shared — общая библиотека (registry, models, etc.)
├── services/         # независимые микросервисы
│   ├── gateway/      # FastAPI front для webhook'ов
│   ├── ingestor-*/   # коннекторы источников (gmail, telegram, ...)
│   ├── brain-*/      # обработчики (triage, jobs, search)
│   ├── bot-telegram/ # Telegram-бот
│   ├── dashboard/    # Web UI
│   └── archive-importer/
├── infra/            # docker-compose, конфиги
├── tests/            # unit / service / integration / e2e / evaluation
└── scripts/          # утилиты миграции, ops
```

## Быстрый старт (локально)

```bash
cd infra
cp .env.example .env  # заполнить
docker compose up -d  # postgres, hatchet, langfuse + сервисы
```

## Прогресс по фазам (из ТЗ)

| Phase | Что | Статус |
|---|---|---|
| 0 | Подготовка + backup Vera 2.0 | ✅ Done |
| 1 | Capture layer (gateway + ingestors) | 🔨 In progress |
| 2 | Search layer | ⏸ |
| 3 | Triage layer + Backfill workflow | ⏸ |
| 4 | Deep memory (graph + consolidation) | ⏸ |
| 5 | Personality + Behavior modeling | ⏸ |
| 6 | More sources (n8n + custom) | ⏸ |
| 7 | Cleanup + миграция | ⏸ |

## Принципы

1. **Каждый сервис — отдельный Docker-контейнер**, обновляется независимо.
2. **Каждый токен помечен** `tier: free|paid|trial`, free — приоритет.
3. **Hard cost caps** на каждый paid токен + глобально.
4. **Все вызовы LLM трейсятся** в Langfuse.
5. **80%+ coverage** обязательно для merge, критичные пути 100%.
6. **Durable jobs** через Hatchet — рестарт не теряет работу.
