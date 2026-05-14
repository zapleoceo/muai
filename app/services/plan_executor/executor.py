import asyncio

from app.services.answering_types import (
    DynamicToolSpec,
    Plan,
    PlanChatType,
    PlanScope,
    RetrievedContext,
    ToolRun,
)
from app.services.plan_executor.time_range import resolve_time_range
from app.services.plan_executor.tools_rag import tool_rag_search
from app.services.plan_executor.tools_sql import (
    tool_get_recent_dialog,
    tool_sql_active_chats_by_date,
    tool_sql_dynamic_query,
    tool_sql_find_chats,
    tool_sql_lex_search_messages,
    tool_sql_media_messages_by_chat_query,
    tool_sql_message_by_tg_ref,
    tool_sql_messages_by_chat_query_and_date,
    tool_sql_messages_by_date,
    tool_sql_messages_by_folder_and_date,
    tool_sql_recent_messages_by_chat_query,
    tool_sql_search_messages,
    tool_sql_search_messages_by_date,
    tool_sql_stats_by_date,
)

_ALLOWED_TOOLS: dict[str, set[str]] = {
    "INFO_ONLY": {"get_recent_dialog"},
    "RAG_SEMANTIC": {"get_recent_dialog", "rag_search", "sql_search_messages", "sql_find_chats", "sql_lex_search_messages"},
    "SQL_DATE_SUMMARY": {
        "get_recent_dialog", "sql_messages_by_date", "sql_messages_by_chat_query_and_date",
        "sql_messages_by_folder_and_date", "sql_stats_by_date", "sql_active_chats_by_date",
        "sql_dynamic_query", "sql_search_messages", "sql_search_messages_by_date",
        "sql_find_chats", "sql_lex_search_messages", "sql_message_by_tg_ref",
        "sql_recent_messages_by_chat_query", "sql_media_messages_by_chat_query",
    },
    "HYBRID": {
        "get_recent_dialog", "rag_search", "sql_messages_by_date", "sql_messages_by_chat_query_and_date",
        "sql_messages_by_folder_and_date", "sql_stats_by_date", "sql_active_chats_by_date",
        "sql_dynamic_query", "sql_search_messages", "sql_search_messages_by_date",
        "sql_find_chats", "sql_lex_search_messages", "sql_message_by_tg_ref",
        "sql_recent_messages_by_chat_query", "sql_media_messages_by_chat_query",
    },
    "COMMAND": set(),
}


