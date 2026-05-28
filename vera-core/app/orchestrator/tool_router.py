import json
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Agent

log = logging.getLogger(__name__)
_TIMEOUT = 60.0
_STALE_AFTER = timedelta(minutes=5)


async def collect_tools() -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """Return (tool_specs, tool_name → (kind, target)).
    kind = 'http' → target is bot http_url
    kind = 'mcp'  → target is MCP server name
    HTTP tools (fresh agents) come first; MCP tools augment them.
    On name collisions, HTTP wins (so manual adapter overrides MCP)."""
    fresh_after = datetime.utcnow() - _STALE_AFTER
    async with get_session() as session:
        result = await session.execute(
            select(Agent).where(
                Agent.status == "online",
                Agent.last_heartbeat.isnot(None),
                Agent.last_heartbeat >= fresh_after,
            )
        )
        agents = result.scalars().all()

    specs: list[dict] = []
    route: dict[str, tuple[str, str]] = {}
    seen: set[str] = set()
    for a in agents:
        for t in (a.tools or []):
            n = t["name"]
            if n in seen:
                continue
            seen.add(n)
            specs.append(t)
            route[n] = ("http", a.http_url)

    # MCP
    try:
        from app.mcp.manager import get_routed_tools
        mcp_routed = await get_routed_tools()
        for tool_name, (server_name, spec) in mcp_routed.items():
            if tool_name in seen:
                continue
            seen.add(tool_name)
            specs.append(spec)
            route[tool_name] = ("mcp", server_name)
    except Exception as exc:
        log.warning("collect MCP tools failed: %s", exc)
    return specs, route


