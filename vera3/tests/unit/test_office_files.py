from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from vera_mcp import room_tools
from vera_shared.db.engine import get_session
from vera_shared.db.models_office import OfficeImageRow, RoomTaskOriginRow
from vera_shared.office import file_repository, images, intake
from vera_shared.office.acl import OfficePrincipal
from vera_shared.office.file_content import document_text
from vera_shared.office.intake_input import InvalidInput
from vera_shared.room import messages, tasks
from vera_shared.timeutil import utc_naive_now

ROOM = "office-files-test"
BOT = OfficePrincipal(name="veranda_bot", project="Veranda")
CONTENT = b'Office file verification: FILE-20261105-K8\n'


@pytest.fixture(autouse=True)
def _room(monkeypatch, sqlite_db):
    monkeypatch.setenv("OFFICE_ROOM", ROOM)
    monkeypatch.setenv("ROOM_NAMES", ROOM)


async def make(cid: str = "file-1") -> dict:
    return await intake.create_task(BOT, cid, "File", "Inspect inert test file",
        {"channel": "test"}, {"channel": "test"}, attachments=[{
            "type": "document", "file_id": "synthetic-test", "mime": "text/plain",
            "size": len(CONTENT), "sha256": hashlib.sha256(CONTENT).hexdigest()}])


@pytest.mark.asyncio
async def test_existing_message_tool_delivers_exact_file_only_to_leased_actor(monkeypatch):
    made = await make()
    saved = await images.put_image(BOT, made["task_id"], 0, CONTENT)
    assert saved["ready"]
    msg = (await messages.history(room=ROOM, task_id=made["task_id"], limit=10))[0]
    monkeypatch.setattr(room_tools, "client_of", lambda ctx: "claude")
    before = await room_tools.room_message_get(msg["id"], ROOM, made["task_id"], object())
    assert "attachments" not in before
    await tasks.claim(room=ROOM, task_id=made["task_id"], agent="claude", lease_seconds=300)
    result = await room_tools.room_message_get(msg["id"], ROOM, made["task_id"], object())
    attachment = result["attachments"][0]
    assert attachment["content"].encode() == CONTENT
    assert attachment["sha256"] == hashlib.sha256(CONTENT).hexdigest()
    assert attachment["untrusted_data"] is True
    monkeypatch.setattr(room_tools, "client_of", lambda ctx: "codex")
    assert "attachments" not in await room_tools.room_message_get(msg["id"], ROOM, None, object())
    with pytest.raises(LookupError, match="not an image"):
        await images.read_image("claude", ROOM, made["task_id"], 0)


@pytest.mark.asyncio
async def test_expiry_prunes_only_new_bytes_and_cannot_resurrect():
    made, legacy = await make(), await make("legacy")
    for item in (made, legacy):
        await images.put_image(BOT, item["task_id"], 0, CONTENT)
    async with get_session() as s:
        row = await s.get(OfficeImageRow, (ROOM, made["task_id"], 0))
        row.expires_at = utc_naive_now() - timedelta(seconds=1)
        origin = await s.get(RoomTaskOriginRow, (ROOM, made["task_id"]))
        origin.created_at = utc_naive_now() - timedelta(days=2)
        old = await s.get(OfficeImageRow, (ROOM, legacy["task_id"], 0))
        old.expires_at = None
    await tasks.claim(room=ROOM, task_id=made["task_id"], agent="claude", lease_seconds=300)
    with pytest.raises(LookupError):
        await images.read_attachment("claude", ROOM, made["task_id"], 0)
    async with get_session() as s:
        assert await file_repository.prune_expired(s) == 1
    async with get_session() as s:
        assert await s.get(RoomTaskOriginRow, (ROOM, made["task_id"])) is not None
        assert (await s.get(OfficeImageRow, (ROOM, legacy["task_id"], 0))).content == CONTENT
    with pytest.raises(InvalidInput, match="expired"):
        await images.put_image(BOT, made["task_id"], 0, CONTENT)


@pytest.mark.asyncio
async def test_storage_quota_rejects_second_upload_without_dispatch(monkeypatch):
    monkeypatch.setattr(file_repository, "MAX_STORED_BYTES", len(CONTENT))
    first, second = await make(), await make("second")
    await images.put_image(BOT, first["task_id"], 0, CONTENT)
    with pytest.raises(file_repository.StorageFull, match="storage is full"):
        await images.put_image(BOT, second["task_id"], 0, CONTENT)
    assert await messages.history(room=ROOM, task_id=second["task_id"], limit=10) == []


@pytest.mark.parametrize(("content", "mime"), [
    (b'MZ\x00executable', "text/plain"), (b'\xff', "text/plain"),
    (b'not-json', "application/octet-stream"), (b'x' * 65537, "text/csv"),
    (b'<html>bad</html>', "text/html"),
], ids=["binary", "utf8", "json", "oversize", "unsupported"])
def test_binary_or_large_documents_use_file_resource_instead_of_text_preview(content, mime):
    assert document_text(content, mime) is None
