"""Telegram → graph layer: upsert Entity/Alias/Membership rows on each
ingested message. Cheap, in-band; happens after save_event.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from vera_shared.graph.chat_link import resolve_migrated_chat
from vera_shared.graph.repo import (
    find_entity_by_alias,
    upsert_entity,
    upsert_membership,
)
from vera_shared.graph.twin_suggest import suggest_chat_twin, suggest_person_twin

log = logging.getLogger("tg.entity_sync")


def _person_name(user: Any) -> str:
    name = (getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "last_name", "") or "").strip()
    if last:
        name = f"{name} {last}".strip()
    if not name:
        name = getattr(user, "username", None) or f"tg_user_{user.id}"
    return name


PLACEHOLDER_PREFIX = "tg_user_"


async def _sender_is_the_chat(sender_id: int, name: str) -> bool:
    """Безымянный отправитель, чей id — уже известный чат (анонимный админ или
    канал пишут от имени самого чата): из него рождалась заглушка `tg_user_<id>`
    рядом с настоящей сущностью чата (аудит 2026-09-26: 11 штук). Живого юзера с
    именем это не касается, даже если его id случайно равен id чата."""
    if not name.startswith(PLACEHOLDER_PREFIX):
        return False
    try:
        return await find_entity_by_alias("telegram", f"chat:{sender_id}") is not None
    except SQLAlchemyError:
        log.warning("не смог проверить, не чат ли отправитель %s", sender_id, exc_info=True)
        return False


async def _chat_entity_for_migration(chat_id: int, new_id: int, title: str) -> int | None:
    """Гонка двух воркеров на одном alias → IntegrityError: логируем и идём
    дальше без сущности чата, а не теряем персону и членство этого сообщения."""
    try:
        return await resolve_migrated_chat(chat_id, new_id, title)
    except SQLAlchemyError:
        log.warning("миграция чата %s→%s: не удалось связать, пропускаю сообщение "
                    "в граф чата", chat_id, new_id, exc_info=True)
        return None


async def _suggest_twin(call: Any, entity_id: int, name: str) -> None:
    try:
        await call(entity_id, name)
    except Exception:  # noqa: BLE001 — подсказка двойника не должна ронять приём сообщения
        log.warning("не удалось проверить двойника сущности %s", entity_id, exc_info=True)


async def sync_message_entities(chat: Any, sender: Any) -> None:
    """For every TG message: ensure Chat-entity + Person-entity + Membership exist."""
    chat_type_name = type(chat).__name__.lower()
    chat_id_int = getattr(chat, "id", None)
    if chat_id_int is None:
        return

    # 1) Chat entity (skip for private 1:1 — that IS the person)
    chat_entity_id = None
    if chat_type_name in {"channel", "chat", "chatfull"}:
        is_megagroup = bool(getattr(chat, "megagroup", False))
        chat_entity_type = (
            "supergroup" if (chat_type_name == "channel" and is_megagroup)
            else "channel" if chat_type_name == "channel"
            else "group"
        )
        title = getattr(chat, "title", None) or f"tg_chat_{chat_id_int}"
        migrated_to = getattr(getattr(chat, "migrated_to", None), "channel_id", None)
        if chat_type_name == "chat" and migrated_to:
            chat_entity_id = await _chat_entity_for_migration(chat_id_int, migrated_to, title)
        else:
            chat_entity_id = await upsert_entity(
                type=chat_entity_type, name=title,
                source="telegram", identifier=f"chat:{chat_id_int}",
                attributes={
                    "tg_id": chat_id_int,
                    "tg_type": chat_type_name,
                    "username": getattr(chat, "username", None),
                    "is_megagroup": is_megagroup,
                },
            )
            if chat_entity_type == "supergroup":
                await _suggest_twin(suggest_chat_twin, chat_entity_id, title)

    # 2) Person entity (sender). Каналы постят от СВОЕГО имени — Telethon
    # отдаёт сам Channel как sender'а; персону из него не делаем (chat-entity
    # выше уже покрывает канал; иначе плодятся дубли channel+person с одним
    # @username — их потом приходится сливать на /entities/duplicates).
    sender_is_chat = type(sender).__name__.lower() in {"channel", "chat"}
    if sender is not None and not sender_is_chat \
            and not getattr(sender, "bot", False):
        sender_username = getattr(sender, "username", None)
        sender_id = getattr(sender, "id", None)
        person_name = _person_name(sender) if sender_id is not None else ""
        if sender_id is not None and await _sender_is_the_chat(sender_id, person_name):
            return
        if sender_id is not None:
            person_entity_id = await upsert_entity(
                type="person",
                name=person_name,
                source="telegram",
                identifier=f"user:{sender_id}",
                attributes={
                    "tg_id": sender_id,
                    "username": sender_username,
                    "is_bot": False,
                },
            )

            await _suggest_twin(suggest_person_twin, person_entity_id, person_name)

            # 3) Membership: if message was in a group → sender is member
            if chat_entity_id is not None:
                await upsert_membership(
                    parent_entity_id=chat_entity_id,
                    child_entity_id=person_entity_id,
                    source="telegram", role="member",
                    attributes={"observed_via": "message_seen"},
                )
