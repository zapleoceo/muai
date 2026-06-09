"""Telegram → graph layer: upsert Entity/Alias/Membership rows on each
ingested message. Cheap, in-band; happens after save_event.
"""
from __future__ import annotations

import logging
from typing import Any

from vera_shared.graph.repo import upsert_entity, upsert_membership

log = logging.getLogger("tg.entity_sync")


def _person_name(user: Any) -> str:
    name = (getattr(user, "first_name", "") or "").strip()
    last = (getattr(user, "last_name", "") or "").strip()
    if last:
        name = f"{name} {last}".strip()
    if not name:
        name = getattr(user, "username", None) or f"tg_user_{user.id}"
    return name


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

    # 2) Person entity (sender)
    if sender is not None and not getattr(sender, "bot", False):
        sender_username = getattr(sender, "username", None)
        sender_id = getattr(sender, "id", None)
        if sender_id is not None:
            person_entity_id = await upsert_entity(
                type="person",
                name=_person_name(sender),
                source="telegram",
                identifier=f"user:{sender_id}",
                attributes={
                    "tg_id": sender_id,
                    "username": sender_username,
                    "is_bot": False,
                },
            )

            # 3) Membership: if message was in a group → sender is member
            if chat_entity_id is not None:
                await upsert_membership(
                    parent_entity_id=chat_entity_id,
                    child_entity_id=person_entity_id,
                    source="telegram", role="member",
                    attributes={"observed_via": "message_seen"},
                )
