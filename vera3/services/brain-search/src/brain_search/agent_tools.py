"""Инструменты агента: описание, проверка аргументов, исполнение.

Аргументы приходят от LLM, а LLM читает недоверенный текст писем и чатов
(prompt injection), поэтому каждый встроенный инструмент принимает только
свою pydantic-модель с границами: лишние ключи игнорируются, выход за
границы или не тот тип — наблюдение-ошибка, а не TypeError → 500. Любой
сбой исполнения тоже превращается в наблюдение, цикл агента не падает.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.timeutil import utc_naive_now

from brain_search.evidence import PRIMARY_CHARS, SECONDARY_CHARS, evidence_excerpt
from brain_search.pipeline import search_ranked
from brain_search.query_parse import TZ_OFFSET_H
from brain_search.source_links import source_url

log = logging.getLogger(__name__)

TELEGRAM_TOOLS_URL = os.environ.get(
    "TELEGRAM_TOOLS_URL", "http://ingestor-telegram:8000"
)
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")

#: Метка происхождения записи агента. Источник vera_memory от неё не
#: тяжелее первичных событий (query_parse.SOURCE_WEIGHTS), а по метке такие
#: записи можно найти и вычистить, если в них попал текст из инъекции.
AGENT_WRITER = "search_agent"
MAX_SEARCH_LIMIT = 50


@dataclass
class ToolDescriptor:
    name: str
    description: str
    params_schema: dict[str, Any]
    invoker: str  # 'http:telegram' | 'builtin:search_events' | 'builtin:memory'


class SearchEventsArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    q: str = Field(default="", max_length=500)
    source: Literal["telegram", "gmail", "instagram", "vera_chat", "any"] = "any"
    limit: int = Field(default=20, ge=1, le=MAX_SEARCH_LIMIT)
    date_from: str | None = Field(default=None, max_length=32)
    date_to: str | None = Field(default=None, max_length=32)


class RememberArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fact: str = Field(min_length=1, max_length=2000)
    tags: list[Annotated[str, StringConstraints(max_length=40)]] = Field(
        default_factory=list, max_length=10)
    confidence: float = Field(default=0.8, ge=0, le=1)


BUILTIN_SPECS: list[ToolDescriptor] = [
    ToolDescriptor(
        name="search_events",
        description=(
            "Search across ALL events (telegram, gmail, instagram, vera_chat) "
            "with the same ranking as the initial context. Use when you need MORE "
            "messages than the initial context shows. For time-bound questions "
            "(вчера, за неделю, дата) ALWAYS pass date_from/date_to (ISO date, "
            "e.g. 2026-06-09) — an empty q with dates returns the whole period."
        ),
        params_schema={
            "type": "object",
            "properties": {
                "q": {"type": "string"},
                "source": {"type": "string",
                            "enum": ["telegram", "gmail", "instagram", "vera_chat", "any"]},
                "limit": {"type": "integer", "default": 20,
                          "minimum": 1, "maximum": MAX_SEARCH_LIMIT},
                "date_from": {"type": "string",
                               "description": "ISO date inclusive, e.g. 2026-06-09"},
                "date_to": {"type": "string",
                             "description": "ISO date inclusive, e.g. 2026-06-09"},
            },
            "required": ["q"],
        },
        invoker="builtin:search_events",
    ),
    ToolDescriptor(
        name="memory.remember",
        description=(
            "Save a long-lived fact into Vera's own brain (source='vera_memory'). "
            "Use this AFTER deriving a non-obvious truth from tool calls, so future "
            "questions don't repeat the same work. Example: after counting members "
            "of group X, remember the count + date."
        ),
        params_schema={
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "Plain Russian sentence."},
                "tags": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["fact"],
        },
        invoker="builtin:memory",
    ),
]


async def load_remote_tool_specs(url: str) -> list[ToolDescriptor]:
    """Fetch /tools/spec from a remote ingestor and adapt."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{url}/tools/spec")
        if r.status_code >= 400:
            log.warning("remote /tools/spec returned %s", r.status_code)
            return []
        specs = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("failed to fetch %s/tools/spec: %s", url, e)
        return []
    return [
        ToolDescriptor(
            name=s["name"],
            description=s["description"],
            params_schema=s["params_schema"],
            invoker="http:telegram",
        )
        for s in specs
    ]


