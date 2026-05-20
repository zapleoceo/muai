"""Gmail OAuth 2.0 flow + access-token refresh."""
import logging
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
_REDIRECT_PATH = "/api/gmail/oauth/callback"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Module-level state store for CSRF. In a single-process FastAPI this is fine.
# For multi-worker we'd move to Redis or the settings table.
_state_store: dict[str, datetime] = {}
_STATE_TTL = timedelta(minutes=10)


def build_redirect_uri() -> str:
    return get_settings().webhook_base_url.rstrip("/") + _REDIRECT_PATH


def build_auth_url() -> tuple[str, str]:
    settings = get_settings()
    state = secrets.token_urlsafe(32)
    _state_store[state] = datetime.utcnow()
    _gc_states()
    params = {
        "response_type": "code",
        "client_id": settings.gmail_client_id,
        "redirect_uri": build_redirect_uri(),
        "scope": " ".join(_SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # force refresh_token issuance every time
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{_AUTH_URL}?{urlencode(params)}", state


def consume_state(state: str) -> bool:
    issued = _state_store.pop(state, None)
    if issued is None:
        return False
    if datetime.utcnow() - issued > _STATE_TTL:
        return False
    return True


def _gc_states() -> None:
    cutoff = datetime.utcnow() - _STATE_TTL
    dead = [k for k, v in _state_store.items() if v < cutoff]
    for k in dead:
        _state_store.pop(k, None)


async def exchange_code(code: str) -> dict:
    """Exchange auth code for {access_token, refresh_token, expires_in, ...}."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
            "redirect_uri": build_redirect_uri(),
        })
    r.raise_for_status()
    return r.json()


async def refresh_access_token(refresh_token: str) -> tuple[str, datetime]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.gmail_client_id,
            "client_secret": settings.gmail_client_secret,
        })
    r.raise_for_status()
    data = r.json()
    expiry = datetime.utcnow() + timedelta(seconds=int(data.get("expires_in", 3500)) - 60)
    return data["access_token"], expiry


async def fetch_userinfo(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    r.raise_for_status()
    return r.json()
