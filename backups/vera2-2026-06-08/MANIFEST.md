# Vera 2.0 — Full Backup

**Date**: 2026-06-08 14:11 UTC
**Source**: Hetzner CX22 (alias `hetzner-root`, port 9617)
**Reason**: подготовка к Vera 3.0 миграции
**Owner**: Дима

## Что внутри `vera2-full-backup-FINAL.tar.gz` (25 MB)

| Файл | Размер | Что |
|---|---|---|
| vera2_backup.db | 17 MB | SQLite — 4552 события + 23 токена + sources + jobs |
| neo4j_dump.json | 55 MB | Полный Neo4j Aura — 2764 узла + 7926 рёбер (Graphiti) |
| tokens_decrypted.csv | 3.6 KB | Все 23 токена в plaintext (для миграции в Vera 3.0) |
| env_server.txt | 1017 B | .env сервера: Telegram, Gmail OAuth, SESSION_SECRET, и т.д. |
| docker-compose.yml | 1.4 KB | Текущая конфигурация контейнеров |
| vera-backfill.service | 421 B | systemd unit для backfill loop |
| userbot.session | 1.2 MB | MTProto session Telegram userbot **(критично!)** |
| sessions/ | — | Запасные TG сессии |
| backfill_progress.json | 301 B | Состояние backfill |

Также — отдельно ранний backup без Neo4j: `vera2-full-backup-20260608-1409.tar.gz` (3.1 MB).

## Восстановление

### Полное окружение
```bash
mkdir vera2-restore && cd vera2-restore
tar xzf ../vera2-full-backup-FINAL.tar.gz
ls -lh
```

### Импорт токенов в Vera 3.0
```python
import csv
with open('tokens_decrypted.csv') as f:
    for row in csv.DictReader(f):
        await register_token(
            provider=row['provider'],
            label=row['label'],
            token=row['token'],
            tier='paid' if row['provider'] in ['anthropic', 'deepseek'] else 'free',
        )
```

### Импорт Neo4j (если нужен граф в Vera 3.0)
```python
import json
data = json.load(open('neo4j_dump.json'))
# data['nodes'], data['edges'] → cypher CREATE statements
```

## Что в БД на момент бэкапа

### События (events table)
- Всего: **4552**
- Период: ~30 дней истории
- Source breakdown: gmail (большинство), telegram, instagram (manychat smoke test)
- В Graphiti графе: 943 эпизода обработано (~23%)
- Остальное: только в SQLite сырое (некоторые с embeddings)

### Токены (tokens table) — 23 шт.

| Provider | Кол-во | Заметка |
|---|---|---|
| cerebras | 5 | все free (demoniwwwe, zapleosoft, veranda, levaromat, itstep) |
| groq | 1 | demoniwwwe (free, остальные 4 забанили) |
| gemini | 4 | 3 free (Liza, Billaa, Oleg) + 1 paid disabled (demoniwwwe id=18) |
| deepseek | 5 | demoniwwwe оригинал + 4 пополнённых |
| voyage | 5 | все с привязанными картами |
| anthropic | 1 | trial |
| openrouter | 1 | gemma4 free |
| manychat | 1 | webhook secret only |

### Граф Neo4j (Aura free tier)
- Узлов: **2764** (Entity, Person, Project, Organization)
- Рёбер: **7926** (RELATES_TO, MENTIONS, etc.)
- Окно: ~30 дней

## Критичные доступы (из env_server.txt)

| Что | Где найти в env |
|---|---|
| Telegram Bot Token | TELEGRAM_BOT_TOKEN_VERA |
| Telegram API ID/Hash | TELEGRAM_API_ID, TELEGRAM_API_HASH |
| Telegram phone (userbot) | TELEGRAM_PHONE |
| Owner ID (для auth) | OWNER_TELEGRAM_ID |
| Session secret (HMAC cookies) | SESSION_SECRET |
| Deploy secret | DEPLOY_SECRET |
| Webhook base URL | WEBHOOK_BASE_URL (dima.veranda.my) |
| Gmail OAuth client | GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET |
| Manychat webhook secret | MANYCHAT_WEBHOOK_SECRET |
| Internal service-to-service auth | INTERNAL_SECRET |
| Neo4j Aura | NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE |

## Сервисы и доступы

| Что | Куда |
|---|---|
| Hetzner SSH | `ssh hetzner-root` (alias из ~/.ssh/config) |
| Neo4j Aura | dashboard через web, free tier до 200k узлов |
| Domain | dima.veranda.my, через Cloudflare |
| GitHub repo | https://github.com/zapleoceo/muai |
| Telegram bot | смотри TELEGRAM_BOT_TOKEN_VERA в env |
| Manychat | https://app.manychat.com/fb5043879/ |

## КРИТИЧНО

⚠️ **НЕ удалять до полной верификации миграции в Vera 3.0**
⚠️ Этот бэкап — единственная страховка от потери 17 месяцев данных + всех OAuth сессий
⚠️ `userbot.session` восстановить НЕЛЬЗЯ кроме как ре-авторизацией по SMS (риск с Telegram limits)

Дополнительная защита: GitHub repo `zapleoceo/muai` хранит весь код Vera 2.0.

## Что НЕ в этом бэкапе (нужно сделать руками если понадобится)

- Логи контейнеров (только последние 24h в docker logs) — не критично
- ManyChat настроенный External Request — нужно пересоздать руками в их UI
- DNS / Cloudflare config — не менялся, остаётся как есть
- GitHub Actions secrets — есть в репо, но secret values не экспортируются
- Logs of cost burn analysis ($12 burn) — в логах
