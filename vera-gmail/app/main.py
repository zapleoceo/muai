import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.backfill import stream_envelopes
from app.poller import poll_loop
from app.registration import register_loop
from app.tool_handlers import HANDLERS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(register_loop())
    asyncio.create_task(poll_loop())
    yield


app = FastAPI(title="vera-gmail", lifespan=lifespan)


@app.post("/tool/{name}")
async def call_tool(name: str, payload: dict) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        raise HTTPException(404, f"unknown tool {name}")
    try:
        result = await handler(**(payload or {}))
        return {"ok": True, "result": result}
    except TypeError as exc:
        return {"ok": False, "error": f"bad args: {exc}"}
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("Tool %s failed: %s", name, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "agent": "vera-gmail", "tools": list(HANDLERS.keys())}


@app.get("/backfill")
async def backfill(email: str = Query(...), since: str = Query(...)) -> StreamingResponse:
    """NDJSON stream of envelopes for one Gmail account since YYYY-MM-DD.
    Consumed by vera-core sources/gmail.py — keeps the brain in vera-core
    while letting Gmail-specific code stay co-located with the OAuth flow."""
    try:
        since_date = date.fromisoformat(since)
    except ValueError:
        raise HTTPException(400, "since must be YYYY-MM-DD")

    async def gen():
        async for env in stream_envelopes(email, since_date):
            yield (json.dumps(env, ensure_ascii=False) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson")
