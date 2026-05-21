"""Thin wrappers over Gmail REST API."""
import asyncio
import base64
import hashlib
import logging
from email.mime.text import MIMEText

import httpx

from vera_shared.media.multimodal import media_to_text

from app.credentials import get_access_token

log = logging.getLogger(__name__)

_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_OCR_INSTRUCTION = (
    "Это вложение из e-mail. Извлеки весь читаемый текст. "
    "Если это таблица — сохрани структуру (имена колонок, строки построчно). "
    "Если рисунок без значимого текста — короткое описание (1 строка). "
    "Не комментируй, не интерпретируй, только содержимое."
)
_OCR_MAX_PER_MESSAGE = 4
_OCR_MAX_PER_THREAD = 6           # total OCR calls across whole thread
_OCR_MAX_BYTES = 8 * 1024 * 1024
_OCR_SEM = asyncio.Semaphore(1)   # serialise OCR globally so we don't burst tokens


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


def _collect_image_parts(payload: dict, out: list | None = None) -> list[tuple]:
    """Walk payload tree, return [(filename, mime_type, attachment_id), ...]
    for image parts that have an attachmentId."""
    if out is None:
        out = []
    if not payload:
        return out
    mime = payload.get("mimeType", "")
    body = payload.get("body") or {}
    att_id = body.get("attachmentId")
    if mime.startswith("image/") and att_id:
        out.append((payload.get("filename") or "image", mime, att_id))
    for p in payload.get("parts") or []:
        _collect_image_parts(p, out)
    return out


async def _download_attachment(token: str, message_id: str, attachment_id: str) -> bytes | None:
    async with _client(token) as c:
        r = await c.get(
            f"{_BASE}/messages/{message_id}/attachments/{attachment_id}"
        )
    if r.status_code != 200:
        return None
    body_data = r.json().get("data") or ""
    body_data += "=" * (-len(body_data) % 4)
    try:
        return base64.urlsafe_b64decode(body_data.encode())
    except Exception:
        return None


async def _ocr_message_attachments(
    token: str, message_id: str, payload: dict,
    *, cache: dict, ocr_budget: list[int],
) -> str:
    """OCR image attachments. cache: {sha256: ocr_text} shared across the
    whole thread to dedupe identical screenshots quoted in many replies.
    ocr_budget is a [remaining] mutable counter so we stop at the thread limit."""
    images = _collect_image_parts(payload)
    if not images:
        return ""
    parts: list[str] = []
    for i, (filename, mime, att_id) in enumerate(images[:_OCR_MAX_PER_MESSAGE]):
        if ocr_budget[0] <= 0:
            log.info("OCR thread budget exhausted, skipping remaining images")
            break
        try:
            data = await _download_attachment(token, message_id, att_id)
        except Exception as exc:
            log.warning("attachment download %s failed: %s", att_id, exc)
            continue
        if not data or len(data) > _OCR_MAX_BYTES:
            continue

        sig = hashlib.sha256(data).hexdigest()
        cached = cache.get(sig)
        if cached is not None:
            if cached:
                parts.append(f"[Картинка {i + 1} «{filename}» — дубликат] {cached}")
            continue

        async with _OCR_SEM:
            try:
                text = await media_to_text(mime, data, _OCR_INSTRUCTION)
            except Exception as exc:
                log.warning("OCR failed for %s: %s", filename, exc)
                cache[sig] = ""
                continue
        ocr_budget[0] -= 1

        if text and not text.startswith("⚠"):
            clean = text.strip()
            cache[sig] = clean
            parts.append(f"[Картинка {i + 1} «{filename}»]\n{clean}")
        else:
            cache[sig] = ""
            log.info("OCR returned warning, skipping: %s", text[:80] if text else "")
    return "\n\n".join(parts)


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


async def read_thread(email: str, thread_id: str, ocr_images: bool = True) -> dict:
    token = await get_access_token(email)
    async with _client(token) as c:
        r = await c.get(f"{_BASE}/threads/{thread_id}", params={"format": "full"})
    if r.status_code != 200:
        return {"error": f"gmail {r.status_code}: {r.text[:200]}"}
    data = r.json()

    ocr_cache: dict[str, str] = {}      # sha256 → OCR text
    ocr_budget = [_OCR_MAX_PER_THREAD]   # mutable counter

    messages = []
    for m in data.get("messages") or []:
        payload = m.get("payload") or {}
        hdrs = _headers(payload)
        text, html = _walk_parts(payload)
        body = text[:6000] or html[:6000]

        ocr_text = ""
        if ocr_images:
            ocr_text = await _ocr_message_attachments(
                token, m.get("id"), payload,
                cache=ocr_cache, ocr_budget=ocr_budget,
            )
            if ocr_text:
                body = (body + "\n\n" + ocr_text)[:14000]

        messages.append({
            "id": m.get("id"),
            "internal_date": m.get("internalDate"),
            "from": hdrs.get("from"),
            "to": hdrs.get("to"),
            "subject": hdrs.get("subject"),
            "date": hdrs.get("date"),
            "snippet": m.get("snippet"),
            "text": body,
            "has_ocr": bool(ocr_text),
            "label_ids": m.get("labelIds") or [],
        })
    return {
        "thread_id": data.get("id"),
        "history_id": data.get("historyId"),
        "messages_count": len(messages),
        "ocr_done": _OCR_MAX_PER_THREAD - ocr_budget[0],
        "ocr_unique": len([v for v in ocr_cache.values() if v]),
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
