import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

from vera_shared.db.engine import get_engine
from vera_shared.db.migrations import run_migrations

from app.userbot.client import start_client, stop_client
from app.bot import handle_task
from app.registration import register_loop

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
    yield
    await stop_client()


app = FastAPI(title="vera-telegram", lifespan=lifespan)


class TaskRequest(BaseModel):
    request: str
    intent: dict = Field(default_factory=dict)
    task_id: int | None = None


class TaskResponse(BaseModel):
    success: bool
    summary: str
    data: dict | list | None = None
    error: str | None = None


@app.post("/task", response_model=TaskResponse)
async def receive_task(req: TaskRequest) -> TaskResponse:
    result = await handle_task(req.request, req.intent)
    return TaskResponse(
        success=result.success,
        summary=result.summary,
        data=result.data,
        error=result.error,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "agent": "vera-telegram"}
