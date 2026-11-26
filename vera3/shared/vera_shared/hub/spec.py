"""Проверка настроек апстрима из админки до записи в БД."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from vera_shared.hub.names import PROJECTS, HubNameError, valid_slug

HTTP_AUTH = ("none", "bearer", "header", "url")
STDIO_AUTH = ("none", "env")
_HEADER = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")
_ENV = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
# эти заголовки ставит сам хаб: подмена ломала бы протокол или маршрутизацию
_RESERVED_HEADERS = {"host", "content-type", "content-length", "accept", "connection",
                     "mcp-session-id", "mcp-protocol-version", "x-vera-hub-agent"}


@dataclass
class UpstreamSpec:
    project: str
    slug: str
    title: str = ""
    transport: str = "http"
    url: str = ""
    command: list[str] = field(default_factory=list)
    auth_kind: str = "none"
    auth_header: str = ""
    enabled: bool = True
    hint: str = ""
    timeout_s: int = 30
    max_concurrency: int = 4

    def validate(self) -> UpstreamSpec:
        if self.project not in PROJECTS:
            raise HubNameError(f"unknown project {self.project!r}")
        if not valid_slug(self.slug):
            raise HubNameError("slug: 2-16 chars, a-z and digits, starts with a letter")
        if not 1 <= self.timeout_s <= 120:
            raise HubNameError("timeout_s must be 1..120")
        if not 1 <= self.max_concurrency <= 16:
            raise HubNameError("max_concurrency must be 1..16")
        if self.transport == "http":
            self._check_http()
        elif self.transport == "stdio":
            self._check_stdio()
        else:
            raise HubNameError("transport must be http or stdio")
        return self

    def _check_http(self) -> None:
        if self.auth_kind not in HTTP_AUTH:
            raise HubNameError(f"http auth_kind must be one of {', '.join(HTTP_AUTH)}")
        if self.url:
            parts = urlsplit(self.url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise HubNameError("url must be http(s)://host/…")
            if parts.username or parts.password:
                raise HubNameError("credentials do not belong in url: use the secret field")
        elif self.enabled and self.auth_kind != "url":
            raise HubNameError("enabled http upstream needs a url")
        if self.auth_kind == "header" and (
                not _HEADER.fullmatch(self.auth_header)
                or self.auth_header.lower() in _RESERVED_HEADERS):
            raise HubNameError("auth_header: a custom header name (not a protocol header)")

    def _check_stdio(self) -> None:
        if self.auth_kind not in STDIO_AUTH:
            raise HubNameError(f"stdio auth_kind must be one of {', '.join(STDIO_AUTH)}")
        if self.enabled and not self.command:
            raise HubNameError("enabled stdio upstream needs a command")
        if self.auth_kind == "env" and not _ENV.fullmatch(self.auth_header):
            raise HubNameError("auth_header: env variable name for the secret (A-Z0-9_)")


def needs_secret(auth_kind: str) -> bool:
    return auth_kind != "none"
