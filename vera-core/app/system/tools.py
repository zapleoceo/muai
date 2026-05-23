"""Self-tools exposed by vera-core through the agent registry.
Lets Vera deploy herself, check her own state, refresh her caches."""
import asyncio
import json
import logging
import os
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

_GH_REPO = "zapleoceo/muai"
_DEFAULT_WORKFLOW = "deploy.yml"


async def _gh_token() -> str | None:
    """GitHub PAT lookup order:
      1. mcp_servers row named 'github' (where the user already entered it)
      2. GITHUB_PERSONAL_ACCESS_TOKEN env
      3. GITHUB_TOKEN env
    Means Dima doesn't have to enter the token twice."""
    try:
        from sqlalchemy import select
        from vera_shared.db.engine import get_session
        from vera_shared.db.models import MCPServer
        async with get_session() as s:
            r = await s.execute(select(MCPServer).where(MCPServer.name == "github"))
            row = r.scalar_one_or_none()
            if row and row.env:
                tok = row.env.get("GITHUB_PERSONAL_ACCESS_TOKEN")
                if tok:
                    return tok
    except Exception as exc:
        log.debug("github MCP token lookup failed: %s", exc)
    return os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or \
           os.environ.get("GITHUB_TOKEN") or None


async def system_deploy(ref: str = "master",
                          message: str | None = None) -> dict:
    """Trigger the Deploy workflow on GitHub. Re-uses the same PAT that
    powers the github MCP. Does NOT touch the repo working tree —
    deploy.sh on the server takes care of pull/build/test/rollback."""
    token = await _gh_token()
    if not token:
        return {"ok": False, "error": "no GITHUB_PERSONAL_ACCESS_TOKEN in env"}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = (f"https://api.github.com/repos/{_GH_REPO}/actions/workflows/"
           f"{_DEFAULT_WORKFLOW}/dispatches")
    payload = {"ref": ref}
    if message:
        payload["inputs"] = {"message": message[:200]}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, headers=headers, json=payload)
    if r.status_code in (200, 201, 202, 204):
        return {"ok": True, "triggered_at": datetime.utcnow().isoformat(),
                "ref": ref, "message": message,
                "actions_url": f"https://github.com/{_GH_REPO}/actions"}
    return {"ok": False,
            "error": f"GitHub API {r.status_code}: {r.text[:200]}"}


async def system_status() -> dict:
    """Return git HEAD + last 5 deploy runs + container statuses."""
    token = await _gh_token()
    out: dict = {"now": datetime.utcnow().isoformat()}
    # Local git HEAD (from mounted /var/www/vera)
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", "/var/www/vera", "log", "-1", "--oneline",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        sout, _ = await proc.communicate()
        out["server_head"] = sout.decode().strip()
    except Exception as exc:
        out["server_head"] = f"err: {exc}"
    if token:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"https://api.github.com/repos/{_GH_REPO}/actions/"
                    f"workflows/{_DEFAULT_WORKFLOW}/runs?per_page=5",
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/vnd.github+json"},
                )
            data = r.json() if r.status_code == 200 else {}
            out["recent_runs"] = [
                {"id": w["id"], "status": w["status"],
                 "conclusion": w.get("conclusion"),
                 "head_branch": w["head_branch"],
                 "head_sha": w["head_sha"][:7],
                 "created_at": w["created_at"]}
                for w in (data.get("workflow_runs") or [])[:5]
            ]
        except Exception as exc:
            out["recent_runs"] = f"err: {exc}"
    return {"ok": True, "result": out}


async def vera_set_pref(key: str, value) -> dict:
    """Toggle a user preference. Lets Vera flip her own behaviour when
    Dima asks her in plain text (e.g. «удаляй карточку после реакции»)."""
    from app.bot import preferences
    if key not in preferences.known_keys():
        return {"ok": False,
                "error": f"unknown pref {key!r}, allowed: {preferences.known_keys()}"}
    # Coerce truthy strings into bool for boolean prefs
    if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no", "да", "нет", "вкл", "выкл"):
        value = value.lower() in ("true", "1", "yes", "да", "вкл")
    await preferences.set(key, value)
    return {"ok": True, "key": key, "value": value, "all": await preferences.get_all()}


async def vera_get_prefs() -> dict:
    from app.bot import preferences
    return {"ok": True, "result": await preferences.get_all()}


# ---------- bot admin tools (forum topic + message management) ----------


def _normalise_chat_id(chat_id: int) -> int:
    """Telegram Bot API requires supergroup ids in the form -100xxxxxxxxxx.
    If we get a bare positive id that looks like a supergroup channel id
    (10+ digits), prefix it. Leave usernames / negative ids untouched."""
    if isinstance(chat_id, int) and chat_id > 0 and len(str(chat_id)) >= 10:
        return int(f"-100{chat_id}")
    return chat_id


