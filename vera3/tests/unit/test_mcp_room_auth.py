"""Токен комнаты видит только room_*-инструменты, токен владельца — только память."""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from vera_mcp.auth import (
    WeakTokenError,
    allowed_rooms,
    load_room_tokens,
    validate_room_tokens,
)
from vera_mcp.server import build_app

OWNER_TOKEN = "owner-token-0123456789-0123456789"
ROOM_CLAUDE = "room-claude-0123456789-0123456789"
ROOM_CODEX = "room-codex-abcdefghij-abcdefghij"
HEADERS = {"Accept": "application/json, text/event-stream",
           "Content-Type": "application/json"}


def _rpc(method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


def _auth(token: str) -> dict:
    return {**HEADERS, "Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _open_client():
    app = build_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


@pytest.fixture
def room_client(monkeypatch, sqlite_db):
    monkeypatch.setenv("MCP_TOKENS", f"claude:{OWNER_TOKEN}")
    monkeypatch.setenv("ROOM_TOKENS", f"claude:{ROOM_CLAUDE},codex:{ROOM_CODEX}")
    return _open_client


async def _tool_names(c: httpx.AsyncClient, token: str) -> set[str]:
    r = await c.post("/mcp", json=_rpc("tools/list"), headers=_auth(token))
    assert r.status_code == 200
    return {t["name"] for t in r.json()["result"]["tools"]}


@pytest.mark.asyncio
async def test_existing_message_reader_injects_actual_actor_for_private_files(room_client):
    import base64
    import hashlib

    from vera_shared.office import images, intake
    from vera_shared.office.acl import OfficePrincipal
    from vera_shared.room import messages, tasks

    content = b"Office file proof HTTP-20261105\n"
    principal = OfficePrincipal(name="vbot", project="Veranda")
    made = await intake.create_task(principal, "file-http", "File", "Test only",
        {"channel": "test"}, {"channel": "test"}, attachments=[{
            "type": "document", "file_id": "test", "mime": "text/plain",
            "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}])
    await images.put_image(principal, made["task_id"], 0, content)
    msg = (await messages.history(room="main", task_id=made["task_id"], limit=10))[0]
    await tasks.claim(room="main", task_id=made["task_id"], agent="claude", lease_seconds=300)
    async with room_client() as c:
        params = {"name": "room_message_get", "arguments": {"id": msg["id"]}}
        response = await c.post("/mcp", json=_rpc("tools/call", params),
                                headers=_auth(ROOM_CLAUDE))
        result = response.json()["result"]
        assert result.get("isError") is not True, result
        assert result["structuredContent"]["attachments"][0]["content"].encode() == content
        other = await c.post("/mcp", json=_rpc("tools/call", params),
                            headers=_auth(ROOM_CODEX))
        assert "attachments" not in other.json()["result"]["structuredContent"]
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwAC"
            "TGCSAQANHQEDgslx/wAAAABJRU5ErkJggg==")
        image_task = await intake.create_task(principal, "image-existing-http", "Image", "Test",
            {"channel": "test"}, {"channel": "test"}, attachments=[{
                "type": "photo", "file_id": "test-image", "mime": "image/png",
                "size": len(png), "sha256": hashlib.sha256(png).hexdigest()}])
        await images.put_image(principal, image_task["task_id"], 0, png)
        image_message = (await messages.history(
            room="main", task_id=image_task["task_id"], limit=10))[0]
        await tasks.claim(room="main", task_id=image_task["task_id"], agent="claude",
                          lease_seconds=300)
        response = await c.post("/mcp", json=_rpc("tools/call", {
            "name": "room_message_get", "arguments": {"id": image_message["id"]}}),
            headers=_auth(ROOM_CLAUDE))
        result = response.json()["result"]
        assert result.get("isError") is not True, result
        picture = next(item for item in result["content"] if item["type"] == "image")
        assert base64.b64decode(picture["data"]) == png
        assert result["structuredContent"]["attachments"][0]["encoding"] == "mcp-image"
        binary = b"PK\x03\x04synthetic opaque file\x00" + b"x" * (1024 * 1024)
        file_task = await intake.create_task(principal, "opaque-http", "Opaque file", "Test",
            {"channel": "test"}, {"channel": "test"}, attachments=[{
                "type": "document", "file_id": "opaque", "mime": "application/zip",
                "size": len(binary), "sha256": hashlib.sha256(binary).hexdigest()}])
        await images.put_image(principal, file_task["task_id"], 0, binary)
        file_message = (await messages.history(
            room="main", task_id=file_task["task_id"], limit=10))[0]
        await tasks.claim(room="main", task_id=file_task["task_id"], agent="claude",
                          lease_seconds=300)
        response = await c.post("/mcp", json=_rpc("tools/call", {
            "name": "room_message_get", "arguments": {"id": file_message["id"]}}),
            headers=_auth(ROOM_CLAUDE))
        result = response.json()["result"]
        assert result.get("isError") is not True, result
        resource = next(item["resource"] for item in result["content"]
                        if item["type"] == "resource")
        assert resource["mimeType"] == "application/octet-stream"
        assert base64.b64decode(resource["blob"]) == binary
        assert result["structuredContent"]["attachments"][0]["encoding"] == "mcp-resource"


def test_room_tokens_parse_and_drop_short():
    assert load_room_tokens({"ROOM_TOKENS": f"codex:{ROOM_CODEX},x:short"}) == {
        ROOM_CODEX: "codex"}


def test_room_token_must_not_reuse_an_owner_token():
    with pytest.raises(ValueError, match="reuse an MCP token"):
        validate_room_tokens({"MCP_TOKENS": f"claude:{OWNER_TOKEN}",
                              "ROOM_TOKENS": f"claude:{OWNER_TOKEN}"})


def test_same_room_token_under_two_names_refuses_start():
    with pytest.raises(ValueError, match="same token twice"):
        validate_room_tokens({"ROOM_TOKENS": f"claude:{ROOM_CODEX},codex:{ROOM_CODEX}"})


def test_allowed_rooms_default_to_main():
    assert allowed_rooms({}) == {"main"}
    assert allowed_rooms({"ROOM_NAMES": "main, proj-a ,"}) == {"main", "proj-a"}


def test_short_room_token_refuses_start():
    with pytest.raises(WeakTokenError, match="ROOM tokens for codex"):
        validate_room_tokens({"ROOM_TOKENS": "codex:short"})


@pytest.mark.asyncio
async def test_room_token_sees_only_room_tools(room_client):
    async with room_client() as c:
        names = await _tool_names(c, ROOM_CODEX)
    assert names == {"room_post", "room_inbox", "room_ack", "room_history",
                     "room_message_get", "room_task_open",
                     "room_task_claim", "room_task_update", "room_task_progress",
                     "room_task_state", "room_task_history", "room_task_wait",
                     "room_task_next", "room_task_release", "room_tasks", "room_task_ask",
                     "room_task_answer_ack", "room_task_questions", "room_task_handoff",
                     "room_task_handoff_accept", "room_task_handoff_decline",
                     "room_task_attest", "room_task_attested_complete",
                     "room_task_cancel", "room_task_inventory", "room_task_image",
                     "room_inbox_wait"}


@pytest.mark.asyncio
async def test_metadata_tool_registered_only_with_activation_flag(room_client, monkeypatch):
    monkeypatch.setenv("ROOM_METADATA_ENABLED", "1")
    async with room_client() as c:
        room_names = await _tool_names(c, ROOM_CODEX)
        office_names = await _tool_names(c, OWNER_TOKEN)
    assert "room_task_metadata_correct" in room_names
    assert "room_task_metadata_correct" not in office_names


@pytest.mark.asyncio
async def test_owner_token_does_not_get_room_tools(room_client):
    async with room_client() as c:
        names = await _tool_names(c, OWNER_TOKEN)
    assert "sql_query" in names and not any(n.startswith("room_") for n in names)


@pytest.mark.asyncio
async def test_room_token_cannot_call_memory_tools(room_client):
    async with room_client() as c:
        r = await c.post("/mcp", json=_rpc(
            "tools/call", {"name": "sql_query", "arguments": {"query": "select 1"}}),
            headers=_auth(ROOM_CODEX))
    assert r.json()["result"]["isError"] is True


@pytest.mark.asyncio
async def test_message_round_trip_between_agents_over_http(room_client):
    async with room_client() as c:
        r = await c.post("/mcp", json=_rpc("tools/call", {
            "name": "room_post", "arguments": {"body": "привет", "to": "codex",
                                               "status": "request"}}),
            headers=_auth(ROOM_CLAUDE))
        assert not r.json()["result"].get("isError")
        r = await c.post("/mcp", json=_rpc(
            "tools/call", {"name": "room_inbox", "arguments": {}}),
            headers=_auth(ROOM_CODEX))
    (msg,) = r.json()["result"]["structuredContent"]["messages"]
    assert (msg["from"], msg["to"], msg["body"]) == ("claude", "codex", "привет")


@pytest.mark.asyncio
async def test_healthz_still_open(room_client):
    async with room_client() as c:
        assert (await c.get("/healthz")).status_code == 200
