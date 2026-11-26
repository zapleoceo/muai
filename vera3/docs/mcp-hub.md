# MCP-хаб: один вход для агентов

`https://dima.veranda.my/hub/mcp` — один MCP-сервер (Streamable HTTP,
stateless, JSON-ответы), за которым стоят все серверные MCP-соединения
владельца, разложенные по проектам. Агенты (Claude, Dot, Codex, будущие
персонажи) подключают только его; каждый видит лишь то, что ему разрешено.
Админка — `https://dima.veranda.my/hub` (только владелец).

Инвентаризация, с которой хаб начинался: [mcp-inventory-2026-11-27.md](./mcp-inventory-2026-11-27.md).

## Где живёт и почему отдельным сервисом

Хаб — отдельный контейнер `vera3-mcp-hub` (`services/mcp-hub`, пакет
`vera_hub`, хост `127.0.0.1:8008`), а не третий «мир» внутри `vera3-mcp`:

- **Изоляция отказов.** Хаб держит исходящие соединения к чужим серверам и,
  при необходимости, stdio-процессы. Зависший апстрим, утечка памяти в
  дочернем процессе или перезапуск хаба не задевают комнату агентов и личную
  память — у них свой процесс, свой лимит памяти, свой рестарт.
- **Лёгкость.** Хаб не тянет mcp SDK: сервер и клиенты протокола — свой
  тонкий JSON-RPC на starlette + httpx (`mem_limit: 192m`). Поведение
  полностью под контролем: фильтр `tools/list` по правам агента нужен на
  каждый запрос, а высокоуровневый сервер SDK держит один общий список.
- **Своя граница секретов.** Ключ расшифровки секретов апстримов
  (`HUB_SECRET_KEY`) есть только у `mcp-hub` и `dashboard`; у `vera3-mcp` его нет.
- Цена — отдельный `location = /hub/mcp` в nginx (тот же файл
  `infra/nginx/vera3-mcp.location.conf`, ручной `nginx -t && systemctl reload nginx`)
  и строка в матрице сборки и покрытия CI.

Комната и память Веры подключены к хабу как обычные апстримы по внутреннему
адресу `http://mcp:8000/mcp` — тот же код, те же права, что у прямого входа.

## Протокол

- `POST /hub/mcp`, заголовок `Authorization: Bearer <токен агента>`.
- Методы: `initialize` (версии `2025-11-25`, `2025-06-18`, `2025-03-26`;
  неизвестная — ответ `2025-06-18`), `ping`, `tools/list`, `tools/call`;
  уведомления — `202`. Остальное — `-32601`. Пакеты JSON-RPC (массив) не
  поддерживаются (`-32600`), тело больше 1 МБ — `413`.
- `GET`/`DELETE` — `405`: потока SSE и сессий у хаба нет, каждый запрос самостоятелен.
- Запрос с заголовком `Origin` — `403`: агенты не браузеры, это защита от
  DNS-rebinding и CSRF.
- Имена инструментов: `<проект>__<апстрим>__<инструмент>`
  (`tool_name` / `split_tool_name`, `vera_shared/hub/names.py`). Слаги проекта
  и апстрима — `[a-z][a-z0-9]{1,15}`, без `_`, пара уникальна в БД — коллизий
  нет. К описанию добавляется метка `[Проект / апстрим]`. Имя длиннее 128
  символов (лимит MCP) пропускается с предупреждением в лог.
- Инструмент вне прав агента, несуществующий и «ждущий секрета» дают одну и ту
  же ошибку `-32602 unknown tool`: агент не узнаёт, что стоит за хабом.
- Ошибка JSON-RPC апстрима передаётся агенту с его кодом и префиксом
  `проект/апстрим:`; таймаут, занятость и недоступность — `-32001`.

## Проекты

`PROJECTS`: `vera` (Vera), `aib` (AIbroker), `sniffer` (SnifferBot), `sim`
(SIM), `stepan` (Stepan2), `itstep` (IT STEP / SintOS), `personal` (Personal).
Список зашит в коде, а не в БД: проект — граница прав, а не настройка.

## Агенты и права

