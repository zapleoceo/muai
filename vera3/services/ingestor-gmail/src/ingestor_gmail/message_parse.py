"""Письмо Gmail API → спецификация события: текст из MIME, собеседник, шапка."""
from __future__ import annotations

import base64
import logging
from datetime import UTC
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any

from vera_shared.graph.sender_entity import sender_entity
from vera_shared.ingest import AuthorExtractor
from vera_shared.text_chunks import clip_content
from vera_shared.timeutil import utc_naive_now

from ingestor_gmail.html_text import has_diff_markup, is_jira_sender
from ingestor_gmail.html_text import html_to_text as _html_to_text

log = logging.getLogger("gmail")


def _decode_part(payload: dict, message_id: str) -> str | None:
    """Тело MIME-части из base64url; None — часть пустая или не декодируется
    (тогда в журнал уходит id письма, а тело остаётся пустым: событие с
    заголовками лучше потерянного письма)."""
    data = payload.get("body", {}).get("data", "")
    if not data:
        return None
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")
    except ValueError as e:
        log.warning("gmail message %s: часть %s не декодируется: %s",
                    message_id or "?", payload.get("mimeType"), e)
        return None


def _find_diff_html(payload: dict, message_id: str) -> str | None:
    """HTML-часть с diff-разметкой: в text/plain-альтернативе Jira её нет."""
    if payload.get("mimeType") == "text/html":
        raw = _decode_part(payload, message_id)
        return raw if raw and has_diff_markup(raw) else None
    for part in payload.get("parts", []) or []:
        found = _find_diff_html(part, message_id)
        if found:
            return found
    return None


def _extract_text(payload: dict, message_id: str = "", from_: str = "") -> str:
    """Recursive extract plain text from MIME parts."""
    diff_html = _find_diff_html(payload, message_id) if is_jira_sender(from_) else None
    if diff_html:
        return _html_to_text(diff_html, jira=True)
    return _extract_plain_first(payload, message_id)


def _extract_plain_first(payload: dict, message_id: str) -> str:
    if payload.get("mimeType") == "text/plain":
        raw = _decode_part(payload, message_id)
        if raw is not None:
            return raw
    for part in payload.get("parts", []) or []:
        txt = _extract_plain_first(part, message_id)
        if txt:
            return txt
    if payload.get("mimeType") == "text/html":
        raw = _decode_part(payload, message_id)
        if raw is not None:
            return _html_to_text(raw)
    return ""


def correspondent_of(account_email: str, from_: str, to_: str) -> tuple[str, str] | None:
    """(email, display_name) собеседника письма — то, что станет person-сущностью.

    received → From; sent → первый адрес To. Свои ящики пропускаем (иначе
    каждый свой аккаунт превратился бы в отдельного «человека»)."""
    if account_email.lower() in from_.lower():
        pairs = getaddresses([to_]) if to_ else []
        name, addr = pairs[0] if pairs else ("", "")
    else:
        name, addr = parseaddr(from_)
    addr = (addr or "").strip().lower()
    if not addr or "@" not in addr or addr == account_email.lower():
        return None
    return addr, (name or "").strip() or addr.split("@")[0]


def _correspondent_entity_of(account_email: str) -> AuthorExtractor:
    """Экстрактор для `ingest.sync_author_entities`: письмо → сущность автора.

    Служебные ящики (no-reply@, invoice@, crm@) заводятся организациями, а не
    людьми, и организация одна на ДОМЕН, а не на адрес. См. sender_entity.py.
    """
    def extract(spec: dict[str, Any]) -> dict[str, Any] | None:
        meta = spec.get("metadata_") or {}
        who = correspondent_of(account_email, meta.get("from", ""), meta.get("to", ""))
        if who is None:
            return None
        return sender_entity(*who)

    return extract


def _format_event(account_email: str, msg: dict) -> dict[str, Any]:
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    from_ = headers.get("from", "")
    to_ = headers.get("to", "")
    subject = headers.get("subject", "(no subject)")
    date_raw = headers.get("date", "")

    try:
        occurred = parsedate_to_datetime(date_raw)
    except Exception:
        occurred = utc_naive_now()
    if occurred.tzinfo:
        # Convert to UTC FIRST, then strip tz — иначе "11:22 +07:00" станет "11:22"
        # naive вместо корректного "04:22" UTC.
        occurred = occurred.astimezone(UTC).replace(tzinfo=None)

    direction = "sent" if account_email.lower() in from_.lower() else "received"
    author_role = "self" if direction == "sent" else "counterparty"
    author_label = "Я" if author_role == "self" else (from_ or "(unknown)")
    body = clip_content(_extract_text(msg.get("payload", {}), msg.get("id", ""), from_))

    content = (
        f"Author: {author_label} [{author_role}]\n"
        f"From: {from_}\n"
        f"To: {to_}\n"
        f"Subject: {subject}\n"
        f"Date: {date_raw}\n"
        f"Direction: {direction}\n"
        f"---\n{body}"
    )

    return {
        "source": "gmail",
        "source_event_id": msg["id"],
        "account": account_email,
        "category": "email",
        "content_text": content,
        "occurred_at": occurred,
        "metadata_": {
            "direction": direction,
            "author_role": author_role,
            "author_label": author_label,
            "subject": subject,
            "from": from_,
            "to": to_,
        },
    }
