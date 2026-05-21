"""Detect MCP auth errors → DM owner with reply-capture for new token.

State: MCPServer.auth_state = 'token_expired'. We DM once per transition
(de-dup via settings.self_extend.token_dm_at[server_name]).
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import MCPServer, Setting

from app.bot.sender import get_bot
from app.config import get_settings

log = logging.getLogger(__name__)

_DM_KEY = "self_extend.token_dm_at"
_RENOTIFY = timedelta(hours=6)


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


async def _was_recently_notified(server_name: str) -> bool:
    async with get_session() as s:
        row = await s.get(Setting, _DM_KEY)
        if not row or not isinstance(row.value, dict):
            return False
        last_iso = row.value.get(server_name)
        if not last_iso:
            return False
        try:
            return datetime.utcnow() - datetime.fromisoformat(last_iso) < _RENOTIFY
        except Exception:
            return False


async def _mark_notified(server_name: str) -> None:
    async with get_session() as s:
        row = await s.get(Setting, _DM_KEY)
        data = dict(row.value) if (row and isinstance(row.value, dict)) else {}
        data[server_name] = datetime.utcnow().isoformat()
        if row is None:
            s.add(Setting(key=_DM_KEY, value=data))
        else:
            row.value = data
        await s.commit()


async def notify_token_expired(server_name: str) -> None:
    if await _was_recently_notified(server_name):
        return
    async with get_session() as s:
        result = await s.execute(select(MCPServer).where(MCPServer.name == server_name))
        row = result.scalar_one_or_none()
    env_keys = list((row.env or {}).keys()) if row else []
    settings = get_settings()
    bot = get_bot()
    text = (
        f"⚠️ <b>Токен протух у MCP <code>{_html_escape(server_name)}</code>.</b>\n\n"
        f"Известные env: <code>{', '.join(env_keys) or '—'}</code>\n\n"
        f"Чтобы обновить — ответь reply'ем на это сообщение:\n"
        f"  <code>#token-{_html_escape(server_name)} KEY value</code>\n\n"
        f"Пример: <code>#token-{_html_escape(server_name)} GITHUB_PERSONAL_ACCESS_TOKEN ghp_xxx…</code>"
    )
    try:
        await bot.send_message(chat_id=settings.owner_telegram_id,
                               text=text, parse_mode="HTML")
        await _mark_notified(server_name)
    except Exception as exc:
        log.warning("token_expired DM failed: %s", exc)


async def apply_token_update(server_name: str, key: str, value: str) -> str:
    async with get_session() as s:
        result = await s.execute(select(MCPServer).where(MCPServer.name == server_name))
        row = result.scalar_one_or_none()
        if row is None:
            return f"⚠️ MCP {server_name} не найден."
        env = dict(row.env or {})
        env[key] = value.strip()
        row.env = env
        row.auth_state = "ok"
        await s.commit()
    try:
        from app.mcp import manager
        await manager._stop(server_name)
        await manager.refresh_from_db()
        return f"✅ Токен <code>{_html_escape(key)}</code> обновлён, сервер перезапущен."
    except Exception as exc:
        log.exception("token update restart failed: %s", exc)
        return f"⚠️ Обновила запись, но рестарт упал: {exc}"


async def find_idle_mcps(days: int = 30) -> list[dict]:
    """List MCPs that haven't been called in N days. Candidates for auto-uninstall."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    async with get_session() as s:
        result = await s.execute(
            select(MCPServer)
            .where(MCPServer.enabled == True)
            .where(MCPServer.installed_by == "self_extend")
        )
        rows = result.scalars().all()
    out = []
    for r in rows:
        if r.last_tool_call_at is None or r.last_tool_call_at < cutoff:
            out.append({
                "name": r.name, "tool_calls": r.tool_calls_count or 0,
                "last_used": r.last_tool_call_at.isoformat() if r.last_tool_call_at else None,
                "installed_at": r.created_at.isoformat() if r.created_at else None,
            })
    return out
