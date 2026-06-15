import base64
import hashlib
import secrets
import tempfile
import time

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from app.mcp.auth_gate import _sign, _valid
from app.mcp.provider import TelegramOAuthProvider
from app.mcp.store import OAuthStore

_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="c1",
        client_secret="s1",
        redirect_uris=[AnyUrl(_REDIRECT)],
        grant_types=["authorization_code", "refresh_token"],
        token_endpoint_auth_method="client_secret_post",
    )


def _params() -> tuple[AuthorizationParams, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    params = AuthorizationParams(
        state="xyz",
        scopes=["telegram"],
        code_challenge=challenge,
        redirect_uri=AnyUrl(_REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource="https://tg-mcp.veranda.my",
    )
    return params, challenge


async def _provider() -> TelegramOAuthProvider:
    store = OAuthStore(tempfile.mktemp(suffix=".db"))
    await store.init()
    return TelegramOAuthProvider(store, ["telegram"])


@pytest.mark.asyncio
async def test_client_registration_roundtrip():
    prov = await _provider()
    await prov.register_client(_client())
    assert (await prov.get_client("c1")).client_id == "c1"
    assert await prov.get_client("missing") is None


@pytest.mark.asyncio
async def test_authorization_code_flow_is_single_use():
    prov = await _provider()
    client = _client()
    await prov.register_client(client)
    params, challenge = _params()

    redirect = await prov.authorize(client, params)
    assert "state=xyz" in redirect
    code = redirect.split("code=")[1].split("&")[0]

    loaded = await prov.load_authorization_code(client, code)
    assert loaded is not None and loaded.code_challenge == challenge

    token = await prov.exchange_authorization_code(client, loaded)
    assert token.access_token and token.refresh_token
    assert await prov.load_authorization_code(client, code) is None


@pytest.mark.asyncio
async def test_access_token_carries_resource_and_subject():
    prov = await _provider()
    client = _client()
    await prov.register_client(client)
    params, _ = _params()
    code = (await prov.authorize(client, params)).split("code=")[1].split("&")[0]
    loaded = await prov.load_authorization_code(client, code)
    token = await prov.exchange_authorization_code(client, loaded)

    access = await prov.load_access_token(token.access_token)
    assert access.resource == "https://tg-mcp.veranda.my"
    assert access.subject == "owner"
    assert access.scopes == ["telegram"]


@pytest.mark.asyncio
async def test_refresh_token_rotation_revokes_old():
    prov = await _provider()
    client = _client()
    await prov.register_client(client)
    params, _ = _params()
    code = (await prov.authorize(client, params)).split("code=")[1].split("&")[0]
    loaded = await prov.load_authorization_code(client, code)
    first = await prov.exchange_authorization_code(client, loaded)

    rt = await prov.load_refresh_token(client, first.refresh_token)
    second = await prov.exchange_refresh_token(client, rt, ["telegram"])

    assert second.access_token != first.access_token
    assert await prov.load_refresh_token(client, first.refresh_token) is None
    assert await prov.load_refresh_token(client, second.refresh_token) is not None


def test_owner_cookie_signature():
    good = _sign("secret", int(time.time()) + 100)
    assert _valid("secret", good)
    assert not _valid("other", good)
    assert not _valid("secret", "tampered.value")
    assert not _valid("secret", _sign("secret", int(time.time()) - 5))
