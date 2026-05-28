import json
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Request

from vera_shared.db.engine import init_engine
from vera_shared.db.migrations import run_migrations

from app.bot.callbacks import router as callbacks_router
from app.bot.digest_callbacks import router as digest_callbacks_router
from app.bot.proactive_callbacks import router as proactive_callbacks_router
from app.bot.handler import router as bot_router
from app.bot.sender import init_bot
from app.config import get_settings
from app.dashboard.api import router as dashboard_api_router
from app.dashboard.static import router as dashboard_static_router
from app.events.routes import router as events_router
from app.jobs.routes import router as jobs_router
from app.decide.routes import router as decide_router
from app.brain.routes import router as brain_router
from app.brain.observability import router as observability_router
from app.gmail.routes import router as gmail_router
from app.graph.routes import router as graph_router
from app.internal.agents import router as agents_router
from app.internal.llm_proxy import router as llm_proxy_router
from app.internal.coder import router as coder_router
from app.mcp.routes import router as mcp_router
from app.persona.routes import router as persona_router
from app.admin.routes import router as admin_router
from app.sources.routes import router as sources_router
from app.self_extend.routes import router as self_extend_router
from app.research.routes import router as research_router
from app.system.routes import register_self_loop, router as system_router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

_bot: Bot | None = None
_dp: Dispatcher | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Boot sequence — keep BLOCKING path to the absolute minimum so
    FastAPI starts accepting traffic in <2s. Anything that does network
    I/O (Telegram webhook, MCP servers, agent self-registration) goes
    into a background task and the dashboard becomes responsive immediately."""
    global _bot, _dp
    settings = get_settings()

    # BLOCKING (must run before app accepts traffic): DB schema, token
    # encryption. Both are local I/O, fast (<200ms cold).
    engine = await init_engine()
    await run_migrations(engine)
    from vera_shared.tokens.repository import migrate_plaintext_tokens
    migrated = await migrate_plaintext_tokens()
    if migrated:
        log.info("Encrypted %d plaintext tokens at rest", migrated)

    # Bot instance + dispatcher routes must be wired BEFORE we
    # background-set the webhook, since webhook callback → dispatcher.
    _bot = Bot(
        token=settings.telegram_bot_token_vera,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    init_bot(_bot)
    _dp = Dispatcher()
    _dp.include_router(callbacks_router)
    _dp.include_router(digest_callbacks_router)
    _dp.include_router(proactive_callbacks_router)
    _dp.include_router(bot_router)

    # ASYNC (run in background, don't block boot):
    #   - MCP server boot (network: spawn N processes)
    #   - Telegram webhook registration (network: TG API call)
    #   - Self-loop agent registration (network: own HTTP)
    #   - jobs runner + synth (CPU-light loops)
    import asyncio
    asyncio.create_task(_boot_async(settings))

    yield

    try:
        from app.mcp.manager import stop_all
        await stop_all()
    except Exception as exc:
        log.warning("MCP stop_all failed: %s", exc)

    await _bot.delete_webhook()
    await _bot.session.close()
    log.info("Bot shutdown complete")


async def _boot_async(settings) -> None:
    """All slow boot tasks. Errors logged, never crash core."""
    # MCP servers
    try:
        from app.mcp.manager import refresh_from_db
        await refresh_from_db()
    except Exception as exc:
        log.exception("MCP boot failed: %s", exc)
    # Telegram webhook
    try:
        webhook_url = f"{settings.webhook_base_url}/bot/webhook"
        await _bot.set_webhook(
            webhook_url, drop_pending_updates=True,
            allowed_updates=["message", "edited_message", "callback_query",
                             "my_chat_member", "chat_join_request"],
        )
        log.info("Webhook set to %s", webhook_url)
    except Exception as exc:
        log.exception("set_webhook failed: %s", exc)
    # Self-loop registration so vera's own tools surface in collect_tools().
    # register_self_loop() is an infinite loop — spawn, don't await.
    import asyncio as _asyncio
    _asyncio.create_task(register_self_loop())


app = FastAPI(title="vera-core", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """Fast deploy smoke check — always 200 once FastAPI bound the port.
    Does NOT touch DB or external services; tells you the process is up."""
    return {"ok": True}


@app.on_event("startup")
async def _start_bg_loops() -> None:
    """Background workers — spawned AFTER FastAPI starts so binding is
    not blocked. Failures logged but don't crash boot."""
    try:
        from app.jobs.runner import start_all as start_jobs
        start_jobs()
    except Exception as exc:
        log.exception("jobs.runner failed to start: %s", exc)
    try:
        from app.brain.synth import start as start_synth
        start_synth()
    except Exception as exc:
        log.exception("brain.synth failed to start: %s", exc)
    # P1: Pattern miner — autonomous repetition detection from events.
    try:
        from app.brain.pattern_miner import start as start_miner
        start_miner()
    except Exception as exc:
        log.exception("pattern_miner failed to start: %s", exc)
app.include_router(agents_router)
app.include_router(llm_proxy_router)
app.include_router(coder_router)
app.include_router(events_router)
app.include_router(jobs_router)
app.include_router(decide_router)
app.include_router(brain_router)
app.include_router(observability_router)
app.include_router(gmail_router)
app.include_router(graph_router)
app.include_router(mcp_router)
app.include_router(persona_router)
app.include_router(admin_router)
app.include_router(sources_router)
app.include_router(self_extend_router)
app.include_router(research_router)
app.include_router(system_router)
app.include_router(dashboard_api_router)
app.include_router(dashboard_static_router)


@app.post("/bot/webhook")
async def bot_webhook(request: Request) -> dict:
    if _dp is None or _bot is None:
        return {"ok": False}
    body = await request.body()
    update = Update.model_validate(json.loads(body))
    await _dp.feed_update(bot=_bot, update=update)
    return {"ok": True}
