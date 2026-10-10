"""Сеть MCP Events: SSRF-охрана callback URL, подпись Standard Webhooks, HTTP-клиент."""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from standardwebhooks import Webhook

Resolver = Callable[[str], Awaitable[list[str]]]
SECRET_PREFIX = "whsec_"
MIN_SECRET_BYTES, MAX_SECRET_BYTES = 24, 64
MAX_BODY_BYTES = 262_144


class CallbackRejected(ValueError):
    """Callback URL или секрет не прошли проверку; reason идёт в data.reason ошибки."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


@dataclass
class EventsNet:
    """Точка подмены для тестов: резолвер и транспорт. Проверки адресов не отключаются."""
    resolver: Resolver = system_resolver
    transport: httpx.AsyncBaseTransport | None = None


net = EventsNet()


def _is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def check_callback_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise CallbackRejected("https_required")
    if parts.username or parts.password:
        raise CallbackRejected("credentials_in_url")
    try:
        parts.port  # noqa: B018  ValueError на мусорном порту
        ipaddress.ip_address(parts.hostname)
        addresses = [parts.hostname]
    except ValueError:
        try:
            addresses = await net.resolver(parts.hostname)
        except OSError as exc:
            raise CallbackRejected("dns_failed") from exc
    if not addresses or not all(_is_public(a) for a in addresses):
        raise CallbackRejected("address_not_public")


def validate_secret(secret: str) -> None:
    if not secret.startswith(SECRET_PREFIX):
        raise CallbackRejected("bad_secret")
    try:
        raw = secret[len(SECRET_PREFIX):].rstrip("=")
        size = len(base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True))
    except (binascii.Error, ValueError) as exc:
        raise CallbackRejected("bad_secret") from exc
    if not MIN_SECRET_BYTES <= size <= MAX_SECRET_BYTES:
        raise CallbackRejected("bad_secret")


def signed_headers(secret: str, event_id: str, body: str, subscription_id: str) -> dict[str, str]:
    """Свежие timestamp и подпись на каждую попытку; webhook-id остаётся event_id."""
    now = datetime.now(UTC)
    return {"Content-Type": "application/json", "webhook-id": event_id,
            "webhook-timestamp": str(int(now.timestamp())),
            "webhook-signature": Webhook(secret).sign(event_id, now, body),
            "X-MCP-Subscription-Id": subscription_id}


def http_client(timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=net.transport, follow_redirects=False, timeout=timeout)
