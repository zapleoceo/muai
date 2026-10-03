"""MCP-инструменты записи связей: назвать голос в созвоне, добавить прозвище.

Обе правки идут в `mcp_audit` одной транзакцией с изменением и откатываются `undo`.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession
from vera_shared.db.engine import get_session
from vera_shared.journal import audit
from vera_shared.links.nicknames import ACTIVE, NicknameError, put_nickname
from vera_shared.links.scope import WORK, ScopeError, scope_ids_for
from vera_shared.links.speakers import SpeakerError, put_speaker

from vera_mcp.auth import client_of

ScopeKind = Literal["work", "contacts", "chats", "global"]


async def voice_speaker_set(
    event_id: int, label: Annotated[str, Field(min_length=1, max_length=120)],
    entity_id: int, ctx: Context,
) -> dict[str, Any]:
    """Назвать голос в созвоне: ярлык говорящего (например «Собеседник 2», см. event_participants → unresolved_speakers) → сущность. Участник сразу появляется в связях события (source manual). Name an unrecognised speaker of a call; reversible via undo."""
    args = {"event_id": event_id, "label": label, "entity_id": entity_id}
    try:
        async with get_session() as s:
            previous = await put_speaker(s, event_id, label, entity_id)
            audit_id = await audit.record(
                s, client=client_of(ctx), tool="voice_speaker_set", args=args, kind="speaker",
                target_id=event_id, before={"label": label.strip(), "entity_id": previous},
                after={"label": label.strip(), "entity_id": entity_id})
    except SpeakerError as e:
        raise ValueError(str(e)) from e
    return {"ok": True, "audit_id": audit_id, "previous_entity_id": previous}


async def entity_add_nickname(
    entity_id: int, token: Annotated[str, Field(min_length=2, max_length=80)], ctx: Context,
    scope: ScopeKind = WORK, chats: Annotated[list[str] | None, Field(max_length=50)] = None,
    case_sensitive: bool = True,
    project: Annotated[str | None, Field(max_length=40)] = None,
) -> dict[str, Any]:
    """Добавить человеку прозвище или инициалы (например «ДА» = Дмитрий Александрович) с областью: work — рабочие чаты и личка с его сильными контактами, contacts — плюс группы с двумя его контактами, chats — только перечисленные (chats=['telegram:<chat_id>']), global — везде; project сужает work одним проектом (project='itstep': инициалы директора не ловятся в чатах другого бизнеса). Регистрозависимо по умолчанию: «ДА» не совпадёт со словом «да». Упоминания пересчитываются при следующем backfill. Add a scoped nickname; reversible via undo."""
    try:
        scope_ids = scope_ids_for(scope, chats, project)
    except ScopeError as e:
        return {"ok": False, "error": str(e)}
    args = {"entity_id": entity_id, "token": token, "scope": scope, "chats": chats or [],
            "case_sensitive": case_sensitive, "project": project}

    async def op(s: AsyncSession) -> int:
        nickname_id, before, after = await put_nickname(
            s, entity_id, token, scope_kind=scope, scope_ids=scope_ids,
            case_sensitive=case_sensitive, source="owner", status=ACTIVE)
        return await audit.record(s, client=client_of(ctx), tool="entity_add_nickname", args=args,
                                  kind="nickname", target_id=nickname_id, before=before, after=after)

    try:
        async with get_session() as s:
            audit_id = await op(s)
    except NicknameError as e:
        raise ValueError(str(e)) from e
    return {"ok": True, "audit_id": audit_id}


LINK_WRITE_TOOLS = (voice_speaker_set, entity_add_nickname)