- Таблица `hub_agents`: имя, sha256 токена (`token_hash`), первые 9 символов
  токена для опознания (`token_prefix`), список прав `allow`, `enabled`.
- Токен (`vhub_…`, `new_agent_token`) выдаётся владельцем в админке
  (`create_agent`, `rotate_agent`) и показывается **один раз**; в БД — только хеш.
- Права (`normalize_allow`, `allowed`): `проект` — все апстримы проекта,
  `проект/апстрим` — один. Подстановки `*` нет: «доступ ко всему» — явный
  список всех проектов.
- Вход (`AgentAuth.resolve`): поиск по хешу (`agent_by_token`), кэш 30 с на
  найденного и на неверный токен. Отключение агента или ротация токена
  действуют не позже чем через 30 с. `last_seen_at` обновляется не чаще раза в
  5 минут (`touch_agent`).
- Частоту и параллелизм режет nginx теми же зонами, что `/mcp`: ключ —
  заголовок `Authorization`, то есть токен агента.

## Апстримы

Таблица `hub_upstreams` (модель `HubUpstreamRow`, миграция `064_mcp_hub.sql`):
`project`, `slug`, `title`, `transport` (`http` | `stdio`), `url` или
`command`, `auth_kind`, `auth_header`, `secret_enc`, `enabled`, `hint`
(подсказка в админке), `timeout_s` (1–120), `max_concurrency` (1–16) и поля
здоровья. Проверка настроек до записи — `UpstreamSpec.validate`
(`vera_shared/hub/spec.py`).

Авторизация апстрима (`auth_kind`):

| Транспорт | Вид | Что делает с секретом |
|---|---|---|
| http | `none` | без секрета |
| http | `bearer` | `Authorization: Bearer <секрет>` |
| http | `header` | свой заголовок `auth_header: <секрет>` (служебные заголовки протокола запрещены) |
| http | `url` | секрет — полный URL с токеном (для серверов, где токен в адресе); поле `url` — только справочное, без токена |
| stdio | `none` / `env` | `env`: секрет кладётся в переменную окружения `auth_header` дочернего процесса |

Клиенты (`vera_hub/upstream_http.py`, `vera_hub/upstream_stdio.py`):

- `HttpUpstream` — Streamable HTTP: `initialize` лениво, сессия
  `Mcp-Session-Id` переживает вызовы, `404` на сессию — один повтор с новым
  `initialize`. Ответ — JSON или SSE (берётся сообщение с нашим id). Тело
  ответа ограничено 8 МБ. Редиректы проходят (у Sniffer `/mcp` → `/mcp/catalog`);
  на чужой хост httpx заголовок `Authorization` не несёт.
- `StdioUpstream` — дочерний процесс, JSON-RPC построчно, перезапуск, если
  умер. Окружение — только `PATH`/`HOME`/`LANG`/`LC_ALL`/`TZ` плюс секрет:
  `HUB_SECRET_KEY` и `DATABASE_URL` хаба процессу не достаются. stderr
  отбрасывается (stdio-серверы печатают туда токены и тела). Запись в уже
  закрытый stdin — перезапуск и один повтор (запрос до процесса не дошёл);
  смерть процесса посреди вызова — ошибка без повтора (вызов мог исполниться).

Лимиты (`Registry.call`, `vera_hub/upstreams.py`): семафор на апстрим —
не больше `max_concurrency` параллельных вызовов, ожидание очереди 2 с, потом
ответ `busy`; таймаут `timeout_s` на вызов — `timeout`. Один зависший апстрим
отвечает агенту ошибкой и не держит остальные. Настройки апстримов читаются из
БД с кэшем 15 с: правка в админке действует без перезапуска.

`Upstream.blocker()` — почему апстрим сейчас не вызывается: `disabled`,
`needs_secret` (секрет нужен, но не задан), `needs_config` (нет URL/команды).
Такие апстримы не попадают в `tools/list`.

### Каталог и здоровье

- `Catalog` кэширует `tools/list` каждого апстрима на 5 минут (с пагинацией
  `nextCursor`, до 10 страниц). `tools/list` агента дозапрашивает только
  устаревшие апстримы — параллельно, с таймаутом не больше 10 с; не ответивший
  апстрим отдаёт прежний список или выпадает, остальные отдаются.