async def _exec_http_telegram(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    short = tool_name.split(".", 1)[-1]
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{TELEGRAM_TOOLS_URL}/tools/{short}",
                json=params,
                headers={"X-Internal-Secret": INTERNAL_SECRET},
            )
    except httpx.HTTPError as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if r.status_code >= 400:
        return {"error": f"HTTP {r.status_code}", "body": r.text[:300]}
    return r.json()


def parse_iso_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip()[:10], "%Y-%m-%d")  # noqa: DTZ007 - local date before UTC conversion
    except ValueError:
        return None


def date_window(date_from: str | None,
                date_to: str | None) -> tuple[datetime, datetime] | None:
    """Локальные (Jakarta) даты включительно → [start, end) в naive UTC."""
    d_from, d_to = parse_iso_date(date_from), parse_iso_date(date_to)
    if d_from is None and d_to is None:
        return None
    start = (d_from - timedelta(hours=TZ_OFFSET_H)) if d_from else datetime(2000, 1, 1)  # noqa: DTZ001 - UTC-naive DB bound
    end = ((d_to + timedelta(days=1) - timedelta(hours=TZ_OFFSET_H)) if d_to
           else utc_naive_now() + timedelta(days=1))
    return start, end


async def _exec_search_events(args: SearchEventsArgs) -> dict[str, Any]:
    _found, ranked = await search_ranked(
        args.q, limit=args.limit, source=args.source,
        time_range=date_window(args.date_from, args.date_to))
    return {
        "found": len(ranked),
        "events": [
            {"event_id": c.id, "source": c.source,
             "source_url": source_url(c.source, c.source_event_id, c.source_permalink),
             "occurred_at": f"{str(c.occurred_at)[:19]} UTC",
             "author_role": c.author_role,
             "author_label": c.author_label,
             "chat_title": c.chat_title,
             "preview": evidence_excerpt(
                 c.content_text, PRIMARY_CHARS if i < 3 else SECONDARY_CHARS)}
            for i, (_score, c) in enumerate(ranked)
        ],
    }


async def _exec_memory_remember(args: RememberArgs) -> dict[str, Any]:
    now = utc_naive_now()
    async with get_session() as s:
        ev = EventRow(
            source="vera_memory",
            source_event_id=f"memory:{now.timestamp()}",
            account="vera",
            category="fact",
            content_text=args.fact,
            occurred_at=now,
            metadata_={"tags": args.tags, "confidence": args.confidence,
                       "written_by": AGENT_WRITER},
            triage_status="pending",
        )
        s.add(ev)
        await s.flush()
        return {"saved": True, "event_id": ev.id}


async def _dispatch(tool: ToolDescriptor, params: dict[str, Any]) -> dict[str, Any]:
    if tool.invoker == "http:telegram":
        return await _exec_http_telegram(tool.name, params)
    if tool.invoker == "builtin:search_events":
        return await _exec_search_events(SearchEventsArgs.model_validate(params))
    if tool.invoker == "builtin:memory":
        return await _exec_memory_remember(RememberArgs.model_validate(params))
    return {"error": f"no invoker for {tool.invoker}"}


async def execute_tool(tool: ToolDescriptor, params: Any) -> dict[str, Any]:
    """Всегда возвращает наблюдение: ошибки аргументов и исполнения — тоже."""
    if not isinstance(params, dict):
        return {"error": "params must be a JSON object"}
    try:
        return await _dispatch(tool, params)
    except ValidationError as e:
        return {"error": "invalid params",
                "details": e.errors(include_url=False, include_input=False,
                                    include_context=False)[:5]}
    except Exception as e:  # граница: падение инструмента не должно ронять /search
        log.exception("agent tool %s failed", tool.name)
        return {"error": f"{type(e).__name__}: {e}"[:300]}
