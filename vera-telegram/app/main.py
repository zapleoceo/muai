import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

from vera_shared.db.engine import get_engine
from vera_shared.db.migrations import run_migrations

from app.backfill import stream_envelopes
from app.poller import poll_loop
from app.dialog_cache import refresh_loop as dialog_refresh_loop
from app.push_handler import start_push_listener
from app.registration import register_loop
from app.tool_handlers import HANDLERS
from app.userbot.client import start_client, stop_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.environ.setdefault("DB_PATH", "/data/vera.db")
    await run_migrations(get_engine())
    await start_client()
    # Push handler — real-time, covers ALL chats incl. sent.
    await start_push_listener()
    asyncio.create_task(register_loop())
    # Poll loop kept as safety net (catches anything missed during outages).
    asyncio.create_task(poll_loop())
    # Dialog cache — periodic refresh; search_dialogs hits SQLite first.
    asyncio.create_task(dialog_refresh_loop())
    yield
    await stop_client()


app = FastAPI(title="vera-telegram", lifespan=lifespan)


@app.post("/tool/{name}")
async def call_tool(name: str, payload: dict) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        raise HTTPException(404, f"unknown tool {name}")
    try:
        result = await handler(**(payload or {}))
        return {"ok": True, "result": result}
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    except TypeError as exc:
        return {"ok": False, "error": f"bad args: {exc}"}
    except Exception as exc:
        logger.exception("Tool %s failed: %s", name, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "agent": "vera-telegram", "tools": list(HANDLERS.keys())}


@app.get("/backfill")
async def backfill(source: str = Query(...), since: str = Query(...)) -> StreamingResponse:
    """NDJSON stream of envelopes for one Telegram source row since YYYY-MM-DD."""
    try:
        since_date = date.fromisoformat(since)
    except ValueError:
        raise HTTPException(400, "since must be YYYY-MM-DD")

    async def gen():
        async for env in stream_envelopes(source, since_date):
            yield (json.dumps(env, ensure_ascii=False, default=str) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson")
