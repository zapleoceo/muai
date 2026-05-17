import logging

from aiohttp import web
from aiogram import Bot

logger = logging.getLogger(__name__)


def make_app(bot: Bot, api_secret: str) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["api_secret"] = api_secret
    app.router.add_get("/health", health)
    app.router.add_get("/chats", chats)
    app.router.add_post("/send", send)
    return app


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def chats(request: web.Request) -> web.Response:
    if not _verify(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    from executor_bot.handlers import get_known_chats
    return web.json_response(get_known_chats())


async def send(request: web.Request) -> web.Response:
    if not _verify(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    data = await request.json()
    bot: Bot = request.app["bot"]
    chat_id = data.get("chat_id")
    text = data.get("text", "")
    reply_to = data.get("reply_to_message_id")
    if not chat_id or not text:
        return web.json_response({"error": "chat_id and text required"}, status=400)
    try:
        sent = await bot.send_message(chat_id, text, reply_to_message_id=reply_to)
        return web.json_response({"ok": True, "message_id": sent.message_id})
    except Exception as exc:
        logger.error("send failed chat=%s: %s", chat_id, exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


def _verify(request: web.Request) -> bool:
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {request.app['api_secret']}"
