import logging
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import select

from vera_shared.crypto import decrypt, encrypt
from vera_shared.db.engine import get_session
from vera_shared.db.models import GmailAccount

from app.config import get_settings

log = logging.getLogger(__name__)


def _master() -> str:
    return get_settings().session_secret


async def save_account(
    *, email: str, refresh_token: str,
    access_token: str | None = None, access_expiry: datetime | None = None,
) -> GmailAccount:
    master = _master()
    refresh_enc = encrypt(refresh_token, master)
    access_enc = encrypt(access_token, master) if access_token else None
    async with get_session() as session:
        result = await session.execute(
            select(GmailAccount).where(GmailAccount.email == email)
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = GmailAccount(
                email=email, refresh_token_enc=refresh_enc,
                access_token_enc=access_enc, access_expiry=access_expiry,
                is_active=True,
            )
            session.add(row)
        else:
            row.refresh_token_enc = refresh_enc
            if access_enc is not None:
                row.access_token_enc = access_enc
                row.access_expiry = access_expiry
            row.is_active = True
            row.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(row)
        return row


async def update_tokens(email: str, access_token: str, expiry: datetime) -> None:
    master = _master()
    async with get_session() as session:
        result = await session.execute(
            select(GmailAccount).where(GmailAccount.email == email)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.access_token_enc = encrypt(access_token, master)
        row.access_expiry = expiry
        row.updated_at = datetime.utcnow()
        await session.commit()


async def update_poll_state(email: str, history_id: str | None) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(GmailAccount).where(GmailAccount.email == email)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return
        if history_id:
            row.history_id = history_id
        row.last_polled_at = datetime.utcnow()
        await session.commit()


async def get_account(email: str) -> dict | None:
    master = _master()
    async with get_session() as session:
        result = await session.execute(
            select(GmailAccount).where(GmailAccount.email == email)
        )
        row = result.scalar_one_or_none()
    if row is None:
        return None
    return _to_dict(row, master)


async def list_accounts() -> list[dict]:
    master = _master()
    async with get_session() as session:
        result = await session.execute(select(GmailAccount))
        rows = result.scalars().all()
    return [_to_dict(r, master) for r in rows]


async def deactivate(email: str) -> bool:
    async with get_session() as session:
        result = await session.execute(
            select(GmailAccount).where(GmailAccount.email == email)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        row.is_active = False
        row.updated_at = datetime.utcnow()
        await session.commit()
        return True


def _to_dict(row: GmailAccount, master: str) -> dict:
    return {
        "id": row.id,
        "email": row.email,
        "refresh_token": decrypt(row.refresh_token_enc, master) if row.refresh_token_enc else None,
        "access_token": decrypt(row.access_token_enc, master) if row.access_token_enc else None,
        "access_expiry": row.access_expiry,
        "history_id": row.history_id,
        "last_polled_at": row.last_polled_at,
        "is_active": row.is_active,
        "created_at": row.created_at,
    }
