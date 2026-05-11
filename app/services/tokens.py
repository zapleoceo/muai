import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.db.database import AsyncSessionLocal
from app.db.models import ApiToken

logger = logging.getLogger(__name__)

_COOLDOWN_RATE_LIMIT = 60    # seconds on HTTP 429
_COOLDOWN_ERROR = 300        # 5 min on repeated errors


@dataclass
class _Slot:
    db_id: int
    value: str
    label: str
    cooldown_until: datetime | None = field(default=None)

    def available(self) -> bool:
        if not self.cooldown_until:
            return True
        return datetime.now(tz=timezone.utc) >= self.cooldown_until

    def masked(self) -> str:
        v = self.value
        return f"{v[:8]}...{v[-4:]}" if len(v) > 12 else "***"


class TokenManager:
    def __init__(self) -> None:
        self._slots: list[_Slot] = []
        self._idx = 0
        self._lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def load(self, provider: str = "gemini") -> None:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(ApiToken)
                .where(ApiToken.provider == provider, ApiToken.is_active.is_(True))
                .order_by(ApiToken.id)
            )).scalars().all()
        async with self._lock:
            existing = {s.db_id: s.cooldown_until for s in self._slots}
            self._slots = [
                _Slot(
                    db_id=r.id,
                    value=r.token,
                    label=r.label or "",
                    cooldown_until=existing.get(r.id),  # keep live cooldowns on reload
                )
                for r in rows
            ]
        logger.info("TokenManager: %d active %s token(s)", len(self._slots), provider)

    async def seed_from_env(self, api_key: str, provider: str = "gemini") -> None:
        """Insert the .env key into DB if no tokens exist yet."""
        if not api_key:
            return
        async with AsyncSessionLocal() as session:
            existing = (await session.execute(
                select(ApiToken).where(ApiToken.provider == provider)
            )).first()
            if not existing:
                session.add(ApiToken(provider=provider, token=api_key, label="default"))
                await session.commit()
                logger.info("TokenManager: seeded %s key from env", provider)

    # ── token access ──────────────────────────────────────────────────────────

    async def next_token(self, provider: str = "gemini") -> str | None:
        async with self._lock:
            if not self._slots:
                return None
            available = [s for s in self._slots if s.available()]
            if not available:
                # all cooling — return the one with the soonest cooldown
                soonest = min(self._slots, key=lambda s: s.cooldown_until or datetime.min.replace(tzinfo=timezone.utc))
                return soonest.value
            token = available[self._idx % len(available)]
            self._idx = (self._idx + 1) % len(available)
            return token.value

    async def on_rate_limit(self, token_value: str) -> None:
        async with self._lock:
            for slot in self._slots:
                if slot.value == token_value:
                    slot.cooldown_until = datetime.now(tz=timezone.utc) + timedelta(seconds=_COOLDOWN_RATE_LIMIT)
                    logger.warning("TokenManager: %s on rate-limit cooldown (%ds)", slot.masked(), _COOLDOWN_RATE_LIMIT)
                    break

    async def on_error(self, token_value: str) -> None:
        async with self._lock:
            for slot in self._slots:
                if slot.value == token_value:
                    slot.cooldown_until = datetime.now(tz=timezone.utc) + timedelta(seconds=_COOLDOWN_ERROR)
                    logger.warning("TokenManager: %s on error cooldown (%ds)", slot.masked(), _COOLDOWN_ERROR)
                    break

    # ── CRUD (called from API routes) ─────────────────────────────────────────

    async def add(self, token: str, label: str, provider: str = "gemini") -> ApiToken:
        async with AsyncSessionLocal() as session:
            row = ApiToken(provider=provider, token=token, label=label)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        await self.load(provider)
        return row

    async def remove(self, token_id: int, provider: str = "gemini") -> bool:
        async with AsyncSessionLocal() as session:
            row = await session.get(ApiToken, token_id)
            if not row:
                return False
            await session.delete(row)
            await session.commit()
        await self.load(provider)
        return True

    async def toggle(self, token_id: int, provider: str = "gemini") -> ApiToken | None:
        async with AsyncSessionLocal() as session:
            row = await session.get(ApiToken, token_id)
            if not row:
                return None
            row.is_active = not row.is_active
            await session.commit()
            await session.refresh(row)
        await self.load(provider)
        return row

    async def list_tokens(self, provider: str = "gemini") -> list[ApiToken]:
        async with AsyncSessionLocal() as session:
            return list((await session.execute(
                select(ApiToken).where(ApiToken.provider == provider).order_by(ApiToken.id)
            )).scalars().all())

    # ── status (for dashboard) ────────────────────────────────────────────────

    def slot_status(self) -> dict[int, str]:
        """Return {db_id: 'active'|'cooldown'|'inactive'} from live in-memory state."""
        now = datetime.now(tz=timezone.utc)
        return {
            s.db_id: "cooldown" if s.cooldown_until and now < s.cooldown_until else "active"
            for s in self._slots
        }


# ── singleton ─────────────────────────────────────────────────────────────────

_manager: TokenManager | None = None


def get_token_manager() -> TokenManager:
    global _manager
    if _manager is None:
        _manager = TokenManager()
    return _manager
