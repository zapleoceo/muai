# Инвентаризация MCP-соединений — 27.11.2026

Собрал координатор (claude_demoniwwwe_zasus) по конфигам ноутбука Димы и пробам эндпоинтов без авторизации. Секреты сюда не выписаны. Репозиторий публичный: локальные пути ноутбука и адреса IT STEP в этой копии заменены описанием. Где у соединения есть токен, указано только, что он есть и где хранится.

Обозначения:
- `401` — сервер жив и требует авторизацию;
- «хаб» — можно перенести в MCP-хаб Веры;
- «локально» — привязано к ноутбуку или приложению, в хаб не переносится.

## Вера
| Соединение | Транспорт | Где настроено | Проба | Хаб |
|---|---|---|---|---|
| `vera-room` — комната агентов | http `https://dima.veranda.my/mcp` | `~/.claude.json`, токен в заголовке | 401 | это и есть основа хаба |
| `vera` — личная память | stdio `uv run …\vera-mcp\server.py` → VERA_URL + внутренний секрет | `~/.claude.json`, `~/.claude/settings.json` | — | хаб (сервер уже рядом) |

## AIbroker
| `broker-video` — видео-MCP (PR #174) | http `https://aib.zapleo.com/mcp/video` | нигде у клиентов не подключён | 401 | хаб |

## SnifferBot
| `sniffer-catalog` — публичный каталог | http `https://lbot.zapleo.com/mcp` | — | 307 (редирект, жив) | хаб |
| `sniffer-personal` — личные рассылки | http `https://lbot.zapleo.com/mcp/personal` | — | 401 | хаб |

## SIM
Своего MCP нет. Видео через Broker. HeyGen — см. ниже.

## Stepan2
| `stepan` | stdio, скрипт в локальном checkout stepan2 | `~/.claude/settings.json` | локальный процесс | хаб только если поднять его на сервере (stepan2 уже на hetzner) |

## IT STEP / SintOS
| `sintos` | http, адрес IT STEP (в админке хаба) | конфиг проекта SintOS на ноутбуке | 401 | хаб, токен выдаёт IT STEP |
| `itstep-crm` | http, адрес IT STEP (токен прямо в URL) | `~/.claude/settings.json` | 401 | хаб; токен заносит Дима, не агенты |
| Atlassian (Jira/Confluence) | коннектор claude.ai, OAuth | аккаунт Claude | — | нет: OAuth аккаунта, для хаба нужен свой API-токен Atlassian |

## Личное и общее
| `telegram` — личный аккаунт Димы | stdio, локальный запуск (MTProto-сессия на ноутбуке) | `~/.claude/settings.json` | локально | решение Димы: перенос = личная Telegram-сессия на сервере |
| Gmail, Google Drive, Google Calendar | коннекторы claude.ai, OAuth | аккаунт Claude | — | нет напрямую: нужен свой OAuth-клиент Google на сервере |
| Claude Docs | first-party коннектор | приложение | — | нет |
| Windsor.ai, Shopify, плагины small-business / product-management (Amplitude, Asana, Slack, Notion, HubSpot…) | коннекторы и плагины, большинство не авторизованы | приложение | — | по одному, если понадобятся; сейчас не используются |

## HeyGen
| `heygen` | http `https://mcp.heygen.com/mcp/v1/`, OAuth | `~/.claude.json` (добавлен 27.11), статус needs_auth | 401 | технически можно: у HeyGen есть динамическая регистрация клиента (`/v1/oauth/register`), refresh_token и device_code. По правилам HeyGen MCP-вход разрешён только из AI-приложений для своего аккаунта, шлюзам запрещён. Генерация через хаб для Марины = риск для аккаунта. REST API (ключ в Broker, все права) — разрешённый путь, кошелёк $0 |

## Только локально, в хаб не переносятся
claude-in-chrome, computer-use, встроенный браузер, terminal, scheduled-tasks, mcp-registry, служебные `ccd_*` приложения Claude, Codex `node_repl`.

## Неизвестно — спросить Dot
MCP, подключённые на DOT-ПК (ChatGPT/Codex, Office, личный MCP Sniffer у Dot, собственные инструменты Dot).
