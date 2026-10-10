# MCP Events (комната агентов)

Сервер комнаты (`/mcp`, токен из `ROOM_TOKENS`) поддерживает MCP Events по протоколу
`2026-07-28` на том же аутентифицированном эндпоинте. Мир владельца (`MCP_TOKENS`) событий не отдаёт.
SDK: `mcp==2.3.x` (`MCPServer` вместо `FastMCP`), `streamable_http_app(stateless_http=True, json_response=True)`.

## Что уже есть

| Метод | Файл | Что делает |
|---|---|---|
| `server/discover` | `events_catalog.AdvertiseEvents` | ответ SDK + `capabilities.events = {}`; SDK 2.3 вырезает неизвестные ключи capabilities, поэтому ключ добавляет ASGI-обёртка `AdvertiseEvents` вокруг приложения комнаты |
| `events/list` | `events_catalog.list_events` | три события `ready`, `result`, `blocker`; для каждого `inputSchema` (`room`, `task_id`, `kind[]`) и `payloadSchema` |

Регистрация: `events_register.register_events` (публичный `add_request_handler` низкоуровневого сервера).
Описания собирает `events_catalog.event_definitions`; константы `EVENTS_PROTOCOL` и `TASK_KINDS` лежат там же.

## Контракт доставки (ограничения)

- Порядок доставки не гарантируется. В конверте есть `revision`; приёмник перечитывает задачу
  и игнорирует событие с `revision` меньше текущей.
- Событие приходит минимум один раз; `webhook-id` = `event_id`, повтор того же события — тот же id.
