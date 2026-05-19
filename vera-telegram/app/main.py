import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

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
    input_text: str
    task_id: int | None = None


class TaskResponse(BaseModel):
    success: bool
    output: str


@app.post("/task", response_model=TaskResponse)
async def receive_task(req: TaskRequest) -> TaskResponse:
    result = await handle_task(req.input_text)
    return TaskResponse(success=result.success, output=result.output)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "agent": "vera-telegram"}
