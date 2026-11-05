"""Deliver private attachment bytes through the existing Room message reader."""
from __future__ import annotations

import base64
from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.office import repository
from vera_shared.office.file_content import document_text
from vera_shared.office.images import expected_images, read_attachment


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
        preview = document_text(content, mime)
        if preview is not None:
            item.update(encoding="utf-8", content=preview)
        else:
            item.update(encoding="base64", content=base64.b64encode(content).decode("ascii"))
        result.append(item)
    return result
