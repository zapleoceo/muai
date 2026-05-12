import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import ApiToken
from app.llm.capabilities import effective_capabilities

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
    capabilities: set[str]
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


@dataclass(frozen=True)
class TokenLease:
    id: int
    provider: str
    token: str


class TokenManager:
    def __init__(self) -> None:
        self._slots_by_id: dict[int, _Slot] = {}
        self._rr_idx: dict[str, int] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def load(self, provider: str | None = None) -> None:
        async with AsyncSessionLocal() as session:
            q = select(ApiToken).where(ApiToken.is_active.is_(True)).order_by(ApiToken.provider, ApiToken.id)
            if provider:
                q = q.where(ApiToken.provider == provider)
            rows = (await session.execute(q)).scalars().all()
        async with self._lock:
            existing = self._slots_by_id
            next_slots: dict[int, _Slot] = {}
            for r in rows:
                prov = r.provider
                caps_raw = r.capabilities if isinstance(r.capabilities, list) else None
                caps = effective_capabilities(prov, caps_raw)
                prev = existing.get(r.id)
                next_slots[r.id] = _Slot(
                    db_id=r.id,
                    value=r.token,
                    label=r.label or "",
                    provider=prov,
                    capabilities=caps,
                    daily_limit=self._daily_limit_for(prov),
                    cooldown_until=prev.cooldown_until if prev else None,
                    requests_today=prev.requests_today if prev else 0,
                    _day=prev._day if prev else date.today(),
                )
            self._slots_by_id = next_slots
            self._loaded = True
        logger.info("TokenManager: %d active token(s)", len(self._slots_by_id))

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

    async def next_token(self, capability: str, provider: str | None = None) -> TokenLease | None:
        if not self._loaded:
            await self.load()
        key = f"{provider or '*'}:{capability}"
        while True:
            async with self._lock:
                slots = [
                    s for s in self._slots_by_id.values()
                    if capability in s.capabilities and (provider is None or s.provider == provider)
                ]
                if not slots:
                    return None
                available = [s for s in slots if s.available()]
                if available:
                    idx = self._rr_idx.get(key, 0)
                    slot = available[idx % len(available)]
                    self._rr_idx[key] = (idx + 1) % len(available)
                    slot.increment()
                    return TokenLease(id=slot.db_id, provider=slot.provider, token=slot.value)
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

    async def on_rate_limit(self, token_id: int) -> None:
        async with self._lock:
            slot = self._slots_by_id.get(token_id)
            if not slot:
                return
            slot.cooldown_until = datetime.now(tz=timezone.utc) + timedelta(seconds=_COOLDOWN_RATE_LIMIT)
            logger.warning("TokenManager: %s on rate-limit cooldown (%ds)", slot.masked(), _COOLDOWN_RATE_LIMIT)

    async def on_error(self, token_id: int) -> None:
        async with self._lock:
            slot = self._slots_by_id.get(token_id)
            if not slot:
                return
            slot.cooldown_until = datetime.now(tz=timezone.utc) + timedelta(seconds=_COOLDOWN_ERROR)
            logger.warning("TokenManager: %s on error cooldown (%ds)", slot.masked(), _COOLDOWN_ERROR)

    # ── CRUD (called from API routes) ─────────────────────────────────────────

    async def add(self, token: str, label: str, provider: str = "gemini", capabilities: list[str] | None = None) -> ApiToken:
        async with AsyncSessionLocal() as session:
            row = ApiToken(provider=provider, token=token, label=label, capabilities=capabilities)
            session.add(row)
            await session.commit()
            await session.refresh(row)
        await self.load()
        return row

    async def remove(self, token_id: int) -> bool:
        async with AsyncSessionLocal() as session:
            row = await session.get(ApiToken, token_id)
            if not row:
                return False
            await session.delete(row)
            await session.commit()
        await self.load()
        return True

    async def toggle(self, token_id: int) -> ApiToken | None:
        async with AsyncSessionLocal() as session:
            row = await session.get(ApiToken, token_id)
            if not row:
                return None
            row.is_active = not row.is_active
            await session.commit()
            await session.refresh(row)
        await self.load()
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
        for s in self._slots_by_id.values():
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
                "capabilities": sorted(s.capabilities),
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