- `health_loop` раз в `HUB_HEALTH_INTERVAL_S` (60 с, не меньше 15) делает
  `check_all`: `initialize` + `tools/list` на каждый апстрим, итог —
  `set_health` в `hub_upstreams` (`ok` / `down` с видом ошибки / `needs_secret` /
  `needs_config` / `disabled`, число инструментов). Это и есть статус в админке.

## Секреты

- Секрет апстрима хранится только в `hub_upstreams.secret_enc` как шифротекст
  Fernet (`seal`/`unseal`, `vera_shared/hub/secrets.py`, формат `enc1:` из
  `vera_shared.crypto`). CHECK в миграции не пускает в колонку открытый текст.
- Ключ — `HUB_SECRET_KEY` (≥32 символов, `openssl rand -hex 32`, `hub_key`),
  отдельный от общего `TOKEN_SECRET`: его получают только `mcp-hub` и
  `dashboard`. `mcp-hub` без ключа не стартует (`HubKeyMissing`).
- Админка: поле секрета — только на запись (`type=password`, пустое значение
  оставляет прежний секрет, отдельная галка — стереть). После сохранения
  страница показывает лишь «задан / нет» (`upstream_dict` отдаёт `has_secret`,
  без шифротекста). Страницы `/hub` — `Cache-Control: no-store`.
- Серверный CLI (`vera_hub.cli`, `set_secret`) читает секрет из stdin, не из
  аргументов (argv видно в `ps` и в истории shell):
  `printf %s "$TOKEN" | docker exec -i vera3-mcp-hub python -m vera_hub.cli secret vera/room`.
- Агент секрета не видит: он не уходит ни в ответ `tools/list`/`tools/call`,
  ни в журнал, ни в логи; `repr` апстрима его не печатает.
- **Агенты секреты не вводят.** Токены чужих сервисов (IT STEP, Google,
  Atlassian, личный Telegram) заносит Дима в админке. Сервисные токены наших
  собственных сервисов заводятся на сервере штатным способом этих сервисов и
  кладутся CLI прямо из серверного окружения — не копируются с ноутбука.

## Журнал вызовов

`hub_calls` (`HubCallRow`, `record_call` → `insert_call` фоновой задачей,
`drain_calls` при остановке): агент, метод, имя инструмента, проект, апстрим,
статус (`ok`, `tool_error` — `isError` в результате, `error`, `timeout`,
`busy`, `denied`), длительность в мс, вид ошибки. **Без аргументов, ответов и
заголовков** — персональные данные и секреты туда не попадают. Последние 100
вызовов — в админке (`recent_calls`).

## Агент за хабом в комнате и памяти Веры

Хаб ходит в `/mcp` одним сервисным токеном `hub` (запись в `ROOM_TOKENS` и в
`MCP_TOKENS`) и передаёт имя своего агента заголовком `X-Vera-Hub-Agent` —
только внутренним апстримам (`HUB_INTERNAL_HOSTS`, по умолчанию `mcp`,
`internal_hosts`); внешним сервисам имя агента не уходит. `vera3-mcp`
учитывает заголовок только у клиентов из `HUB_DELEGATE_CLIENTS` (по умолчанию
`hub`; `hub_clients`, `delegated_client` в `vera_mcp/hub_delegate.py`): агент
`dot` за хабом пишет в комнату как `hub/dot`, в `mcp_audit.client` — тоже
`hub/dot`. Префикс `hub/` не даёт выдать себя за настоящего агента комнаты:
`ROOM_ATTEST_ACTORS` и `ROOM_OAUTH_ACTORS` его не содержат. Адресовать такому
агенту сообщение в комнате — `to_agent="hub/dot"`.

## Админка `/hub`

Только владелец (`owner_or_redirect` на GET, `owner_post_gate` — владелец и
запрос со страницы дашборда — на POST). Пункт «MCP-хаб» в меню.

- Проекты и апстримы: статус здоровья, число инструментов, время проверки,
  подсказка; форма правки и форма добавления (`upstream_form`, `hub_save_upstream`).
