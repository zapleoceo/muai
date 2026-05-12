import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import ApiToken

logger = logging.getLogger(__name__)

_COOLDOWN_RATE_LIMIT = 60    # seconds on HTTP 429
_COOLDOWN_ERROR = 300        # 5 min on repeated errors
GEMINI_DAILY_LIMIT = 1500    # free Gemini tier requests/day per key


@dataclass
class _Slot:
    db_id: int
    value: str
    label: str
    provider: str
    daily_limit: int
    cooldown_until: datetime | None = field(default=None)
    requests_today: int = field(default=0)
    _day: date = field(default_factory=date.today)

    def available(self) -> bool:
        self._reset_if_new_day()
        if self.daily_limit > 0 and self.requests_today >= self.daily_limit:
            return False
        if not self.cooldown_until:
            return True
        return datetime.now(tz=timezone.utc) >= self.cooldown_until

    def increment(self) -> None:
        self._reset_if_new_day()
        self.requests_today += 1

    def _reset_if_new_day(self) -> None:
        today = date.today()
        if self._day != today:
            self.requests_today = 0
            self._day = today

    def masked(self) -> str:
        v = self.value
        return f"{v[:8]}...{v[-4:]}" if len(v) > 12 else "***"


class TokenManager:
    def __init__(self) -> None:
        self._slots_by_provider: dict[str, list[_Slot]] = {}
        self._idx_by_provider: dict[str, int] = {}
        self._loaded: set[str] = set()
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
            existing = {s.db_id: s for s in self._slots_by_provider.get(provider, [])}
            daily_limit = self._daily_limit_for(provider)
            self._slots_by_provider[provider] = [
                _Slot(
                    db_id=r.id,
                    value=r.token,
                    label=r.label or "",
                    provider=provider,
                    daily_limit=daily_limit,
                    cooldown_until=existing[r.id].cooldown_until if r.id in existing else None,
                    requests_today=existing[r.id].requests_today if r.id in existing else 0,
                    _day=existing[r.id]._day if r.id in existing else date.today(),
                )
                for r in rows
            ]
            self._idx_by_provider.setdefault(provider, 0)
            self._loaded.add(provider)
        logger.info("TokenManager: %d active %s token(s)", len(self._slots_by_provider.get(provider, [])), provider)

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
        if provider not in self._loaded:
            await self.load(provider)
        while True:
            async with self._lock:
                slots = self._slots_by_provider.get(provider, [])
                if not slots:
                    return None
                available = [s for s in slots if s.available()]
                if available:
                    idx = self._idx_by_provider.get(provider, 0)
                    slot = available[idx % len(available)]
                    self._idx_by_provider[provider] = (idx + 1) % len(available)
                    slot.increment()
                    return slot.value
                # All on cooldown — find how long until the soonest recovers
                now = datetime.now(tz=timezone.utc)
                soonest = min(
                    (s for s in slots if s.cooldown_until),
                    key=lambda s: s.cooldown_until,
                    default=None,
                )
                if soonest is None:
                    return None  # all daily-maxed, nothing to wait for
                wait = (soonest.cooldown_until - now).total_seconds()
            if wait > 0:
                logger.info("TokenManager: all tokens on cooldown, waiting %.1fs", wait)
                await asyncio.sleep(wait)
            # loop back and try again

    async def on_rate_limit(self, provider: str, token_value: str) -> None:
        async with self._lock:
            for slot in self._slots_by_provider.get(provider, []):
                if slot.value == token_value:
                    slot.cooldown_until = datetime.now(tz=timezone.utc) + timedelta(seconds=_COOLDOWN_RATE_LIMIT)
                    logger.warning("TokenManager: %s on rate-limit cooldown (%ds)", slot.masked(), _COOLDOWN_RATE_LIMIT)
                    break

    async def on_error(self, provider: str, token_value: str) -> None:
        async with self._lock:
            for slot in self._slots_by_provider.get(provider, []):
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

    async def remove(self, token_id: int) -> bool:
        async with AsyncSessionLocal() as session:
            row = await session.get(ApiToken, token_id)
            if not row:
                return False
            provider = row.provider
            await session.delete(row)
            await session.commit()
        await self.load(provider)
        return True

    async def toggle(self, token_id: int) -> ApiToken | None:
        async with AsyncSessionLocal() as session:
            row = await session.get(ApiToken, token_id)
            if not row:
                return None
            row.is_active = not row.is_active
            provider = row.provider
            await session.commit()
            await session.refresh(row)
        await self.load(provider)
        return row

    async def list_tokens(self, provider: str | None = None) -> list[ApiToken]:
        async with AsyncSessionLocal() as session:
            q = select(ApiToken).order_by(ApiToken.provider, ApiToken.id)
            if provider:
                q = q.where(ApiToken.provider == provider)
            return list((await session.execute(q)).scalars().all())

    # ── status (for dashboard) ────────────────────────────────────────────────

    def slot_status(self) -> dict[int, dict]:
        """Return {db_id: {status, requests_today, daily_limit}} from live in-memory state."""
        now = datetime.now(tz=timezone.utc)
        result = {}
        for slots in self._slots_by_provider.values():
            for s in slots:
                s._reset_if_new_day()
                if s.cooldown_until and now < s.cooldown_until:
                    status = "cooldown"
                elif s.daily_limit > 0 and s.requests_today >= s.daily_limit:
                    status = "daily_limit"
                else:
                    status = "active"
                result[s.db_id] = {
                    "status": status,
                    "requests_today": s.requests_today,
                    "daily_limit": s.daily_limit,
                }
        return result

    @staticmethod
    def _daily_limit_for(provider: str) -> int:
        if provider == "gemini":
            return GEMINI_DAILY_LIMIT
        return 0


# ── singleton ─────────────────────────────────────────────────────────────────

_manager: TokenManager | None = None


def get_token_manager() -> TokenManager:
    global _manager
    if _manager is None:
        _manager = TokenManager()
    return _manager
