"""Access-token cache + refresh against Google OAuth."""
import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from vera_shared.crypto import decrypt, encrypt
from vera_shared.db.engine import get_session
from vera_shared.db.models import GmailAccount

from app.config import get_settings

log = logging.getLogger(__name__)

_TOKEN_URL = "https://oauth2.googleapis.com/token"


async def get_access_token(email: str) -> str:
    """Return a fresh access_token for an account, refreshing if needed."""
    cfg = get_settings()
    master = cfg.session_secret

    async with get_session() as session:
        result = await session.execute(
            select(GmailAccount).where(
                GmailAccount.email == email, GmailAccount.is_active == True,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise LookupError(f"Gmail account '{email}' not connected")

        now = datetime.utcnow()
        if (
            row.access_token_enc
            and row.access_expiry
            and row.access_expiry > now + timedelta(seconds=30)
        ):
            return decrypt(row.access_token_enc, master)

        refresh = decrypt(row.refresh_token_enc, master)
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": cfg.gmail_client_id,
                "client_secret": cfg.gmail_client_secret,
            })
        if r.status_code != 200:
            # NEVER log r.text — Google may echo refresh_token / client_secret
            # in error bodies. Log status + sanitized error code only.
            try:
                err_code = (r.json() or {}).get("error", "")[:40]
            except Exception:
                err_code = "parse_failed"
            log.warning("token refresh failed for %s: HTTP %d error=%s",
                        email, r.status_code, err_code)
            r.raise_for_status()
        data = r.json()
        new_access = data["access_token"]
        new_expiry = now + timedelta(seconds=int(data.get("expires_in", 3500)) - 60)
        row.access_token_enc = encrypt(new_access, master)
        row.access_expiry = new_expiry
        await session.commit()
        return new_access


async def list_connected_emails() -> list[str]:
    async with get_session() as session:
        result = await session.execute(
            select(GmailAccount.email).where(GmailAccount.is_active == True)
        )
        return [row[0] for row in result.all()]
