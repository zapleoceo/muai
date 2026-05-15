from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, text

from app.db.database import AsyncSessionLocal
from app.db.models import Chat, Message
from app.services.answering_types import DynamicFilterOp, DynamicSelectAgg, DynamicToolSpec, PlanChatType, PlanScope
from app.services.plan_executor.links import build_message_link
from app.services.plan_executor.time_range import ResolvedRange


async def tool_sql_dynamic_query(
    *,
    chat_id: int,
    scope: PlanScope,
    chat_types: list[PlanChatType] | None,
    chat_ids: list[int] | None,
    resolved: ResolvedRange | None,
    spec: DynamicToolSpec,
) -> tuple[list[dict], dict]:
    def _field_expr(name: str):
        _aliases = {
            "messages": "message_id", "message_count": "message_id", "count": "message_id",
            "msg_id": "message_id", "id": "message_id",
            "chat_name": "chat_title", "title": "chat_title", "name": "chat_title",
            "username": "chat_username", "type": "chat_type",
        }
        name = _aliases.get(name, name)
        m = {
            "message_id": Message.id,
            "chat_id": Message.chat_id,
            "user_id": Message.user_id,
            "telegram_msg_id": Message.telegram_msg_id,
            "direction": Message.direction,
            "media_type": Message.media_type,
            "date_utc": Message.date_utc,
            "text": Message.text,
            "caption": Message.caption,
            "chat_type": Chat.type,
            "chat_title": Chat.title,
            "chat_username": Chat.username,
            "folder": Chat.folder,
        }
        if name == "text_any":
            return func.coalesce(Message.text, "") + " " + func.coalesce(Message.caption, "")
        return m.get(name)

    if spec.require_time_range and resolved is None:
        return [], {"count": 0, "error": "time_range_required"}

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))

        cols = []
        keys = []
        for s in spec.select:
            expr = _field_expr(s.field)
            if expr is None:
                raise ValueError(f"unsupported_field:{s.field}")
            if s.agg:
                if s.agg == DynamicSelectAgg.COUNT:
                    expr = func.count(expr)
                elif s.agg == DynamicSelectAgg.COUNT_DISTINCT:
                    expr = func.count(expr.distinct())
                elif s.agg == DynamicSelectAgg.MAX:
                    expr = func.max(expr)
                elif s.agg == DynamicSelectAgg.MIN:
                    expr = func.min(expr)
                else:
                    raise ValueError(f"unsupported_agg:{s.agg}")
            key = s.as_name or (f"{s.agg.value.lower()}_{s.field}" if s.agg else s.field)
            cols.append(expr.label(key))
            keys.append(key)

        q = select(*cols).select_from(Message).join(Chat, Chat.id == Message.chat_id)

        where = []
        if resolved is not None:
            where.append(Message.date_utc >= resolved.from_utc)
            where.append(Message.date_utc < resolved.to_utc)
        if chat_types:
            where.append(Chat.type.in_([ct.value for ct in chat_types]))
        if scope == PlanScope.CURRENT_CHAT:
            where.append(Message.chat_id == chat_id)
        elif chat_ids:
            where.append(Message.chat_id.in_(chat_ids))

        for f in spec.filters:
            expr = _field_expr(f.field)
            if expr is None:
                raise ValueError(f"unsupported_field:{f.field}")
            if f.op == DynamicFilterOp.EQ:
                where.append(expr == f.value)
            elif f.op == DynamicFilterOp.ILIKE:
                v = str(f.value or "").strip()
                where.append(expr.ilike(f"%{v}%"))
            elif f.op == DynamicFilterOp.IN:
                if not isinstance(f.value, list):
                    raise ValueError("IN requires list value")
                where.append(expr.in_(f.value))
            elif f.op == DynamicFilterOp.BETWEEN:
                if f.value is None or f.value_to is None:
                    raise ValueError("BETWEEN requires value and value_to")
                where.append(expr >= f.value)
                where.append(expr <= f.value_to)
            elif f.op == DynamicFilterOp.IS_NOT_NULL:
                where.append(expr.isnot(None))
            else:
                raise ValueError(f"unsupported_op:{f.op}")

        if where:
            q = q.where(*where)
        if spec.group_by:
            group_exprs = [_field_expr(g) for g in spec.group_by]
            if any(e is None for e in group_exprs):
                raise ValueError("unsupported field in group_by")
            q = q.group_by(*group_exprs)
        if spec.order_by:
            order_exprs = []
            for o in spec.order_by:
                expr = _field_expr(o.field)
                if expr is None:
                    raise ValueError(f"unsupported_field:{o.field}")
                order_exprs.append(expr.desc() if o.desc else expr.asc())
            q = q.order_by(*order_exprs)

        q = q.limit(int(spec.limit))
        rows = (await session.execute(q)).all()

    items = []
    for r in rows:
        d = dict(r._mapping) if hasattr(r, "_mapping") else {k: v for k, v in zip(keys, r, strict=False)}
        for key in ("date_utc", "last_date_utc"):
            if key in d and isinstance(d[key], datetime):
                d[key] = d[key].isoformat()
        if d.get("telegram_msg_id") and d.get("chat_id"):
            link = build_message_link(
                chat_id=int(d.get("chat_id") or 0),
                chat_type=d.get("chat_type"),
                chat_username=d.get("chat_username"),
                telegram_msg_id=int(d.get("telegram_msg_id") or 0),
            )
            if link:
                d["link"] = link
        items.append(d)

    return items, {
        "count": len(items),
        "limit": int(spec.limit),
        "group_by": spec.group_by,
        "from_utc": resolved.from_utc.isoformat() if resolved else None,
        "to_utc": resolved.to_utc.isoformat() if resolved else None,
    }