async def execute_plan(
    *,
    plan: Plan,
    chat_id: int,
    query: str,
    timezone_name: str = "UTC",
) -> RetrievedContext:
    ctx = RetrievedContext()
    resolved = resolve_time_range(
        time_range=plan.time_range,
        tz=timezone_name,
        explicit_from=plan.explicit_from,
        explicit_to=plan.explicit_to,
    )
    if resolved:
        ctx.meta["time_range"] = {"from_utc": resolved.from_utc.isoformat(), "to_utc": resolved.to_utc.isoformat()}

    allowed = _ALLOWED_TOOLS.get(plan.strategy.value, set())
    async def _run_tool(tc) -> dict:
        name = tc.name
        if name not in allowed:
            return {"name": name, "ok": False, "tool_meta": {"error": "tool_not_allowed"}}

        try:
            if name == "get_recent_dialog":
                msgs, meta = await tool_get_recent_dialog(chat_id=chat_id, limit=int(tc.args.get("limit", 20)))
                return {"name": name, "ok": True, "tool_meta": meta, "messages": msgs}

            if name == "sql_find_chats":
                chat_types = _coerce_chat_types(tc.args.get("chat_types", plan.chat_types))
                items, meta = await tool_sql_find_chats(
                    query=str(tc.args.get("query") or query),
                    limit=int(tc.args.get("limit", 10)),
                    chat_types=chat_types,
                )
                return {"name": name, "ok": True, "tool_meta": meta, "meta_add": {"chat_candidates": items}}

            if name == "sql_messages_by_date":
                if not resolved:
                    raise ValueError("time_range required")
                msgs, meta = await tool_sql_messages_by_date(
                    chat_id=chat_id,
                    scope=PlanScope(tc.args.get("scope", plan.scope.value)),
                    chat_types=_coerce_chat_types(tc.args.get("chat_types", plan.chat_types)),
                    chat_ids=_coerce_chat_ids(tc.args.get("chat_ids", plan.chat_ids)),
                    resolved=resolved,
                    max_rows=int(tc.args.get("max_rows", 1500)),
                )
                return {"name": name, "ok": True, "tool_meta": meta, "messages": msgs}

            if name == "sql_stats_by_date":
                if not resolved:
                    raise ValueError("time_range required")
                stats, meta = await tool_sql_stats_by_date(
                    chat_id=chat_id,
                    scope=PlanScope(tc.args.get("scope", plan.scope.value)),
                    chat_types=_coerce_chat_types(tc.args.get("chat_types", plan.chat_types)),
                    chat_ids=_coerce_chat_ids(tc.args.get("chat_ids", plan.chat_ids)),
                    resolved=resolved,
                )
                return {"name": name, "ok": True, "tool_meta": meta, "stats": stats}

            if name == "sql_active_chats_by_date":
                if not resolved:
                    raise ValueError("time_range required")
                items, meta = await tool_sql_active_chats_by_date(
                    chat_id=chat_id,
                    scope=PlanScope(tc.args.get("scope", plan.scope.value)),
                    chat_types=_coerce_chat_types(tc.args.get("chat_types", plan.chat_types)),
                    chat_ids=_coerce_chat_ids(tc.args.get("chat_ids", plan.chat_ids)),
                    resolved=resolved,
                    limit=int(tc.args.get("limit", 50)),
                )
                return {"name": name, "ok": True, "tool_meta": meta, "meta_set": {"active_chats": items}}

            if name == "sql_dynamic_query":
                spec = DynamicToolSpec.model_validate(tc.args.get("spec") or {})
                items, meta = await tool_sql_dynamic_query(
                    chat_id=chat_id,
                    scope=PlanScope(tc.args.get("scope", plan.scope.value)),
                    chat_types=_coerce_chat_types(tc.args.get("chat_types", plan.chat_types)),
                    chat_ids=_coerce_chat_ids(tc.args.get("chat_ids", plan.chat_ids)),
                    resolved=resolved,
                    spec=spec,
                )
                return {"name": name, "ok": True, "tool_meta": meta, "meta_add": {"dynamic_rows": items}}

            if name == "rag_search":
                chat_ids = _coerce_chat_ids(tc.args.get("chat_ids", plan.chat_ids))
                chunks, meta = await tool_rag_search(
                    chat_id=chat_id,
                    scope=PlanScope(tc.args.get("scope", plan.scope.value)),
                    chat_ids=chat_ids,
                    query=str(tc.args.get("query") or query),
                    top_k=int(tc.args.get("top_k", 8)),
                )
                return {"name": name, "ok": True, "tool_meta": meta, "chunks": chunks}

            if name == "sql_search_messages":
                msgs, meta = await tool_sql_search_messages(
                    chat_id=chat_id,
                    scope=PlanScope(tc.args.get("scope", plan.scope.value)),
                    chat_types=_coerce_chat_types(tc.args.get("chat_types", plan.chat_types)),
                    chat_ids=_coerce_chat_ids(tc.args.get("chat_ids", plan.chat_ids)),
                    query=str(tc.args.get("query") or query),
                    limit=int(tc.args.get("limit", 30)),
                )
                return {"name": name, "ok": True, "tool_meta": meta, "messages": msgs}

            if name == "sql_search_messages_by_date":
                if not resolved:
                    raise ValueError("time_range required")
                msgs, meta = await tool_sql_search_messages_by_date(
                    chat_id=chat_id,
                    scope=PlanScope(tc.args.get("scope", plan.scope.value)),
                    chat_types=_coerce_chat_types(tc.args.get("chat_types", plan.chat_types)),
                    chat_ids=_coerce_chat_ids(tc.args.get("chat_ids", plan.chat_ids)),
                    resolved=resolved,
                    query=str(tc.args.get("query") or query),
                    limit=int(tc.args.get("limit", 50)),
                )
                return {"name": name, "ok": True, "tool_meta": meta, "messages": msgs}

            if name == "sql_recent_messages_by_chat_query":
                msgs, meta = await tool_sql_recent_messages_by_chat_query(
                    scope=PlanScope(tc.args.get("scope", plan.scope.value)),
                    chat_id=chat_id,
                    chat_query=str(tc.args.get("chat_query") or ""),
                    chat_types=_coerce_chat_types(tc.args.get("chat_types", plan.chat_types)),
                    limit=int(tc.args.get("limit", 5)),
                )
                return {"name": name, "ok": True, "tool_meta": meta, "messages": msgs}

            if name == "sql_media_messages_by_chat_query":
                cq = tc.args.get("chat_query")
                use_time = bool(tc.args.get("use_time_range", False))
                msgs, meta = await tool_sql_media_messages_by_chat_query(
                    scope=PlanScope(tc.args.get("scope", plan.scope.value)),
                    chat_id=chat_id,
                    chat_query=str(cq) if cq is not None else None,
                    chat_types=_coerce_chat_types(tc.args.get("chat_types", plan.chat_types)),
                    media_type=str(tc.args.get("media_type") or ""),
                    limit=int(tc.args.get("limit", 30)),
                    resolved=resolved if (use_time and resolved) else None,
                )
                return {"name": name, "ok": True, "tool_meta": meta, "messages": msgs}

            if name == "sql_lex_search_messages":
                use_time = bool(tc.args.get("use_time_range", False))
                msgs, meta = await tool_sql_lex_search_messages(
                    chat_id=chat_id,
                    scope=PlanScope(tc.args.get("scope", plan.scope.value)),
                    chat_types=_coerce_chat_types(tc.args.get("chat_types", plan.chat_types)),
                    chat_ids=_coerce_chat_ids(tc.args.get("chat_ids", plan.chat_ids)),
                    query=str(tc.args.get("query") or query),
                    limit=int(tc.args.get("limit", 50)),
                    resolved=resolved if (use_time and resolved) else None,
                )
                return {"name": name, "ok": True, "tool_meta": meta, "messages": msgs}

            if name == "sql_message_by_tg_ref":
                chat_id_arg = tc.args.get("chat_id")
                msgs, meta = await tool_sql_message_by_tg_ref(
                    chat_username=str(tc.args.get("chat_username") or "") or None,
                    chat_id=int(chat_id_arg) if chat_id_arg is not None and str(chat_id_arg).strip() else None,
                    telegram_msg_id=int(tc.args.get("telegram_msg_id") or 0),
                )
                return {"name": name, "ok": True, "tool_meta": meta, "messages": msgs}

            if name == "sql_messages_by_chat_query_and_date":
                if not resolved:
                    raise ValueError("time_range required")
                msgs, meta = await tool_sql_messages_by_chat_query_and_date(
                    scope=PlanScope(tc.args.get("scope", plan.scope.value)),
                    chat_id=chat_id,
                    resolved=resolved,
                    chat_query=str(tc.args.get("chat_query") or ""),
                    chat_types=_coerce_chat_types(tc.args.get("chat_types", plan.chat_types)),
                    max_rows=int(tc.args.get("max_rows", 1500)),
                )
                return {"name": name, "ok": True, "tool_meta": meta, "messages": msgs}

            if name == "sql_messages_by_folder_and_date":
                if not resolved:
                    raise ValueError("time_range required")
                msgs, meta = await tool_sql_messages_by_folder_and_date(
                    scope=PlanScope(tc.args.get("scope", plan.scope.value)),
                    chat_id=chat_id,
                    resolved=resolved,
                    folder=str(tc.args.get("folder") or ""),
                    chat_types=_coerce_chat_types(tc.args.get("chat_types", plan.chat_types)),
                    max_rows=int(tc.args.get("max_rows", 1500)),
                )
                return {"name": name, "ok": True, "tool_meta": meta, "messages": msgs}

            return {"name": name, "ok": False, "tool_meta": {"error": "unknown_tool"}}

        except Exception as exc:
            return {"name": name, "ok": False, "tool_meta": {"error": str(exc)[:200]}}

    results = await asyncio.gather(*[_run_tool(tc) for tc in plan.tools])
    _seen_msg_ids: set[int] = set()
    _seen_chunk_ids: set[int] = set()
    for r in results:
        for m in (r.get("messages") or []):
            mid = int(m.get("message_id") or 0)
            if mid and mid in _seen_msg_ids:
                continue
            if mid:
                _seen_msg_ids.add(mid)
            ctx.messages.append(m)
        for c in (r.get("chunks") or []):
            cid = int(c.get("chunk_id") or 0)
            if cid and cid in _seen_chunk_ids:
                continue
            if cid:
                _seen_chunk_ids.add(cid)
            ctx.chunks.append(c)
        if r.get("stats"):
            ctx.stats.update(r["stats"])
        meta_add = r.get("meta_add") or {}
        for k, v in meta_add.items():
            ctx.meta.setdefault(k, []).extend(v or [])
        meta_set = r.get("meta_set") or {}
        for k, v in meta_set.items():
            ctx.meta[k] = v
        ctx.tool_runs.append(ToolRun(name=r.get("name") or "", ok=bool(r.get("ok")), meta=r.get("tool_meta") or {}))

    return ctx


def _coerce_chat_types(raw) -> list[PlanChatType] | None:
    if not raw:
        return None
    return [PlanChatType(x) for x in raw]


def _coerce_chat_ids(raw) -> list[int] | None:
    if not raw:
        return None
    return [int(x) for x in raw]
