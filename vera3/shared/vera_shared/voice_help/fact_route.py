"""Separate public current facts from personal recall. No network or credentials."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

_PERSONAL = re.compile(r"\b(помни\w*|вспомни\w*|обсуждал\w*|говорил\w*|покупал\w*|менял\w*|remember|recall)\b", re.I)
_PERSONAL_ACTION = re.compile(r"\b(я|мы|I|we)\b.{0,60}\b(менял\w*|покупал\w*|обсуждал\w*|говорил\w*|bought|paid|said)\b", re.I)
_CURRENT = re.compile(r"\b(сейчас|текущ\w*|актуаль\w*|сегодня|today|current|now|latest)\b", re.I)
_CURRENCY = re.compile(r"(курс|стоит|стоимост|цена|rate|price).*(доллар|долар|грив|usd|uah)|(доллар|долар|грив|usd|uah).*(курс|стоит|стоимост|цена|rate|price)|(доллар|usd).*(грив|uah)", re.I | re.S)
_PUBLIC = re.compile(r"\b(погод\w*|новост\w*|weather|news)\b", re.I)
PREFIX = "FRESH_FACTS_V1\n"


def external_facts(query: str) -> bool:
    personal = _PERSONAL_ACTION.search(query) or (_PERSONAL.search(query) and not _CURRENT.search(query))
    return not personal and bool(_CURRENCY.search(query) or _PUBLIC.search(query))


def fresh_facts_action(query: str, now: datetime | None) -> str:
    if not external_facts(query):
        return ""
    at = "not supplied; establish actual current time before lookup"
    if now is not None:
        at = now.replace(tzinfo=UTC).isoformat() if now.tzinfo is None else now.isoformat()
    return (
        f"\nCurrent external facts; recorded request reference time {at} (UTC; owner timezone unknown). "
        "Do not use personal memory or old events as current data. Use only already authorized "
        "public sources, no new access or paid calls. For USD/UAH specify official NBU vs "
        "cash/buy/sell rate; do not substitute one for another. "
        "Final note contract: FRESH_FACTS_V1 then newline then JSON with answer, available "
        "(boolean), checked_at (timezone ISO), as_of (timezone ISO), source_url (https), "
        "rate_type. checked_at and data as_of must be within 30 minutes. Exception: rate_type exactly official NBU with source_url host exactly bank.gov.ua allows data as_of within 24 hours. "
        "For unavailable sources set available=false and explicitly say no current value "
        "could be verified; omit as_of/source_url/rate_type. Never refresh an old observation."
    )


def _time(raw: object) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("missing time")
    at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("timezone required")
    return at.astimezone(UTC)


def fresh_result(text: str, now: datetime) -> str | None:
    """Validate executor-attested observation; this is not an independent source fetch."""
    if not text.startswith(PREFIX):
        return None
    try:
        data = json.loads(text[len(PREFIX):])
        at = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        checked = _time(data["checked_at"])
        if not timedelta(0) <= at - checked <= timedelta(minutes=30):
            return None
        body = data["answer"]
        if not isinstance(body, str) or not body.strip() or len(body) > 3000:
            return None
        if data.get("available") is False:
            return f"Current external data unavailable (checked {checked.isoformat()}). {body}"
        if data.get("available") is not True:
            return None
        as_of = _time(data["as_of"])
        url = urlsplit(data["source_url"])
        kind = data["rate_type"]
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            return None
        if not isinstance(kind, str) or not kind.strip() or len(kind) > 80:
            return None
        official = kind.strip().casefold() == "official nbu" and url.hostname == "bank.gov.ua"
        max_age = timedelta(hours=24) if official else timedelta(minutes=30)
        if as_of > checked or not timedelta(0) <= at - as_of <= max_age:
            return None
        return (f"{body}\nSource: {url.geturl()}\nType: {kind}; "
                f"as of {as_of.isoformat()}; checked {checked.isoformat()}")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
