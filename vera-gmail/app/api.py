"""Thin wrappers over Gmail REST API."""
import base64
import logging
from email.mime.text import MIMEText

import httpx

from app.credentials import get_access_token

log = logging.getLogger(__name__)

_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def _client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=30,
        headers={"Authorization": f"Bearer {token}"},
    )


def _decode_b64url(s: str) -> str:
    if not s:
        return ""
    s += "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s.encode()).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _walk_parts(payload: dict) -> tuple[str, str]:
    """Return (text, html) extracted from a Gmail payload tree."""
    if not payload:
        return "", ""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data") or ""
    if mime == "text/plain" and data:
        return _decode_b64url(data), ""
    if mime == "text/html" and data:
        return "", _decode_b64url(data)
    text, html = "", ""
    for p in payload.get("parts") or []:
        t, h = _walk_parts(p)
        text = text or t
        html = html or h
    return text, html


def _headers(payload: dict) -> dict:
    out: dict = {}
    for h in (payload or {}).get("headers", []):
        out[h.get("name", "").lower()] = h.get("value", "")
    return out


async def list_threads(email: str, query: str = "", max_results: int = 20) -> list[dict]:
    token = await get_access_token(email)
    params = {"maxResults": max_results}
    if query:
        params["q"] = query
    async with _client(token) as c:
        r = await c.get(f"{_BASE}/threads", params=params)
    if r.status_code != 200:
        return [{"error": f"gmail {r.status_code}: {r.text[:200]}"}]
    data = r.json()
    threads = []
    for t in data.get("threads") or []:
        threads.append({
            "id": t.get("id"),
            "snippet": t.get("snippet", ""),
            "history_id": t.get("historyId"),
        })
    return threads


async def read_thread(email: str, thread_id: str) -> dict:
    token = await get_access_token(email)
    async with _client(token) as c:
        r = await c.get(f"{_BASE}/threads/{thread_id}", params={"format": "full"})
    if r.status_code != 200:
        return {"error": f"gmail {r.status_code}: {r.text[:200]}"}
    data = r.json()
    messages = []
    for m in data.get("messages") or []:
        hdrs = _headers(m.get("payload") or {})
        text, html = _walk_parts(m.get("payload") or {})
        messages.append({
            "id": m.get("id"),
            "internal_date": m.get("internalDate"),
            "from": hdrs.get("from"),
            "to": hdrs.get("to"),
            "subject": hdrs.get("subject"),
            "date": hdrs.get("date"),
            "snippet": m.get("snippet"),
            "text": text[:6000] or html[:6000],
            "label_ids": m.get("labelIds") or [],
        })
    return {
        "thread_id": data.get("id"),
        "history_id": data.get("historyId"),
        "messages_count": len(messages),
        "messages": messages,
    }


async def send_reply(email: str, thread_id: str, to: str, subject: str,
                     body: str, in_reply_to: str | None = None) -> dict:
    token = await get_access_token(email)
    msg = MIMEText(body, _charset="utf-8")
    msg["To"] = to
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    async with _client(token) as c:
        r = await c.post(
            f"{_BASE}/messages/send",
            json={"raw": raw, "threadId": thread_id},
        )
    if r.status_code not in (200, 201, 202):
        return {"error": f"gmail {r.status_code}: {r.text[:200]}"}
    return {"sent": True, "message_id": r.json().get("id")}


async def modify_thread(email: str, thread_id: str, action: str) -> dict:
    """action ∈ {archive, trash, mark_read, mark_unread, star, unstar}."""
    token = await get_access_token(email)
    if action == "archive":
        payload = {"removeLabelIds": ["INBOX"]}
        endpoint = f"{_BASE}/threads/{thread_id}/modify"
    elif action == "mark_read":
        payload = {"removeLabelIds": ["UNREAD"]}
        endpoint = f"{_BASE}/threads/{thread_id}/modify"
    elif action == "mark_unread":
        payload = {"addLabelIds": ["UNREAD"]}
        endpoint = f"{_BASE}/threads/{thread_id}/modify"
    elif action == "star":
        payload = {"addLabelIds": ["STARRED"]}
        endpoint = f"{_BASE}/threads/{thread_id}/modify"
    elif action == "unstar":
        payload = {"removeLabelIds": ["STARRED"]}
        endpoint = f"{_BASE}/threads/{thread_id}/modify"
    elif action == "trash":
        payload = {}
        endpoint = f"{_BASE}/threads/{thread_id}/trash"
    else:
        return {"error": f"unknown action '{action}'"}

    async with _client(token) as c:
        r = await c.post(endpoint, json=payload)
    if r.status_code not in (200, 201, 202):
        return {"error": f"gmail {r.status_code}: {r.text[:200]}"}
    return {"ok": True, "thread_id": thread_id, "action": action}


async def list_accounts() -> list[str]:
    from app.credentials import list_connected_emails
    return await list_connected_emails()
