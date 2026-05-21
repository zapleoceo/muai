import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from vera_shared.db.engine import get_engine
from vera_shared.db.migrations import run_migrations

from app.poller import poll_loop
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
    asyncio.create_task(register_loop())
    asyncio.create_task(poll_loop())
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
