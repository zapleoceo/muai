import logging

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.mcp.auth_gate import OwnerAuthGate
from app.mcp.provider import TelegramOAuthProvider
from app.mcp.store import OAuthStore
from app.mcp.tools import register_tools

log = logging.getLogger(__name__)

_SCOPES = ["telegram"]


def mcp_misconfig() -> str | None:
    cfg = get_settings()
    missing = [
        name
        for name, val in (
            ("MCP_PUBLIC_URL", cfg.mcp_public_url),
            ("MCP_OAUTH_PASSWORD", cfg.mcp_oauth_password),
            ("MCP_OAUTH_SIGNING_SECRET", cfg.mcp_oauth_signing_secret),
        )
        if not val
    ]
    return f"missing env: {', '.join(missing)}" if missing else None


async def build_mcp_app() -> Starlette:
    cfg = get_settings()
    base = cfg.mcp_public_url.rstrip("/")

    store = OAuthStore(cfg.mcp_oauth_db)
    await store.init()
    provider = TelegramOAuthProvider(store, _SCOPES)

    auth = AuthSettings(
        issuer_url=base,
        resource_server_url=base,
        required_scopes=_SCOPES,
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=_SCOPES, default_scopes=_SCOPES
        ),
        revocation_options=RevocationOptions(enabled=True),
    )

    mcp = FastMCP(
        "vera-telegram",
        instructions="Read, search and message your Telegram account.",
        host="0.0.0.0",
        port=cfg.mcp_port,
        streamable_http_path="/mcp",
        auth_server_provider=provider,
        auth=auth,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    register_tools(mcp, cfg.mcp_allow_destructive)

    gate = OwnerAuthGate(cfg.mcp_oauth_signing_secret, cfg.mcp_oauth_password)
    mcp.custom_route("/owner-login", methods=["POST"])(gate.login)

    app = mcp.streamable_http_app()
    app.add_middleware(BaseHTTPMiddleware, dispatch=gate.dispatch)
    log.info("MCP app built at %s (resource=%s/mcp)", base, base)
    return app
