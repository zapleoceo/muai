"""Authority for routing a Veranda Office task to dot."""
from __future__ import annotations

import logging
import os
from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.office import repository as repo
from vera_shared.office.acl import OfficePrincipal

log = logging.getLogger(__name__)

#: Владельца подтвердила не Вера, а сервис (его `origin.from_id`): по этому ref
#: агенты и дашборд видят, на чьём слове держатся auto_pickup и ответственный dot.
OWNER_CLAIM_REF_PREFIX = "owner_claim: service_asserted by "


def owner_claim_refs(principal: OfficePrincipal) -> list[dict[str, str]]:
    return [{"kind": "source", "ref": f"{OWNER_CLAIM_REF_PREFIX}{principal.name}"}]


def log_owner_promotion(principal: OfficePrincipal, task_id: str) -> None:
    """Без текста сообщения: только кто заявил владельца и какая задача поднята."""
    log.info("office owner dispatch: task=%s promoted to dot on owner claim asserted "
             "by principal=%s project=%s", task_id, principal.name, principal.project)


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
