"""Прозвища человека с областью действия (миграция 043): чтение, запись, решение владельца.

Чтение терпит отсутствие таблицы (код деплоится раньше миграции): прозвищ нет.
Предложения кода (`status='suggested'`) сами не применяются — только после решения
владельца; отвергнутые не переспрашиваются.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from vera_shared.db.engine import get_session
from vera_shared.db.models_links import EntityNicknameRow
from vera_shared.links.scope import SCOPE_KINDS, WORK, NicknameRule
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)

ACTIVE, SUGGESTED, REJECTED = "active", "suggested", "rejected"
MAX_TOKEN_CHARS = 80
MIN_TOKEN_CHARS = 2


class NicknameError(ValueError):
    """Прозвище не принято: пустое, слишком длинное или с неизвестной областью."""


def _rule(row: EntityNicknameRow) -> NicknameRule:
    return NicknameRule(row.entity_id, row.token, row.case_sensitive, row.scope_kind,
                        tuple(str(i) for i in row.scope_ids or ()))


def _clean(token: str, scope_kind: str) -> str:
    token = " ".join(token.split())
    if not MIN_TOKEN_CHARS <= len(token) <= MAX_TOKEN_CHARS:
        raise NicknameError(f"nickname must be {MIN_TOKEN_CHARS}..{MAX_TOKEN_CHARS} chars")
    if scope_kind not in SCOPE_KINDS:
        raise NicknameError(f"scope_kind must be one of {', '.join(SCOPE_KINDS)}")
    return token


async def active_rules(entity_id: int | None = None) -> list[NicknameRule]:
    """Действующие прозвища (всех людей или одного)."""
    query = select(EntityNicknameRow).where(EntityNicknameRow.status == ACTIVE)
    if entity_id is not None:
        query = query.where(EntityNicknameRow.entity_id == entity_id)
    async with get_session() as s:
        try:
            rows = (await s.execute(query)).scalars().all()
        except DBAPIError as e:
            log.warning("entity_nicknames не прочитана (миграция 043?): %s", e)
            return []
    return [_rule(r) for r in rows]


def snapshot(row: EntityNicknameRow) -> dict[str, Any]:
    return {"entity_id": row.entity_id, "token": row.token, "scope_kind": row.scope_kind,
            "scope_ids": list(row.scope_ids or []), "case_sensitive": row.case_sensitive,
            "status": row.status, "source": row.source}


async def put_nickname(s: AsyncSession, entity_id: int, token: str, *, scope_kind: str = WORK,
                       scope_ids: list[str] | None = None, case_sensitive: bool = True,
                       source: str = "owner", status: str = ACTIVE,
                       reason: str = "") -> tuple[int, dict[str, Any] | None, dict[str, Any]]:
    """Записать прозвище в сессии вызывающего; повтор того же токена обновляет область и
    статус. → (id строки, снимок до, снимок после) — для журнала."""
    token = _clean(token, scope_kind)
    row = (await s.execute(select(EntityNicknameRow).where(
        EntityNicknameRow.entity_id == entity_id,
        EntityNicknameRow.token == token))).scalar_one_or_none()
    before = snapshot(row) if row is not None else None
    if row is None:
        row = EntityNicknameRow(entity_id=entity_id, token=token)
        s.add(row)
    row.scope_kind, row.scope_ids = scope_kind, [str(i) for i in scope_ids or []]
    row.case_sensitive, row.source, row.status, row.reason = case_sensitive, source, status, reason
    row.decided_at = utc_naive_now() if status != SUGGESTED else None
    await s.flush()
    return row.id, before, snapshot(row)


async def restore_nickname(s: AsyncSession, nickname_id: int, before: dict[str, Any] | None) -> None:
    """Откат `put_nickname`: строки до не было — удалить, иначе вернуть прежние поля."""
    row = await s.get(EntityNicknameRow, nickname_id)
    if row is None:
        return
    if before is None:
        await s.delete(row)
        return
    row.scope_kind, row.scope_ids = before["scope_kind"], before["scope_ids"]
    row.case_sensitive, row.status, row.source = before["case_sensitive"], before["status"], before["source"]


async def add_nickname(entity_id: int, token: str, **kwargs: Any) -> int:
    """`put_nickname` в собственной сессии (скрипты). → id строки."""
    async with get_session() as s:
        return (await put_nickname(s, entity_id, token, **kwargs))[0]


async def suggest_nickname(entity_id: int, token: str, reason: str,
                           scope_kind: str = WORK) -> bool:
    """Предложить владельцу. False — токен этого человека уже известен (любой статус)."""
    token = _clean(token, scope_kind)
    async with get_session() as s:
        known = (await s.execute(select(EntityNicknameRow.id).where(
            EntityNicknameRow.entity_id == entity_id,
            EntityNicknameRow.token == token))).first()
        if known:
            return False
        s.add(EntityNicknameRow(entity_id=entity_id, token=token, scope_kind=scope_kind,
                                status=SUGGESTED, source="auto", reason=reason))
    return True


async def pending_suggestions() -> list[dict[str, Any]]:
    async with get_session() as s:
        try:
            rows = (await s.execute(text(
                "SELECT n.id, n.entity_id, e.name, n.token, n.scope_kind, n.reason "
                "FROM entity_nicknames n JOIN entities e ON e.id = n.entity_id "
                "WHERE n.status = 'suggested' ORDER BY n.id"))).mappings().all()
        except DBAPIError as e:
            log.warning("entity_nicknames не прочитана (миграция 043?): %s", e)
            return []
    return [dict(r) for r in rows]


async def decide_suggestion(nickname_id: int, approve: bool) -> bool:
    """Решение владельца по предложению. False — такой строки нет или решение уже принято."""
    async with get_session() as s:
        row = await s.get(EntityNicknameRow, nickname_id)
        if row is None or row.status != SUGGESTED:
            return False
        row.status = ACTIVE if approve else REJECTED
        row.decided_at = utc_naive_now()
    return True
