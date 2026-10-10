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

## Подписки (`events/subscribe`, `events/unsubscribe`)

Параметры по спецификации OpenAI: `name` (`ready` | `result` | `blocker`), `arguments`
(`room`, `task_id`?, `kind[]`?), `delivery` (`mode: "webhook"`, `url`, `secret`), `cursor`, `ttlMs`.
Разбор: модели `SubscribeParams`, `UnsubscribeParams`, `Delivery`, `RoomArguments` в `events_subscribe.py`.

1. Принципал — имя токена из scope (`principal_of`, поверх `auth.client_of`). Комната должна входить
   в `allowed_rooms()`, иначе `-32602` с `data.reason`. Аргументы канонизирует `canonical_arguments`.
2. Идентификатор детерминирован: `subscription_id` = `sub_` + sha256 от принципала, URL, имени и
   канонических аргументов. Чужой принципал не может ни отозвать, ни перезаписать чужую подписку:
   у него просто другой id.
3. Callback только HTTPS. `events_net.check_callback_url` режет `http`, userinfo, литеральные и
   разрешённые через DNS адреса, которые не глобальны (loopback, private, link-local, IPv6 local,
   IPv4-mapped). Редиректы не выполняются (`http_client`: `follow_redirects=False`).
   Секрет проверяет `validate_secret`: `whsec_` + base64 на 24..64 байта.
4. Challenge: `{"type":"verification","challenge":<случайный>}`, подпись Standard Webhooks
   (`signed_headers`, `standardwebhooks==1.1.0`). Получатель обязан вернуть тот же challenge,
   сравнение constant-time; иначе `-32015` (`CallbackEndpointError`, `data.reason`:
   `challenge_failed` | `timeout` | `unreachable`). Подписка в БД не создаётся.
5. Секрет хранится зашифрованным (`vera_shared.crypto.encrypt`, ключ `ROOM_OAUTH_KEY`, как в 048).
   Ответ: `{id, refreshBefore, cursor: null, truncated: false}`; `ttlMs` по умолчанию 24 ч,
   максимум 30 суток, `null` = без срока. Повторный subscribe продлевает срок.
6. `events/unsubscribe` идемпотентен; `revoke_subscription` гасит и непоставленные строки outbox.

Ограничение: адрес проверяется при подписке и при каждой попытке доставки, но соединение не
закрепляется на проверенный IP (окно DNS-rebinding остаётся). Для тестов подменяются только
`events_net.net.resolver` и `net.transport` (класс `EventsNet`, функция `system_resolver`);
проверки адресов остаются в силе.

## Outbox (миграция `053_mcp_events.sql`)

Таблицы `room_event_subscriptions` и `room_event_outbox` (модели `RoomEventSubscriptionRow`,
`RoomEventOutboxRow`, `unique(subscription_id, event_id)`). Строку outbox пишет
`vera_shared.room.event_outbox.enqueue_task_event`, которую вызывает `record_event` в транзакции
открытия задачи или смены состояния: откат отменяет и событие, и доставку.

- Соответствие kind журнала и имени события: `EVENT_NAME_BY_KIND` (`created`, `released`,
  `unblocked`, `lease_expired`, `resumed` -> `ready`; `done`, `review` -> `result`; `blocked`,
  `question`, `waiting` -> `blocker`).
- `event_id` = `event_id_for(room, task_id, revision, kind)` (uuid5), `revision` = id записи журнала,
  растёт по задаче. Конверт собирает `build_body`: в `data` лежат `task_id`, `room`, `revision`, `kind`,
  `correlation_id` (`room/task_id`), `causation_id`, `actor`; `parse_body` читает его обратно.
- Самопетля: если актор события и есть подписчик, а этой подписке по задаче уже ставилось событие
  (оно становится `causation_id`), новое событие того же имени не ставится. Результат (`result`) и
  блокер (`blocker`) от того же принципала идут в свои подписки и доставляются.
