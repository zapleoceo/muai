"""FastAPI tools-server inside the userbot container.

Shares the same Telethon client with the live userbot loop, so the agent
can look up dialogs/participants/history in real time without spinning up
a second auth.

Exposed (all POST, JSON body, X-Internal-Secret required):
  /tools/list_dialogs        {q?: str, limit?: int}
  /tools/get_chat_info       {chat_query: str}
  /tools/get_participants    {chat_query: str, limit?: int}
  /tools/get_dialog_history  {chat_query: str, limit?: int}
  /tools/find_user           {q: str}
  /tools/spec                — returns JSON-Schema list of all tools

Not a tool (no spec entry, X-Send-Secret instead of X-Internal-Secret):
  /actions/send_message      {chat_id: str, text: str} — posts AS DIMA to an
                             allow-listed chat; guards in send_guard.py

Only routing lives here: tool logic is in chat_tools.py, media in
media_payload.py, the spec in tools_specs.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from telethon import TelegramClient
from vera_shared.auth import internal_secret_ok

from ingestor_telegram import chat_tools
from ingestor_telegram.media_payload import media_payload
from ingestor_telegram.tools_specs import TOOL_SPECS

_bg_tasks: set[asyncio.Task] = set()   # ссылки — иначе GC может убить задачу

log = logging.getLogger("tg.tools")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")


def _check_secret(x_internal_secret: str | None) -> None:
    if not internal_secret_ok(x_internal_secret, INTERNAL_SECRET):
        raise HTTPException(status_code=401, detail="X-Internal-Secret required")


def build_app(client: TelegramClient) -> FastAPI:
    app = FastAPI(title="ingestor-telegram tools")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "service": "ingestor-telegram-tools"}

    @app.get("/tools/spec")
    async def spec() -> list[dict[str, Any]]:
        return TOOL_SPECS

    @app.post("/tools/list_dialogs")
    async def list_dialogs(
        body: dict[str, Any],
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        _check_secret(x_internal_secret)
        q = (body.get("q") or "").lower()
        return await chat_tools.list_dialogs(client, q, int(body.get("limit") or 50))

    @app.post("/tools/get_chat_info")
    async def get_chat_info(
        body: dict[str, Any],
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        _check_secret(x_internal_secret)
        return await chat_tools.get_chat_info(client, body["chat_query"])

    @app.post("/tools/get_participants")
    async def get_participants(
        body: dict[str, Any],
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        _check_secret(x_internal_secret)
        return await chat_tools.get_participants(
            client, body["chat_query"], int(body.get("limit") or 200))

    @app.post("/tools/get_dialog_history")
    async def get_dialog_history(
        body: dict[str, Any],
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        _check_secret(x_internal_secret)
        return await chat_tools.get_dialog_history(
            client, body["chat_query"], int(body.get("limit") or 30))

    @app.post("/tools/find_user")
    async def find_user(
        body: dict[str, Any],
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        _check_secret(x_internal_secret)
        return await chat_tools.find_user(client, body["q"])

    @app.post("/media/download")
    async def media_download(
        body: dict[str, Any],
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        """Download media bytes for (chat_id, msg_id). Returns base64+mime."""
        _check_secret(x_internal_secret)
        return await media_payload(client, int(body["chat_id"]), int(body["msg_id"]))

    @app.post("/actions/send_message")
    async def send_message(
        body: dict[str, Any],
        x_send_secret: str | None = Header(None, alias="X-Send-Secret"),
    ) -> dict[str, Any]:
        """Post ``text`` to an allow-listed chat AS DIMA (send_guard.py).

        Deliberately outside ``/tools/*`` and absent from TOOL_SPECS: the
        agent must never be able to speak for the owner.
        """
        from ingestor_telegram.send_guard import (
            allowed_chats,
            check_send_request,
            send_to_chat,
        )

        refusal = check_send_request(
            x_send_secret, body.get("chat_id"), body.get("text"),
            send_secret=os.environ.get("TG_SEND_SECRET", ""),
            allowed=allowed_chats(),
        )
        if refusal is not None:
            raise HTTPException(status_code=refusal[0], detail=refusal[1])
        chat_id = int(str(body["chat_id"]).strip())
        try:
            msg_id = await send_to_chat(client, chat_id, body["text"])
        except Exception as e:
            log.warning("send_message to %s failed: %s", chat_id, e)
            raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}") from e
        log.info("send_message: posted msg %s to %s", msg_id, chat_id)
        return {"ok": True, "chat_id": chat_id, "message_id": msg_id}

    @app.post("/tools/sync_project_rosters")
    async def sync_project_rosters(
        x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
    ) -> dict[str, Any]:
        """Молчуны проектных групп → граф (см. roster_sync.py). Троттлится,
        работает в фоне процесса юзербота; повторный вызов при живом прогоне
        не стартует второй."""
        _check_secret(x_internal_secret)
        from ingestor_telegram.roster_sync import run_roster_sync, state
        if state["running"]:
            return {"started": False, "status": "already_running",
                    "last": state["last"]}
        t = asyncio.create_task(run_roster_sync(client))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)
        return {"started": True, "last": state["last"]}

    return app