async def call_tool(route: dict[str, tuple[str, str]], name: str, args: dict) -> dict:
    target = route.get(name)
    if target is None:
        return {"ok": False, "error": f"unknown tool '{name}'"}
    try:
        args = await _resolve_safe_args(name, args)
    except _ResolveError as exc:
        return {"ok": False, "error": f"arg resolution: {exc}"}
    kind, dest = target
    if kind == "mcp":
        from app.mcp.manager import call_tool as mcp_call
        return await mcp_call(dest, name, args)
    # http
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{dest}/tool/{name}", json=args)
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        return {"ok": False, "error": f"timeout after {_TIMEOUT}s"}
    except Exception as exc:
        log.warning("Tool %s failed: %s", name, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


class _ResolveError(Exception):
    pass


# Tools that touch the outside world. LLM args for these are tainted:
# we either (a) re-derive critical args from authoritative state, or
# (b) refuse to execute without explicit user-context binding.
DESTRUCTIVE_TOOLS: set[str] = {
    "gmail_send_reply", "gmail_send_message",
    "gmail_modify_thread", "gmail_modify_threads", "gmail_apply_label",
    "telegram_send_message", "telegram_send_reaction",
    "telegram_send_reply", "telegram_forward_message",
    "telegram_delete_messages", "telegram_edit_message",
    "telegram_pin_message", "telegram_unpin_message",
    "ig_reply_to_comment", "ig_send_dm", "ig_delete_comment",
    "ig_hide_comment", "fb_reply_to_comment", "fb_send_dm",
}

# Tools safe to run automatically without user confirmation (read-only +
# locally-idempotent state changes on Dima's own data). Anything not in
# this set requires explicit user click — never auto-execute from triage.
AUTO_SAFE_TOOLS: set[str] = {
    "gmail_modify_thread", "gmail_modify_threads", "gmail_apply_label",
    "gmail_add_label", "gmail_remove_label",
    "telegram_read_messages", "telegram_mark_read",
    "telegram_search_dialogs", "telegram_list_recent_dialogs",
    "telegram_get_dialog_info",
    "gmail_list_threads", "gmail_read_thread", "gmail_list_accounts",
    "fetch", "git_status", "git_log", "git_diff",
}

# Per-tool reversibility (0 = irreversible, 1 = fully safe to undo).
# Used by decide.scoring to compute alignment score without string heuristics.
_REVERSIBILITY: dict[str, float] = {
    "gmail_modify_thread":          0.9,
    "gmail_modify_threads":         0.9,
    "gmail_add_label":              0.9,
    "gmail_apply_label":            0.9,
    "gmail_remove_label":           0.9,
    "gmail_archive":                0.9,
    "telegram_mark_read":           1.0,
    "telegram_read_messages":       1.0,
    "telegram_search_dialogs":      1.0,
    "telegram_list_recent_dialogs": 1.0,
    "telegram_get_dialog_info":     1.0,
    "gmail_list_threads":           1.0,
    "gmail_read_thread":            1.0,
    "gmail_list_accounts":          1.0,
    "fetch":                        1.0,
    "git_status":                   1.0,
    "git_log":                      1.0,
    "git_diff":                     1.0,
    "gmail_send_reply":             0.1,
    "gmail_send_message":           0.05,
    "telegram_send_message":        0.05,
    "telegram_send_reply":          0.1,
    "telegram_forward_message":     0.2,
    "telegram_edit_message":        0.3,
    "telegram_delete_messages":     0.0,
    "telegram_pin_message":         0.5,
    "telegram_unpin_message":       0.6,
    "ig_reply_to_comment":          0.15,
    "ig_send_dm":                   0.05,
    "ig_delete_comment":            0.0,
    "fb_reply_to_comment":          0.15,
    "fb_send_dm":                   0.05,
    "system_deploy":                0.3,
    "bot_delete_message":           0.0,
    "bot_delete_forum_topic":       0.0,
    "bot_wipe_forum":               0.0,
}

_REVERSIBILITY_BY_PATTERN: list[tuple[str, float]] = [
    ("delete",    0.0),
    ("permanent", 0.0),
    ("wipe",      0.0),
    ("send",      0.1),
    ("reply",     0.15),
    ("post",      0.1),
    ("forward",   0.2),
    ("archive",   0.9),
    ("label",     0.9),
    ("mark",      0.9),
    ("modify",    0.8),
    ("read",      1.0),
    ("list",      1.0),
    ("search",    1.0),
    ("fetch",     1.0),
    ("status",    1.0),
    ("log",       1.0),
    ("diff",      1.0),
]


def is_auto_safe(tool_name: str) -> bool:
    return tool_name in AUTO_SAFE_TOOLS


def tool_reversibility(tool_name: str | None) -> float:
    """Intrinsic reversibility of a tool (0 = irreversible, 1 = fully safe).
    Single source of truth — imported by decide.scoring."""
    if tool_name is None:
        return 0.5
    if tool_name in _REVERSIBILITY:
        return _REVERSIBILITY[tool_name]
    t = tool_name.lower()
    for pattern, score in _REVERSIBILITY_BY_PATTERN:
        if pattern in t:
            return score
    return 0.5


def _email_from_field(raw: str) -> str | None:
    import re as _re
    raw = (raw or "").strip()
    if not raw:
        return None
    m = _re.search(r"<([^>]+)>", raw)
    if m:
        return m.group(1).strip()
    m = _re.search(r"\b[\w.+-]+@[\w.-]+\.\w+\b", raw)
    return m.group(0).strip() if m else None


async def _resolve_safe_args(name: str, args: dict) -> dict:
    """For every destructive tool, re-derive trusted args from server-side
    state. LLM gets only soft inputs (subject, body, label); identifiers
    (to, chat_id, peer, thread_id) come from event/thread metadata."""
    if name == "gmail_send_reply":
        email = args.get("email")
        thread_id = args.get("thread_id")
        if not email or not thread_id:
            raise _ResolveError("gmail_send_reply requires email + thread_id")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            resp = await c.post(
                "http://vera-gmail:8004/tool/gmail_read_thread",
                json={"email": str(email), "thread_id": str(thread_id),
                      "ocr_images": False},
            )
        try:
            resp.raise_for_status()
        except Exception as exc:
            raise _ResolveError(f"thread read failed: {exc}")
        data = resp.json().get("result") or {}
        messages = data.get("messages") or []
        inbound = [m for m in messages if (m.get("from") or "").strip()]
        if not inbound:
            raise _ResolveError("no inbound messages in thread")
        authoritative_to = _email_from_field(inbound[-1].get("from") or "")
        if not authoritative_to:
            raise _ResolveError(
                f"could not parse 'to' from sender {inbound[-1].get('from')!r}")
        if args.get("to") and args["to"].lower() != authoritative_to.lower():
            log.warning("gmail_send_reply override to=%r with %r",
                        args.get("to"), authoritative_to)
        args = dict(args)
        args["to"] = authoritative_to
        return args

    if name in ("telegram_send_message", "telegram_send_reaction",
                "telegram_send_reply"):
        # peer or chat_id must be explicitly named; we cannot infer.
        peer = args.get("peer") or args.get("chat_id")
        if not peer or (isinstance(peer, str) and not peer.strip()):
            raise _ResolveError(
                f"{name} requires peer/chat_id explicitly. "
                "Не выбираю чат сам — назови явно или жми «Свой ответ» на нужной карточке.")
        # Strip any LLM-supplied 'from' override (Telethon doesn't use it,
        # but we don't want LLM injecting authorship metadata).
        args = {k: v for k, v in args.items() if k != "from"}
        return args

    return args


def format_tools_for_prompt(specs: list[dict]) -> str:
    if not specs:
        return "(no tools available)"
    lines = []
    for s in specs:
        params = ", ".join(
            f"{p['name']}: {p['type']}" + (" (optional)" if not p.get('required', True) else "")
            for p in s.get("params", [])
        )
        lines.append(f"- {s['name']}({params})\n    {s['description']}")
    return "\n".join(lines)


def truncate_for_llm(obj: Any, max_chars: int = 16000) -> str:
    """16k chars — баланс между «не теряем контекст» и «не жрём весь
    бюджет». Для большого агрегирования (саммари по 20+ чатам) используй
    purpose-built tool типа telegram_folder_digest, который map-reduce-ит
    результат на стороне сервиса и возвращает уже сжатое."""
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"\n…(truncated, total {len(s)} chars)"