- Агенты: токен (только префикс), права галками по проектам и апстримам,
  вкл/выкл, «выпустить новый токен» (`agent_form`, `hub_save_agent`,
  `hub_rotate_agent`). Новый токен показывается одной страницей (`token_page`).
- Журнал вызовов (`calls_section`).

## Что подключено (2026-11-27)

| Проект | Апстрим | Статус | Как подключён |
|---|---|---|---|
| Vera | `vera/room` | подключён | `http://mcp:8000/mcp`, сервисный токен `hub` в `ROOM_TOKENS` |
| Vera | `vera/memory` | подключён | `http://mcp:8000/mcp`, сервисный токен `hub` в `MCP_TOKENS`; полный доступ к памяти — агентам только по решению Димы |
| AIbroker | `aib/video` | подключён | `https://aib.zapleo.com/mcp/video`, ключ приватного video-only проекта Broker `vera-hub-video` (скоуп `llm:video`, потолки трат); генерация платная, инструмент сам требует `confirm_paid_generation` |
| SnifferBot | `sniffer/catalog` | подключён | `https://lbot.zapleo.com/mcp/catalog`, публичный, без ключа |
| SnifferBot | `sniffer/personal` | подключён | `https://lbot.zapleo.com/mcp/personal`, личный ключ Димы, выпущен на сервере Sniffer тем же `CatalogWatchRepository.issue`, что команда `/mcp` бота |
| IT STEP | `itstep/crm` | ждёт секрета | токен в URL выдаёт IT STEP, заносит Дима |
| IT STEP | `itstep/sintos` | ждёт секрета | bearer-токен выдаёт IT STEP, заносит Дима |
| IT STEP | `itstep/atlassian` | выключен | решение Димы: свой API-токен или OAuth-клиент Atlassian |
| Personal | `personal/google` | выключен | решение Димы: собственный OAuth-клиент Google на сервере |
| Personal | `personal/telegram` | выключен | решение Димы: перенос личной MTProto-сессии на сервер |
| Stepan2 | `stepan/stepan` | выключен | stdio-сервер `stepan_mcp.py` живёт на ноутбуке; для хаба — поднять HTTP-сервисом в стеке stepan2 |
| SIM | — | — | своего MCP нет; видео — через `aib/video` |

MCP на DOT-ПК (ChatGPT/Codex, Office, собственные инструменты Dot) —
запрос Dot в комнате Vera, сообщение #7767; подключаются в хаб после его ответа.

### HeyGen в хаб не подключается

Технически можно: у `https://mcp.heygen.com/mcp/v1/` есть динамическая
регистрация OAuth-клиента, refresh_token и device_code. Но по правилам HeyGen
MCP-вход разрешён только AI-приложениям для собственного аккаунта
пользователя; шлюзы и посредники, через которые ходят другие агенты,
запрещены. Генерация через хаб поставила бы под риск аккаунт. HeyGen MCP
остаётся подключённым напрямую к Claude; для генерации из наших сервисов —
разрешённый путь, REST API HeyGen через Broker (`aib/video` с провайдером
`heygen`, если владелец выдаст проекту скоуп `llm:video:heygen`).

### Только локально, в хаб не переносятся

claude-in-chrome, computer-use, встроенный браузер, terminal,
scheduled-tasks, mcp-registry, служебные `ccd_*`, Codex `node_repl` —
привязаны к ноутбуку или приложению. Коннекторы claude.ai на OAuth аккаунта
(Gmail, Drive, Calendar, Atlassian) в хаб не переносятся: для сервера нужен
собственный клиент.

## Подключить агента

```bash
claude mcp add --transport http vera-hub https://dima.veranda.my/hub/mcp \
  --header "Authorization: Bearer <токен агента из админки>"
```

Codex CLI (`~/.codex/config.toml`):

```toml
[mcp_servers.vera-hub]
url = "https://dima.veranda.my/hub/mcp"
bearer_token_env_var = "VERA_HUB_TOKEN"
```

## Эксплуатация

- Env (`infra/.env`): `HUB_SECRET_KEY`; сервисный токен `hub` — в
  `ROOM_TOKENS` и `MCP_TOKENS` (разные значения, правило `validate_room_tokens`).
