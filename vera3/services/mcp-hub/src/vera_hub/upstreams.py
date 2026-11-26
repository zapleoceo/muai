"""Реестр апстримов: настройки из БД (с коротким кэшем), клиенты, лимиты.

Каждый вызов ограничен двумя рубежами: семафор на апстрим (не больше
`max_concurrency` параллельных вызовов, ожидание очереди — BUSY_WAIT_S) и
таймаут `timeout_s`. Зависший апстрим отвечает агенту ошибкой, а не держит хаб.
Секрет расшифровывается только при сборке клиента и живёт в его заголовках.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import InvalidToken
from vera_shared.db.models_hub import HubUpstreamRow
from vera_shared.hub.repo_upstreams import list_upstreams
from vera_shared.hub.secrets import HubKeyMissing, unseal
from vera_shared.hub.spec import needs_secret

from vera_hub.errors import UpstreamError
from vera_hub.upstream_http import HttpUpstream
from vera_hub.upstream_stdio import StdioUpstream

CONFIG_TTL_S = 15.0
BUSY_WAIT_S = 2.0
AGENT_HEADER = "X-Vera-Hub-Agent"


class Client(Protocol):
    async def request(self, method: str, params: dict[str, Any],
                      extra: dict[str, str] | None = None) -> dict[str, Any]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class Upstream:
    id: int
    project: str
    slug: str
    transport: str
    url: str
    command: tuple[str, ...]
    auth_kind: str
    auth_header: str
    secret_enc: str | None = field(repr=False)
    enabled: bool
    timeout_s: int
    max_concurrency: int
    updated_at: datetime | None

    @classmethod
    def of(cls, row: HubUpstreamRow) -> Upstream:
        return cls(row.id, row.project, row.slug, row.transport, row.url,
                   tuple(row.command or ()), row.auth_kind, row.auth_header, row.secret_enc,
                   row.enabled, row.timeout_s, row.max_concurrency, row.updated_at)

    def blocker(self) -> str | None:
        """Почему апстрим сейчас не вызывается: disabled | needs_secret | needs_config."""
        if not self.enabled:
            return "disabled"
        if needs_secret(self.auth_kind) and not self.secret_enc:
            return "needs_secret"
        if self.transport == "stdio" and not self.command:
            return "needs_config"
        if self.transport == "http" and not self.url and self.auth_kind != "url":
            return "needs_config"
        return None


def internal_hosts() -> frozenset[str]:
    raw = os.environ.get("HUB_INTERNAL_HOSTS", "mcp")
    return frozenset(h.strip() for h in raw.split(",") if h.strip())


def _build(up: Upstream, http: httpx.AsyncClient) -> Client:
    secret = unseal(up.secret_enc) if up.secret_enc else ""
    if up.transport == "stdio":
        env = {up.auth_header: secret} if up.auth_kind == "env" else {}
        return StdioUpstream(list(up.command), env)
    url, headers = up.url, {}
    if up.auth_kind == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif up.auth_kind == "header":
        headers[up.auth_header] = secret
    elif up.auth_kind == "url":
        url = secret
    return HttpUpstream(http, url, headers, internal=urlsplit(url).hostname in internal_hosts())


class Registry:
    def __init__(self, loader: Callable[[], Awaitable[list[HubUpstreamRow]]] = list_upstreams,
                 http: httpx.AsyncClient | None = None) -> None:
        self._loader = loader
        self._http = http or httpx.AsyncClient(
            # Редиректы не идут: httpx снимает на чужом хосте только Authorization, а свой
            # заголовок с секретом и тело вызова ушли бы туда, куда укажет апстрим (и SSRF).
            # pool=3: пул, забитый зависшими апстримами, не держит вызовы к здоровым.
            timeout=httpx.Timeout(120.0, connect=5.0, pool=3.0), follow_redirects=False,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16))
        self._cache: list[Upstream] = []
        self._loaded_at = 0.0
        self._load_lock = asyncio.Lock()
        self._clients: dict[int, tuple[Upstream, Client]] = {}
        self._gates: dict[int, tuple[int, asyncio.Semaphore]] = {}
        self._build_locks: dict[int, asyncio.Lock] = {}

    async def upstreams(self, fresh: bool = False) -> list[Upstream]:
        async with self._load_lock:
            if fresh or time.monotonic() - self._loaded_at > CONFIG_TTL_S:
                self._cache = [Upstream.of(r) for r in await self._loader()]
                self._loaded_at = time.monotonic()
            return list(self._cache)

    async def find(self, project: str, slug: str) -> Upstream | None:
        return next((u for u in await self.upstreams()
                     if u.project == project and u.slug == slug), None)

    async def _client(self, up: Upstream) -> Client:
        # замок на апстрим: два вызова после правки настроек не строят два клиента
        # (лишний stdio-процесс остался бы без хозяина)
        async with self._build_locks.setdefault(up.id, asyncio.Lock()):
            cached = self._clients.get(up.id)
            if cached and cached[0] == up:
                return cached[1]
            if cached:
                await cached[1].close()
                del self._clients[up.id]
            try:
                client = _build(up, self._http)
            except (InvalidToken, HubKeyMissing, ValueError) as exc:
                raise UpstreamError("down", "upstream secret cannot be decrypted") from exc
            self._clients[up.id] = (up, client)
            return client

    def _gate(self, up: Upstream) -> asyncio.Semaphore:
        size, sem = self._gates.get(up.id, (0, None))
        if sem is None or size != up.max_concurrency:
            sem = asyncio.Semaphore(up.max_concurrency)
            self._gates[up.id] = (up.max_concurrency, sem)
        return sem

    async def call(self, up: Upstream, method: str, params: dict[str, Any],
                   agent: str = "", timeout_s: float | None = None) -> dict[str, Any]:
        if (reason := up.blocker()) is not None:
            raise UpstreamError("down", f"upstream {reason}")
        sem = self._gate(up)
        try:
            await asyncio.wait_for(sem.acquire(), BUSY_WAIT_S)
        except TimeoutError:
            raise UpstreamError("busy", "upstream is at its concurrency limit") from None
        try:
            client = await self._client(up)
            extra = {AGENT_HEADER: agent} if agent else None
            return await asyncio.wait_for(client.request(method, params, extra),
                                          timeout_s or up.timeout_s)
        except TimeoutError:
            raise UpstreamError("timeout", f"upstream did not answer in {timeout_s or up.timeout_s}s") from None
        finally:
            sem.release()

    async def close(self) -> None:
        for _, client in self._clients.values():
            await client.close()
        self._clients.clear()
        await self._http.aclose()
