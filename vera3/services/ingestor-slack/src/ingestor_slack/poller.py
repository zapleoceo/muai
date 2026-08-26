"""Slack poller — раз в N секунд забирает новые сообщения каждого канала.

Курсор — `ts` последнего разобранного сообщения: у Slack это одновременно и
время, и идентификатор сообщения в канале, поэтому хвост не теряется при
всплеске активности (та же грабля, что чинили у gmail с date-granular `after:`).

Треды опрашиваются ОТДЕЛЬНО и это не тонкость, а обязательное условие:
`conversations.history` отдаёт только корневое сообщение треда. Ответы в нём
надо забирать через `conversations.replies`, а тред, чьё корневое сообщение
старше курсора, в истории вообще не появится — сколько бы новых ответов там ни
было. Без наблюдения за тредами обсуждения в Slack были бы невидимы навсегда.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from vera_shared.db.engine import init_engine
from vera_shared.db.models_sources import SlackConversationRow
from vera_shared.ingest import poll_forever
from vera_shared.ingest_policy import is_ignored_slack_channel

from ingestor_slack import auth, store
from ingestor_slack.client import SlackAuthError, SlackClient
from ingestor_slack.mapper import message_to_event, parse_ts

log = logging.getLogger("slack")

POLL_S = int(os.environ.get("SLACK_POLL_S", "300"))
BOOTSTRAP_DAYS = int(os.environ.get("SLACK_BOOTSTRAP_DAYS", "7"))
MAX_PAGES = int(os.environ.get("SLACK_MAX_PAGES", "20"))
# Сколько тредов проверяем за прогон на канал и как долго держим их под
# наблюдением. Оба потолка — про расход вызовов, а не про полноту: тред просто
# проверяется реже, а не выпадает.
THREADS_PER_RUN = int(os.environ.get("SLACK_THREADS_PER_RUN", "20"))
THREAD_WATCH_DAYS = int(os.environ.get("SLACK_THREAD_WATCH_DAYS", "21"))
DENY_CHANNELS = frozenset(
    x.strip().lstrip("#").lower()
    for x in os.environ.get("SLACK_DENY_CHANNELS", "").split(",") if x.strip()
)


def bootstrap_ts() -> str:
    return f"{(datetime.utcnow().timestamp() - BOOTSTRAP_DAYS * 86400):.6f}"


def newest_ts(messages: list[dict]) -> str | None:
    return max((str(m.get("ts") or "") for m in messages), default="") or None


class Profiles:
    """Кэш профилей: «id → имя и что известно о человеке».

    Без кэша users.info звался бы на каждое сообщение, а профиль в воркспейсе
    меняется раз в год. Кроме имени берём то, что связывает человека с другими
    каналами: рабочий email — это же алиас gmail, и по нему сущность
    прицепляется к уже существующей, без догадок LLM.
    """

    #: что вытаскиваем из профиля. Не всё подряд: телефон и должность полезны
    #: как контекст, картинки и статус — шум.
    FIELDS = ("email", "phone", "title", "real_name", "display_name")

    def __init__(self, client: SlackClient):
        self._client = client
        self._names: dict[str, str] = {}
        self._profiles: dict[str, dict[str, str]] = {}

    @property
    def known(self) -> dict[str, str]:
        return self._names

    def profile(self, user_id: str) -> dict[str, str]:
        return self._profiles.get(user_id, {})

    async def resolve(self, user_ids: set[str]) -> dict[str, str]:
        for uid in sorted(user_ids - self._names.keys()):
            if not uid:
                continue
            try:
                info = await self._client.user_info(uid)
            except Exception as e:  # noqa: BLE001 — без имени событие всё равно нужно
                log.debug("slack: профиль %s не забрал: %s", uid, e)
                self._names[uid] = uid
                continue
            profile = info.get("profile") or {}
            fields = {}
            for key in self.FIELDS:
                value = str(profile.get(key) or "").strip()
                if value:
                    fields[key] = value
            if info.get("tz"):
                fields["tz"] = str(info["tz"])
            if info.get("is_bot"):
                fields["is_bot"] = "true"
            self._profiles[uid] = fields
            self._names[uid] = str(
                info.get("real_name") or fields.get("real_name")
                or info.get("name") or uid)
        return self._names


async def _events_from(messages: list[dict], row: SlackConversationRow,
                       me_id: str, account: str, names: Profiles) -> list[dict]:
    await names.resolve({str(m.get("user") or "") for m in messages})
    specs = []
    for message in messages:
        spec = message_to_event(
            message, channel_id=row.conversation_id, channel_name=row.name,
            channel_kind=row.kind, is_private=row.is_private,
            me_id=me_id, account=account, names=names.known,
        )
        if spec:
            specs.append(spec)
    return specs


async def poll_threads(client: SlackClient, row: SlackConversationRow,
                       me_id: str, account: str, names: Profiles) -> int:
    """Догнать ответы в наблюдаемых тредах канала."""
    saved = 0
    for thread in await store.due_threads(
        row.conversation_id, limit=THREADS_PER_RUN, watch_days=THREAD_WATCH_DAYS,
    ):
        try:
            messages, complete = await client.replies(
                row.conversation_id, thread.thread_ts,
                oldest=thread.last_reply_ts, max_pages=MAX_PAGES,
            )
        except SlackAuthError:
            raise
        except Exception as e:  # noqa: BLE001 — один тред не должен рвать прогон канала
            log.warning("slack/%s: тред %s не забрал: %s", row.name, thread.thread_ts, e)
            continue
        # Первый элемент ответа — корневое сообщение; оно уже пришло историей.
        replies = [m for m in messages if str(m.get("ts")) != thread.thread_ts]
        fresh = await store.save_events(
            await _events_from(replies, row, me_id, account, names),
            profiles=names)
        saved += len(fresh)
        cursor = newest_ts(replies) if complete else None
        await store.save_thread_cursor(
            thread.id, cursor, parse_ts(cursor) if cursor else None)
    return saved


async def poll_conversation(client: SlackClient, row: SlackConversationRow,
                            me_id: str, account: str, names: Profiles) -> int:
    try:
        messages, complete = await client.history(
            row.conversation_id, oldest=row.last_ts or bootstrap_ts(),
            max_pages=MAX_PAGES,
        )
    except SlackAuthError:
        raise
    except Exception as e:  # noqa: BLE001 — канал мог быть удалён под нами
        log.error("slack/%s: не забрал историю: %s", row.name, e)
        await store.save_cursor(row.conversation_id, None, str(e)[:500])
        return 0

    saved = len(await store.save_events(
        await _events_from(messages, row, me_id, account, names),
        profiles=names))

    # Корневые сообщения тредов — под наблюдение, ответы придут отдельно.
    for message in messages:
        ts = str(message.get("ts") or "")
        if ts and str(message.get("thread_ts") or "") == ts:
            await store.watch_thread(row.conversation_id, ts,
                                     str(message.get("latest_reply") or "") or None,
                                     parse_ts(ts))

    if not complete:
        log.warning("slack/%s: бэклог глубже %d страниц — курсор не двигаю",
                    row.name, MAX_PAGES)
    await store.save_cursor(row.conversation_id,
                            newest_ts(messages) if complete else None, None)

    saved += await poll_threads(client, row, me_id, account, names)
    if saved:
        log.info("slack/%s: %d новых событий", row.name, saved)
    return saved


class _Session:
    """Клиент, «кто я» и кэш имён. Пересобирается после отказа в доступе."""

    def __init__(self) -> None:
        self.client: SlackClient | None = None
        self.me_id = ""
        self.account = ""
        self.names: Profiles | None = None
        self.auth_row: int | None = None

    async def connect(self) -> tuple[SlackClient, Profiles]:
        if self.client is None or self.names is None:
            token, self.auth_row = await auth.load_token()
            self.client = SlackClient(token)
            me = await self.client.whoami()
            self.me_id = str(me.get("user_id") or "")
            self.account = f"{me.get('team') or 'slack'}/{me.get('user') or 'me'}"
            self.names = Profiles(self.client)
            await auth.mark_ok(self.auth_row)
            log.info("Slack: %s (%s), токен %s, опрос каждые %sс",
                     self.account, self.me_id,
                     "из дашборда" if self.auth_row else "из окружения", POLL_S)
        return self.client, self.names

    def reset(self) -> None:
        self.client = None
        self.names = None


async def main_loop() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_engine()
    session = _Session()

    async def poll_once() -> None:
        try:
            client, names = await session.connect()
            raw = await client.list_conversations()
            await names.resolve({str(c.get("user") or "") for c in raw if c.get("is_im")})
            for row in await store.upsert_conversations(raw, names.known):
                # Личку денай-лист не касается: он про шумные служебные каналы.
                if row.kind != "im" and is_ignored_slack_channel(row.name, DENY_CHANNELS):
                    continue
                await poll_conversation(client, row, session.me_id,
                                        session.account, names)
                await asyncio.sleep(1)
        except SlackAuthError as e:
            # Токен отозван или прав не хватает — гасим строку, чтобы дашборд
            # показывал «переподключить», а не «подключено» при мёртвом токене.
            await auth.mark_dead(session.auth_row, str(e))
            session.reset()
            raise

    await poll_forever(name="slack", poll_once=poll_once, interval_s=POLL_S,
                       auth_error=SlackAuthError, log=log)


if __name__ == "__main__":
    asyncio.run(main_loop())
