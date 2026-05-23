import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from app.agent import run_task
from app.config import get_settings
from app.rate_limit import check_and_reserve

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_register_loop())
    yield


app = FastAPI(title="vera-coder", lifespan=lifespan)


def _check_secret(secret_header: str | None) -> None:
    cfg = get_settings()
    if not secret_header or secret_header != cfg.internal_secret:
        raise HTTPException(401, "invalid X-Internal-Secret")


class CodeTask(BaseModel):
    description: str
    requested_by: str = "dima"


@app.post("/code-task")
async def code_task(payload: CodeTask,
                     x_internal_secret: str | None = Header(default=None)) -> dict:
    _check_secret(x_internal_secret)
    ok, wait = check_and_reserve()
    if not ok:
        return {"ok": False, "error": f"rate limit, retry in {int(wait)}s"}
    log.info("Starting code-task: %s", payload.description[:120])
    result = await run_task(payload.description, payload.requested_by)
    log.info("code-task done: %s", result)
    # Notify Dima via vera-core
    try:
        cfg = get_settings()
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"{cfg.vera_core_url}/internal/coder/notify",
                json={"task": payload.description, "result": result},
                headers={"X-Internal-Secret": cfg.internal_secret},
            )
    except Exception as exc:
        log.warning("notify failed: %s", exc)
    return result


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "agent": "vera-coder"}


# Register with vera-core so it appears in /api/observability + tool list
async def _register_loop() -> None:
    cfg = get_settings()
    payload = {
        "id": "vera-coder", "name": "Vera Self-Coder",
        "http_url": "http://vera-coder:8005",
        "tools": [{
            "name": "vera_request_code_change",
            "description": (
                "Request a code change to Vera's own project. The "
                "vera-coder agent will spin a fresh worktree, edit files, "
                "run pytest, open a PR, and DM you a link. Rate-limited "
                "to 1 task/hour. Use ONLY when Dima explicitly asks Vera "
                "to fix/add code in herself."
            ),
            "params": [
                {"name": "description", "type": "string",
                 "description": "Clear, single-paragraph code task.", "required": True},
                {"name": "requested_by", "type": "string",
                 "description": "Identifier for audit (default 'dima').",
                 "required": False, "default": "dima"},
            ],
        }],
    }
    for attempt in range(1, 20):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    f"{cfg.vera_core_url}/internal/agents/register",
                    json=payload,
                    headers={"X-Internal-Secret": cfg.internal_secret},
                )
                r.raise_for_status()
            log.info("Registered with vera-core")
            break
        except Exception as exc:
            log.warning("register attempt %d failed: %s", attempt, exc)
            await asyncio.sleep(10)
    while True:
        await asyncio.sleep(30)
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(
                    f"{cfg.vera_core_url}/internal/agents/heartbeat",
                    json={"id": "vera-coder"},
                    headers={"X-Internal-Secret": cfg.internal_secret},
                )
        except Exception as exc:
            log.debug("heartbeat: %s", exc)
