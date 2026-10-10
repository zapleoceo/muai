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


_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


def _embedded_v4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4, спрятанные в IPv6 (mapped, 6to4, Teredo, NAT64): каждый обязан быть публичным."""
    found = [ip.ipv4_mapped] if ip.ipv4_mapped else []
    if ip.sixtofour:
        found.append(ip.sixtofour)
    if ip.teredo:
        found.extend(ip.teredo)
    if ip in _NAT64_WKP:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return found


def _is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _NAT64_LOCAL or ip in _IPV4_COMPATIBLE:
            return False
        if not all(_is_public(str(v4)) for v4 in _embedded_v4(ip)):
            return False
    return ip.is_global and not ip.is_multicast


async def check_callback_url(url: str) -> list[str]:
    """Возвращает проверенные адреса: соединяться нужно с ними, а не с повторным DNS-ответом."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme != "https" or not host:
            raise CallbackRejected("https_required")
        if parts.username or parts.password:
            raise CallbackRejected("credentials_in_url")
        parts.port  # noqa: B018  ValueError на мусорном порту
        host.encode("idna")
    except CallbackRejected:
        raise
    except ValueError as exc:  # UnicodeError тоже ValueError
        raise CallbackRejected("bad_url") from exc
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = await net.resolver(host)
        except OSError as exc:
            raise CallbackRejected("dns_failed") from exc
        except UnicodeError as exc:
            raise CallbackRejected("bad_url") from exc
    try:
        public = bool(addresses) and all(_is_public(a) for a in addresses)
    except ValueError as exc:
        raise CallbackRejected("address_not_public") from exc
    if not public:
        raise CallbackRejected("address_not_public")
    return addresses


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


def signed_headers(secret: str, event_id: str, body: str, subscription_id: str,
                   now: datetime | None = None) -> dict[str, str]:
    """Свежие timestamp и подпись на каждую попытку; webhook-id остаётся event_id."""
    now = now or datetime.now(UTC)
    return {"Content-Type": "application/json", "webhook-id": event_id,
            "webhook-timestamp": str(int(now.timestamp())),
            "webhook-signature": Webhook(secret).sign(event_id, now, body),
            "X-MCP-Subscription-Id": subscription_id}


def http_client(timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=net.transport, follow_redirects=False, timeout=timeout,
                             trust_env=False)


def _pinned_target(url: str, address: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """URL с IP вместо имени, заголовок Host и SNI/проверка сертификата по исходному имени."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    ip_text = f"[{address}]" if ":" in address else address
    port = f":{parts.port}" if parts.port else ""
    pinned = parts._replace(netloc=f"{ip_text}{port}").geturl()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        shown = f"[{host}]" if ":" in host else host
        return pinned, {"Host": f"{shown}{port}"}, {"sni_hostname": host}
    return pinned, {}, {}


async def post_pinned(url: str, addresses: list[str], *, content: bytes | str,
                      headers: dict[str, str], timeout: float = 10.0) -> httpx.Response:
    """POST на заранее проверенный IP: DNS второй раз не спрашивается (нет окна rebinding)."""
    last: httpx.ConnectError | None = None
    async with http_client(timeout=timeout) as client:
        for address in addresses:
            target, extra, ext = _pinned_target(url, address.split("%", 1)[0])
            try:
                return await client.post(target, content=content, headers={**headers, **extra},
                                         extensions=ext)
            except httpx.ConnectError as exc:
                last = exc
    raise last or httpx.ConnectError("no addresses")