- Миграция `064_mcp_hub.sql` — до выкладки (`scripts/apply_migration.sh`).
- nginx: `location = /hub/mcp` в `infra/nginx/vera3-mcp.location.conf`, после
  выкладки один раз `nginx -t && systemctl reload nginx`.
- CLI в контейнере: `secret <проект/апстрим>` (секрет из stdin),
  `agent-create <имя> [права…]` (печатает токен один раз),
  `agent-set <имя> [--allow …] [--disable|--enable]` (`set_agent`).
- Монитор подхватывает контейнер сам (список берётся из `compose config`),
  `/healthz` открыт.

## Код

| Где | Что |
|---|---|
| `shared/vera_shared/db/models_hub.py` | `HubUpstreamRow`, `HubAgentRow`, `HubCallRow`, `upstream_dict` |
| `shared/vera_shared/hub/names.py` | `PROJECTS`, `HubNameError`, `valid_slug`, `valid_agent_name`, `tool_name`, `split_tool_name`, `normalize_allow`, `allowed` |
| `shared/vera_shared/hub/secrets.py` | `hub_key`, `HubKeyMissing`, `seal`, `unseal`, `new_agent_token`, `token_hash` |
| `shared/vera_shared/hub/spec.py` | `UpstreamSpec` (`validate`), `needs_secret` |
| `shared/vera_shared/hub/repo_upstreams.py` | `list_upstreams`, `save_upstream`, `set_health` |
| `shared/vera_shared/hub/repo_agents.py` | `list_agents`, `create_agent`, `update_agent`, `rotate_agent`, `agent_by_token`, `touch_agent`, `insert_call`, `recent_calls` |
| `services/mcp-hub/src/vera_hub/app.py` | `build_app`: `/hub/mcp`, `/healthz`, lifespan с `health_loop` |
| `services/mcp-hub/src/vera_hub/rpc.py` | `HubRpc` (`handle`), `error` — JSON-RPC-ответ с ошибкой |
| `services/mcp-hub/src/vera_hub/auth.py` | `AgentAuth` (`resolve`), `AgentCtx` |
| `services/mcp-hub/src/vera_hub/upstreams.py` | `Registry` (`upstreams`, `find`, `call`, `close`), `Upstream` (`of`, `blocker`), `Client`, `internal_hosts` |
| `services/mcp-hub/src/vera_hub/upstream_http.py` | `HttpUpstream` (`request`) |
| `services/mcp-hub/src/vera_hub/upstream_stdio.py` | `StdioUpstream` (`request`) |
| `services/mcp-hub/src/vera_hub/catalog.py` | `Catalog` (`refresh`, `cached`, `tools_for`) |
| `services/mcp-hub/src/vera_hub/health.py` | `health_loop`, `check_all`, `interval_s` |
| `services/mcp-hub/src/vera_hub/calls.py` | `record_call`, `drain_calls` |
| `services/mcp-hub/src/vera_hub/errors.py` | `UpstreamError` (`kind`: timeout, busy, down, protocol, rpc) |
| `services/mcp-hub/src/vera_hub/cli.py` | `set_secret`, `set_agent` |
| `services/dashboard/src/dashboard/hub_routes.py` | `hub_page`, `hub_save_upstream`, `hub_save_agent`, `hub_rotate_agent` |
| `services/dashboard/src/dashboard/hub_forms.py` | `form_id`, `upstream_from_form`, `secret_from_form`, `grants_from_form` |
| `services/dashboard/src/dashboard/hub_view.py` | `hub_intro`, `health_pill`, `upstream_form`, `upstreams_section` |
| `services/dashboard/src/dashboard/hub_view_agents.py` | `agent_form`, `agents_section`, `calls_section`, `token_page` |
| `services/mcp/src/vera_mcp/hub_delegate.py` | `hub_clients`, `delegated_client` |

Тесты: `tests/unit/test_hub_protocol.py` (протокол, права, изоляция секретов),
`test_hub_upstreams.py` (таймаут и лимит апстрима, сессии, SSE, stdio,
здоровье), `test_hub_store.py` (имена, проверка настроек, шифрование,
репозитории, CLI), `test_hub_admin.py` (админка, атрибуция `hub/<агент>`).
