"""Authority for routing a Veranda Office task to dot."""
from __future__ import annotations

import os
from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.office import repository as repo
from vera_shared.office.acl import OfficePrincipal


def is_owner_message(principal: OfficePrincipal, origin: dict[str, Any],
                     recipient: dict[str, Any]) -> bool:
    owner = os.environ.get("OWNER_TELEGRAM_ID", "")
    allowed_groups = set(os.environ.get("OFFICE_OWNER_DISPATCH_CHATS", "").split(","))
    chat = str(origin.get("chat"))
    return (owner.isdecimal() and int(owner) > 0
            and principal.name == "veranda_bot" and principal.project == "Veranda"
            and origin.get("channel") == recipient.get("channel") == "telegram"
            and str(origin.get("from_id")) == owner
            and str(recipient.get("chat")) == chat
            and (chat == owner or chat in allowed_groups))


async def verified_office_origin(task: dict[str, Any]) -> bool:
    """Check the write-once Office origin, never an editable task ref, before dispatch."""
    if task.get("project") != "Veranda" or task.get("created_by") != "veranda_bot":
        return False
    async with get_session() as s:
        origin = await repo.origin_of(s, task["task_id"])
    if origin is None or origin.room != task["room"] or origin.creator_principal != "veranda_bot":
        return False
    return is_owner_message(
        OfficePrincipal(name="veranda_bot", project="Veranda"),
        origin.origin, origin.result_recipient)
