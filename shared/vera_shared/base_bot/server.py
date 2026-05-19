import time
from dataclasses import asdict

from fastapi import FastAPI

from vera_shared.base_bot.bot import BaseBot
from vera_shared.base_bot.task import Task, TaskResult

_start_time = time.time()


def create_bot_server(bot: BaseBot) -> FastAPI:
    app = FastAPI(title=bot.name)
    _counters: dict[str, int] = {"total": 0, "success": 0, "error": 0}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": bot.agent_id}

    @app.post("/task")
    async def run_task(body: dict) -> dict:
        t_start = time.monotonic()
        task = Task(
            id=body["id"],
            input_text=body["input_text"],
            context=body.get("context", {}),
            capability_needed=body.get("capability_needed", "chat:fast"),
        )
        _counters["total"] += 1
        try:
            result = await bot.handle_task(task)
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            result.duration_ms = elapsed_ms
            if result.success:
                _counters["success"] += 1
            else:
                _counters["error"] += 1
        except Exception as exc:
            _counters["error"] += 1
            result = TaskResult(
                task_id=task.id,
                agent_id=bot.agent_id,
                output="",
                success=False,
                error=str(exc),
                duration_ms=int((time.monotonic() - t_start) * 1000),
            )
        return asdict(result)

    @app.get("/metrics")
    async def metrics() -> dict:
        return {
            "uptime_seconds": int(time.time() - _start_time),
            "tasks": _counters,
            "agent_id": bot.agent_id,
        }

    return app
