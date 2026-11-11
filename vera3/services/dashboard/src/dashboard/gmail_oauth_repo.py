"""SQL переподключения Gmail: сохранить новый refresh-токен ящика."""
from __future__ import annotations

from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models_sources import GmailAccountRow


async def save_gmail_refresh_token(email: str, refresh_enc: str) -> None:
    """Ящик снова активен, ошибка снята; новый — заводится."""
    async with get_session() as s:
        existing = (await s.execute(
            select(GmailAccountRow).where(GmailAccountRow.email == email)
        )).scalar_one_or_none()
        if existing:
            existing.refresh_token_enc = refresh_enc
            existing.is_active = True
            existing.needs_reauth = False
            existing.last_error = None
            existing.last_polled_at = None  # перепрокачать новые письма
        else:
            s.add(GmailAccountRow(email=email, refresh_token_enc=refresh_enc,
                                  is_active=True, needs_reauth=False))
