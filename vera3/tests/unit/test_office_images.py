from __future__ import annotations

import hashlib
import io

import httpx
import pytest
from PIL import Image
from sqlalchemy import func, select
from vera_mcp.auth import OFFICE_PRINCIPAL_KEY
from vera_mcp.office_routes import office_app
from vera_shared.db.engine import get_session
from vera_shared.db.models_office import OfficeImageRow
from vera_shared.db.models_room import RoomMessageRow, RoomTaskRow
from vera_shared.office import images, intake
from vera_shared.office.acl import OfficePrincipal
from vera_shared.office.intake_input import InvalidInput, clean_attachments
from vera_shared.room import tasks

ROOM = "office-image-test"
BOT = OfficePrincipal(name="veranda_bot", project="Veranda")
OTHER = OfficePrincipal(name="other_bot", project="Veranda")
WRONG_PROJECT = OfficePrincipal(name="veranda_bot", project="Other")


@pytest.fixture(autouse=True)
def _room(monkeypatch, sqlite_db):
    monkeypatch.setenv("OFFICE_ROOM", ROOM)
    monkeypatch.setenv("ROOM_NAMES", ROOM)
    return sqlite_db


def _png(color: str = "red") -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(stream, format="PNG")
    return stream.getvalue()


def _attachment(content: bytes) -> dict:
    return {"type": "photo", "file_id": "telegram-photo-id", "mime": "image/png",
            "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


async def _create(content: bytes, cid: str = "image-1") -> dict:
    return await intake.create_task(
        BOT, cid, "Image task", "Please inspect image",
        {"channel": "telegram", "chat": "-100", "message": "1", "from_id": 7},
        {"channel": "telegram", "chat": "-100", "reply_to": "1"},
        attachments=[_attachment(content)])


@pytest.mark.asyncio
async def test_image_task_is_invisible_until_verified_file_arrives():
    content = _png()
    made = await _create(content)
    async with get_session() as s:
        assert await s.get(RoomTaskRow, (ROOM, made["task_id"])) is None
    assert made["status"] == "pending_media" and made["revision"] == 0
    assert (await _create(content))["task_id"] == made["task_id"]

    saved = await images.put_image(BOT, made["task_id"], 0, content)
    assert saved["ready"] and saved["stored"]
    async with get_session() as s:
        assert await s.get(RoomTaskRow, (ROOM, made["task_id"])) is not None
        count = (await s.execute(select(func.count()).select_from(RoomMessageRow).where(
            RoomMessageRow.task_id == made["task_id"]))).scalar_one()
    assert count == 1
    with pytest.raises(LookupError):
        await images.read_image("claude", ROOM, made["task_id"], 0)
    held = await tasks.claim(room=ROOM, task_id=made["task_id"], agent="claude",
                             lease_seconds=300)
    assert held["lease_holder"] == "claude"
    assert await images.read_image("claude", ROOM, made["task_id"], 0) == (
        content, "image/png", hashlib.sha256(content).hexdigest())
    with pytest.raises(LookupError):
        await images.read_image("codex", ROOM, made["task_id"], 0)


@pytest.mark.asyncio
async def test_retry_is_idempotent_and_different_content_or_actor_is_rejected():
    content = _png()
    made = await _create(content)
    await images.put_image(BOT, made["task_id"], 0, content)
    repeated = await images.put_image(BOT, made["task_id"], 0, content)
    assert repeated["ready"] and not repeated["stored"]
    async with get_session() as s:
        count = (await s.execute(select(func.count()).select_from(OfficeImageRow))).scalar_one()
        assert count == 1
    with pytest.raises(InvalidInput, match="size|sha256"):
        await images.put_image(BOT, made["task_id"], 0, _png("blue"))
    with pytest.raises(LookupError):
        await images.put_image(OTHER, made["task_id"], 0, content)
    with pytest.raises(LookupError):
        await images.put_image(WRONG_PROJECT, made["task_id"], 0, content)
    with pytest.raises(LookupError):
        await images.put_image(BOT, made["task_id"], 1, content)


def test_invalid_image_metadata_and_bytes_are_rejected():
    content = _png()
    valid = _attachment(content)
    for invalid in ({**valid, "sha256": "x"}, {**valid, "size": 0},
                    {**valid, "mime": "image/svg+xml"}, {**valid, "type": "voice"},
                    {**valid, "size": True}):
        with pytest.raises(InvalidInput):
            clean_attachments([invalid])
    with pytest.raises(InvalidInput, match="total size"):
        clean_attachments([{**valid, "size": 4 * 1024 * 1024}] * 3)
    with pytest.raises(InvalidInput, match="count"):
        clean_attachments([valid] * 5)
    with pytest.raises(InvalidInput):
        images.verify_image(b"not an image", "image/png",
                            hashlib.sha256(b"not an image").hexdigest())
    with pytest.raises(InvalidInput):
        images.verify_image(content, "image/jpeg", hashlib.sha256(content).hexdigest())
    with pytest.raises(InvalidInput):
        images.verify_image(content, "image/png", "0" * 64)


@pytest.mark.asyncio
async def test_http_image_upload_waits_for_all_bytes_and_replays_safely():
    first, second = _png("red"), _png("blue")
    made = await intake.create_task(
        BOT, "image-http", "Image task", "Inspect both images",
        {"channel": "telegram", "chat": "-100", "message": "2"},
        {"channel": "telegram", "chat": "-100", "reply_to": "2"},
        attachments=[_attachment(first), {**_attachment(second), "file_id": "second"}])

    app = office_app()

    async def authenticated(scope, receive, send):
        scope[OFFICE_PRINCIPAL_KEY] = BOT
        await app(scope, receive, send)

    transport = httpx.ASGITransport(app=authenticated)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://office") as client:
        path = f"/tasks/{made['task_id']}/images/"
        first_response = await client.put(path + "0", content=first)
        assert first_response.status_code == 202
        assert first_response.json()["ready"] is False
        async with get_session() as s:
            assert await s.get(RoomTaskRow, (ROOM, made["task_id"])) is None
        second_response = await client.put(path + "1", content=second)
        assert second_response.status_code == 200
        assert second_response.json()["ready"] is True
        replay = await client.put(path + "1", content=second)
        assert replay.status_code == 200
        assert replay.json()["stored"] is False
        changed = await client.put(path + "1", content=first)
        assert changed.status_code == 422


@pytest.mark.asyncio
async def test_claimed_executor_receives_mcp_image_content(monkeypatch):
    import base64

    from mcp.types import ImageContent
    from vera_mcp import room_tools

    content = _png()
    made = await _create(content, "image-mcp")
    await images.put_image(BOT, made["task_id"], 0, content)
    await tasks.claim(room=ROOM, task_id=made["task_id"], agent="claude",
                      lease_seconds=300)
    monkeypatch.setattr(room_tools, "client_of", lambda ctx: "claude")
    response = await room_tools.room_task_image(made["task_id"], 0, None, ROOM)
    assert response[0].text == "sha256:" + hashlib.sha256(content).hexdigest()
    assert isinstance(response[1], ImageContent)
    assert base64.b64decode(response[1].data) == content
    monkeypatch.setattr(room_tools, "client_of", lambda ctx: "codex")
    with pytest.raises(LookupError):
        await room_tools.room_task_image(made["task_id"], 0, None, ROOM)
