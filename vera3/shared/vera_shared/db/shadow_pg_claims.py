"""PostgreSQL claim generation comparison and insertion."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vera_shared.ingest.shadow_types import Claim, Quarantine


def _as_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _optional_dt(value: str | None) -> datetime | None:
    return _as_dt(value) if value is not None else None


async def _check_generation_claims(
    conn: AsyncConnection,
    generation: int,
    claims: list[Claim],
    valid_from: list[str],
    valid_to: list[str | None],
) -> None:
    stored = (
        await conn.execute(
            text(
                "SELECT subject,predicate,value,valid_from,valid_to,evidence_anchor,"
                "evidence_kind FROM brain_claims WHERE generation_id=:id ORDER BY ordinal"
            ),
            {"id": generation},
        )
    ).all()
    expected = [
        (
            c.subject,
            c.predicate,
            c.value,
            _as_dt(valid_from[i]),
            _optional_dt(valid_to[i]),
            c.evidence_anchor,
            c.evidence_kind,
        )
        for i, c in enumerate(claims)
    ]
    if [tuple(row) for row in stored] != expected:
        raise Quarantine("extraction version changed")


async def _insert_generation_claims(
    conn: AsyncConnection,
    generation: int,
    claims: list[Claim],
    valid_from: list[str],
    valid_to: list[str | None],
) -> None:
    if not claims:
        return
    await conn.execute(
        text(
            "INSERT INTO brain_claims(generation_id,ordinal,subject,predicate,value,"
            "valid_from,valid_to,evidence_anchor,evidence_kind) "
            "VALUES (:id,:ordinal,:subject,:predicate,:value,:valid_from,:valid_to,"
            ":anchor,:kind)"
        ),
        [
            {
                "id": generation,
                "ordinal": i,
                "subject": c.subject,
                "predicate": c.predicate,
                "value": c.value,
                "valid_from": _as_dt(valid_from[i]),
                "valid_to": _optional_dt(valid_to[i]),
                "anchor": c.evidence_anchor,
                "kind": c.evidence_kind,
            }
            for i, c in enumerate(claims)
        ],
    )