async def bot_delete_message(chat_id: int, message_id: int) -> dict:
    chat_id = _normalise_chat_id(chat_id)
    """Delete a specific message in any chat where the bot has rights.
    Works for bot's own messages always; for others needs admin
    can_delete_messages."""
    from app.bot.sender import get_bot
    try:
        await get_bot().delete_message(chat_id, message_id)
        return {"ok": True, "chat_id": chat_id, "message_id": message_id}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def bot_delete_forum_topic(chat_id: int, message_thread_id: int) -> dict:
    """Delete an entire forum topic (and all its messages). Bot must have
    manage_topics + can_delete_messages."""
    chat_id = _normalise_chat_id(chat_id)
    from app.bot.sender import get_bot
    try:
        await get_bot().delete_forum_topic(
            chat_id=chat_id, message_thread_id=message_thread_id)
        return {"ok": True, "chat_id": chat_id,
                "message_thread_id": message_thread_id}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def bot_clear_topic_messages(chat_id: int, message_thread_id: int,
                                    limit: int = 100) -> dict:
    """Sweep recent messages within a forum topic. Best-effort: iterates
    msg ids backwards from latest, deletes those the bot can reach.
    Stops on the first hard failure (typically too-old)."""
    chat_id = _normalise_chat_id(chat_id)
    from app.bot.sender import get_bot
    bot = get_bot()
    # Telegram doesn't expose 'iter messages' to bots; we sweep by id
    # delta from a known anchor we get from chat.last_message.
    # Cheapest fallback: try the last N message_ids relative to a probe.
    # The cleaner route is delete_forum_topic, then re-create the topic.
    deleted = 0
    errors: list[str] = []
    try:
        # Probe last message id by sending+deleting a marker.
        marker = await bot.send_message(
            chat_id=chat_id, text="·",
            message_thread_id=message_thread_id,
        )
        latest = marker.message_id
        await bot.delete_message(chat_id, latest)
        deleted += 1
        for mid in range(latest - 1, max(latest - limit, 0), -1):
            try:
                await bot.delete_message(chat_id, mid)
                deleted += 1
            except Exception as exc:
                errors.append(f"msg {mid}: {type(exc).__name__}")
                if len(errors) > 5:
                    break
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "deleted": deleted, "errors": errors[:3]}


async def bot_wipe_forum(chat_id: int, exclude_general: bool = True) -> dict:
    """High-level: delete EVERY forum topic in this supergroup.
    Calls telegram_list_forum_topics via vera-telegram, then loops
    bot_delete_forum_topic. Single tool call from the LLM loop's POV."""
    import httpx
    from app.config import get_settings
    from app.bot.sender import get_bot
    chat_id = _normalise_chat_id(chat_id)
    # vera-telegram (userbot side) wants the raw int as Telethon expects;
    # negative-100 prefix isn't needed there. Pass both forms.
    raw_id = chat_id if chat_id > 0 else int(str(chat_id).replace("-100", ""))
    cfg = get_settings()
    url = (getattr(cfg, "vera_telegram_url", None)
           or "http://vera-telegram:8001").rstrip("/")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{url}/tool/telegram_list_forum_topics",
                          json={"chat_id": raw_id, "limit": 200},
                          headers={"X-Internal-Secret": cfg.internal_secret})
    if r.status_code != 200:
        return {"ok": False, "error": f"list_topics HTTP {r.status_code}"}
    data = r.json().get("result") or []
    bot = get_bot()
    deleted, errors = [], []
    for t in data:
        tid = t.get("id")
        if not tid:
            continue
        if exclude_general and tid == 1:
            continue  # General topic in supergroups is id=1, can't delete
        try:
            await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=tid)
            deleted.append({"id": tid, "title": t.get("title")})
        except Exception as exc:
            errors.append({"id": tid, "error": f"{type(exc).__name__}: {exc}"})
    return {"ok": True, "deleted_count": len(deleted),
            "deleted": deleted, "errors": errors}


async def bot_close_forum_topic(chat_id: int, message_thread_id: int) -> dict:
    """Lock a forum topic (no new messages, but content stays)."""
    chat_id = _normalise_chat_id(chat_id)
    from app.bot.sender import get_bot
    try:
        await get_bot().close_forum_topic(
            chat_id=chat_id, message_thread_id=message_thread_id)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


HANDLERS = {
    "system_deploy": system_deploy,
    "system_status": system_status,
    "vera_set_pref": vera_set_pref,
    "vera_get_prefs": vera_get_prefs,
    "bot_delete_message": bot_delete_message,
    "bot_delete_forum_topic": bot_delete_forum_topic,
    "bot_close_forum_topic": bot_close_forum_topic,
    "bot_clear_topic_messages": bot_clear_topic_messages,
    "bot_wipe_forum": bot_wipe_forum,
}
