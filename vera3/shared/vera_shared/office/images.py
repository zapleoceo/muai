"""Store verified Office images and release their tasks only after all images arrive."""
from __future__ import annotations

import hashlib
import io
import warnings
from typing import Any

from PIL import Image, UnidentifiedImageError

from vera_shared.db.engine import get_session
from vera_shared.db.models_office import OfficeImageRow
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.office import repository as repo
from vera_shared.office.acl import OfficePrincipal, authorize
from vera_shared.office.file_repository import RETENTION, require_capacity
from vera_shared.office.intake_input import ID_RE, IMAGE_MIMES, InvalidInput
from vera_shared.timeutil import utc_naive_now

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def expected_images(attachments: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {i: att for i, att in enumerate(attachments) if "sha256" in att}


def verify_image(content: bytes, expected_mime: str, expected_sha: str) -> str:
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise InvalidInput("image must be 1..4194304 bytes")
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected_sha:
        raise InvalidInput("image sha256 does not match task attachment")
    if expected_mime not in IMAGE_MIMES:
        return digest
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                fmt = image.format
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise InvalidInput("image pixel count exceeds limit")
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning,
            Image.DecompressionBombError):
        raise InvalidInput("image content is invalid") from None
    mime = FORMATS.get(fmt or "")
    if mime is None or mime != expected_mime:
        raise InvalidInput("image type does not match task attachment")
    return digest


async def put_image(principal: OfficePrincipal, task_id: str, index: int,
                    content: bytes) -> dict[str, Any]:
    authorize(principal, "create")
    if not ID_RE.match(task_id) or index < 0:
        raise InvalidInput("invalid image task or index")
    async with get_session() as s:
        origin = await repo.origin_of(s, task_id, for_update=True)
        if (origin is None or origin.creator_principal != principal.name
                or origin.creator_project != principal.project):
            raise LookupError("task not found")
        expected = expected_images(origin.attachments).get(index)
        if expected is None:
            raise LookupError("image not expected")
        if len(content) != expected["size"]:
            raise InvalidInput("image size does not match task attachment")
        digest = verify_image(content, str(expected["mime"]), str(expected["sha256"]))
        room, files = origin.room, origin.attachments
        prior = await s.get(OfficeImageRow, (room, task_id, index))
        if prior is not None:
            if prior.expires_at is not None and prior.expires_at <= utc_naive_now():
                raise InvalidInput("attachment has expired")
            if prior.sha256 != digest or prior.content != content:
                raise ValueError("image already stored with different content")
            stored = False
        else:
            expiry = origin.created_at + RETENTION
            if expiry <= utc_naive_now():
                raise InvalidInput("attachment upload period has expired")
            await require_capacity(s, len(content))
            s.add(OfficeImageRow(room=room, task_id=task_id, attachment_index=index,
                                 sha256=digest, mime=expected["mime"], content=content,
                                 expires_at=expiry))
            await s.flush()
            stored = True
    from vera_shared.office.intake import complete_image_task

    ready = await complete_image_task(principal, room, task_id, files)
    return {"task_id": task_id, "index": index, "sha256": digest,
            "stored": stored, "ready": ready}


async def all_images_stored(room: str, task_id: str, attachments: list[dict[str, Any]]) -> bool:
    expected = expected_images(attachments)
    if not expected:
        return True
    async with get_session() as s:
        for i in expected:
            row = await s.get(OfficeImageRow, (room, task_id, i))
            if row is None or (row.expires_at is not None and row.expires_at <= utc_naive_now()):
                return False
    return True


async def read_attachment(agent: str, room: str, task_id: str, index: int,
                     ) -> tuple[bytes, str, str]:
    async with get_session() as s:
        task = await s.get(RoomTaskRow, (room, task_id))
        if (task is None or task.lease_holder != agent or task.lease_until is None
                or task.lease_until <= utc_naive_now() or task.status != "in_progress"):
            raise LookupError("image not available to this executor")
        row = await s.get(OfficeImageRow, (room, task_id, index))
        if row is None or (row.expires_at is not None and row.expires_at <= utc_naive_now()):
            raise LookupError("image not found")
        content, mime, digest = row.content, row.mime, row.sha256
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("stored image integrity check failed")
    return content, mime, digest


async def read_image(agent: str, room: str, task_id: str, index: int,
                     ) -> tuple[bytes, str, str]:
    result = await read_attachment(agent, room, task_id, index)
    if result[1] not in IMAGE_MIMES:
        raise LookupError("attachment is not an image")
    return result
