"""Deliver private attachment bytes through the existing Room message reader."""
from __future__ import annotations

import base64
from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.office import repository
from vera_shared.office.images import expected_images, read_attachment
from vera_shared.office.intake_input import DOCUMENT_MIMES


async def message_files(agent: str, room: str, task_id: str) -> list[dict[str, Any]]:
    async with get_session() as s:
        origin = await repository.origin_of(s, task_id)
        if origin is None or origin.room != room:
            return []
        indexes = list(expected_images(origin.attachments))
    result = []
    for index in indexes:
        try:
            content, mime, digest = await read_attachment(agent, room, task_id, index)
        except LookupError:
            return []
        item: dict[str, Any] = {"index": index, "mime": mime, "sha256": digest,
                                "size": len(content), "untrusted_data": True}
        if mime in DOCUMENT_MIMES:
            item.update(encoding="utf-8", content=content.decode("utf-8"))
        else:
            item.update(encoding="base64", content=base64.b64encode(content).decode("ascii"))
        result.append(item)
    return result
