"""Вход в хаб: свой токен у каждого агента, в БД только sha256.

Найденный агент кэшируется на AGENT_CACHE_S: отключение или ротация в админке
действует не позже чем через это время. Неверный токен тоже кэшируется — перебор
не превращается в нагрузку на БД.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from vera_shared.db.models_hub import HubAgentRow
from vera_shared.hub.repo_agents import agent_by_token, touch_agent
from vera_shared.hub.secrets import token_hash

AGENT_CACHE_S = 30.0
TOUCH_EVERY_S = 300.0
_MAX_TOKEN = 256
CACHE_LIMIT = 1024


@dataclass(frozen=True)
class AgentCtx:
    id: int
    name: str
    allow: tuple[str, ...]


Lookup = Callable[[str], Awaitable[HubAgentRow | None]]


class AgentAuth:
    def __init__(self, lookup: Lookup = agent_by_token,
                 touch: Callable[[int], Awaitable[None]] = touch_agent) -> None:
        self._lookup, self._touch = lookup, touch
        self._cache: dict[str, tuple[float, AgentCtx | None]] = {}
        self._misses: dict[str, tuple[float, AgentCtx | None]] = {}
        self._touched: dict[int, float] = {}

    def _remember(self, key: str, now: float, agent: AgentCtx | None) -> None:
        # Неверные токены живут отдельно: поток мусорных токенов вытесняет только
        # друг друга, а не кэш настоящих агентов.
        cache = self._cache if agent else self._misses
        while len(cache) >= CACHE_LIMIT:
            cache.pop(next(iter(cache)))
        cache[key] = (now, agent)

    async def resolve(self, authorization: str | None) -> AgentCtx | None:
        scheme, _, token = (authorization or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token or len(token) > _MAX_TOKEN:
            return None
        key = token_hash(token)
        now = time.monotonic()
        hit = self._cache.get(key) or self._misses.get(key)
        if hit and now - hit[0] < AGENT_CACHE_S:
            agent = hit[1]
        else:
            row = await self._lookup(token)
            agent = AgentCtx(row.id, row.name, tuple(row.allow or ())) if row else None
            self._remember(key, now, agent)
        if agent and now - self._touched.get(agent.id, 0.0) > TOUCH_EVERY_S:
            self._touched[agent.id] = now
            await self._touch(agent.id)
        return agent
