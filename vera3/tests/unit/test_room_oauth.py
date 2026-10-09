"""Room OAuth is opt-in and never grants private-memory tools."""
from __future__ import annotations

import base64
import hashlib
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from dashboard import room_oauth_routes
from dashboard.app import app as dashboard_app
from dashboard.auth import issue_session
from mcp.server.auth.provider import (
    AuthorizationParams,
    OAuthClientInformationFull,
    RegistrationError,
)
from pydantic import AnyUrl
from vera_mcp.room_oauth import PUBLIC_ORIGIN, RESOURCE, SCOPES, RoomOAuthProvider
from vera_mcp.server import build_app


@asynccontextmanager
async def _client():
    app = build_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                          base_url="http://t") as client,
    ):
        yield client


@pytest.mark.asyncio
async def test_room_oauth_discovery_consent_exchange_and_revoke(monkeypatch, sqlite_db):
    monkeypatch.setenv("ROOM_OAUTH_ENABLED", "1")
    monkeypatch.setenv("ROOM_OAUTH_ACTORS", "dot")
    monkeypatch.setenv("ROOM_OAUTH_KEY", "unit-test-room-key-not-production-0123456789")
    monkeypatch.setenv("INTERNAL_SECRET", "test-internal-secret")
    monkeypatch.setenv("ROOM_TOKENS", "claude:" + "x" * 40)
    provider = RoomOAuthProvider()
    client_info = OAuthClientInformationFull(
        client_id="test-chatgpt", client_secret="test-client-secret",
        token_endpoint_auth_method="client_secret_post",
        redirect_uris=[AnyUrl("https://chatgpt.com/oauth/callback")],
        client_name="ChatGPT room test",
    )
    await provider.register_client(client_info)
    assert (await provider.get_client("test-chatgpt")).client_secret == "test-client-secret"

    params = AuthorizationParams(
        state="state1", scopes=SCOPES, code_challenge="x" * 43,
        redirect_uri=AnyUrl("https://chatgpt.com/oauth/callback"),
        redirect_uri_provided_explicitly=True, resource=RESOURCE,
    )
    consent_url = await provider.authorize(client_info, params)
    ticket = parse_qs(urlparse(consent_url).query)["ticket"][0]
    async with _client() as c:
        r = await c.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        assert r.json()["token_endpoint"] == f"{PUBLIC_ORIGIN}/token"
        r = await c.get("/.well-known/oauth-protected-resource/mcp")
        assert r.json()["resource"] == RESOURCE
        r = await c.get("/mcp")
        assert r.status_code == 401
        assert "resource_metadata" in r.headers["www-authenticate"]
        r = await c.get(f"/oauth/internal/consent?ticket={ticket}")
        assert r.status_code == 401
        headers = {"X-Internal-Secret": "test-internal-secret"}
        r = await c.get(f"/oauth/internal/consent?ticket={ticket}", headers=headers)
        assert r.status_code == 200 and r.json()["client_id"] == "test-chatgpt"
        r = await c.post("/oauth/internal/consent",
                         json={"ticket": ticket, "actor": "dot"}, headers=headers)
        assert r.status_code == 200
        code = parse_qs(urlparse(r.json()["redirect_uri"]).query)["code"][0]

    loaded = await provider.load_authorization_code(client_info, code)
    assert loaded and loaded.subject == "dot"
    tokens = await provider.exchange_authorization_code(client_info, loaded)
    assert (await provider.load_authorization_code(client_info, code)) is None
    access = await provider.load_access_token(tokens.access_token)
    assert access and access.subject == "dot" and access.scopes == SCOPES
    async with _client() as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/list", "params": {}},
                         headers={"Authorization": f"Bearer {tokens.access_token}",
                                  "Accept": "application/json, text/event-stream",
                                  "Content-Type": "application/json"})
        assert r.status_code == 200
        names = {tool["name"] for tool in r.json()["result"]["tools"]}
        assert "room_inbox" in names and "sql_query" not in names
        posted = await c.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "room_post", "arguments": {
                "message_id": "oauth-provenance-test", "body": "test-only",
                "to": "claude", "status": "info"}}},
            headers={"Authorization": f"Bearer {tokens.access_token}",
                     "Accept": "application/json, text/event-stream",
                     "Content-Type": "application/json"})
        assert posted.status_code == 200
        assert posted.json()["result"]["structuredContent"]["message"]["from"] == "dot"
    refresh = await provider.load_refresh_token(client_info, tokens.refresh_token)
    assert refresh and refresh.subject == "dot"
    rotated = await provider.exchange_refresh_token(client_info, refresh, SCOPES)
    assert await provider.load_access_token(tokens.access_token) is None
    assert (await provider.load_access_token(rotated.access_token)).subject == "dot"
    await provider.revoke_token(await provider.load_access_token(rotated.access_token))
    assert await provider.load_access_token(rotated.access_token) is None


@pytest.mark.asyncio
async def test_room_oauth_disabled_has_no_discovery(monkeypatch, sqlite_db):
    monkeypatch.delenv("ROOM_OAUTH_ENABLED", raising=False)
    async with _client() as c:
        assert (await c.get("/.well-known/oauth-authorization-server")).status_code == 401


@pytest.mark.asyncio
async def test_dcr_rejects_unapproved_redirect_host(monkeypatch, sqlite_db):
    monkeypatch.setenv("ROOM_OAUTH_KEY", "unit-test-room-key-not-production-0123456789")
    client_info = OAuthClientInformationFull(
        client_id="unapproved", client_secret="test-secret",
        redirect_uris=[AnyUrl("https://elsewhere.example/callback")],
    )
    with pytest.raises(RegistrationError):
        await RoomOAuthProvider().register_client(client_info)


@pytest.mark.asyncio
async def test_dashboard_consent_requires_owner_and_same_origin(monkeypatch):
    monkeypatch.setenv("ROOM_OAUTH_ENABLED", "1")

    async def fake_internal(method: str, *, ticket: str, actor: str | None = None):
        if method == "GET":
            return httpx.Response(200, json={
                "client_id": "chatgpt-client-1", "client_name": "ChatGPT",
                "redirect_uri": "https://chatgpt.com/oauth/callback",
                "scopes": SCOPES, "actors": ["dot"]})
        return httpx.Response(200, json={
            "redirect_uri": "https://chatgpt.com/oauth/callback?code=test"})

    monkeypatch.setattr(room_oauth_routes, "_internal", fake_internal)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard_app),
                                 base_url=PUBLIC_ORIGIN) as c:
        url = "/room/oauth/consent?ticket=test-ticket"
        assert (await c.get(url)).status_code == 401
        cookie, _ = issue_session()
        c.cookies.set("vera3_session", cookie)
        page = await c.get(url)
        assert page.status_code == 200
        assert "chatgpt-client-1" in page.text
        assert "chatgpt.com/oauth/callback" in page.text
        assert page.headers["x-frame-options"] == "DENY"
        # form-action действует и на 303 после формы: без origin callback браузер
        # молча блокировал возврат в ChatGPT
        assert ("form-action 'self' https://chatgpt.com;"
                in page.headers["content-security-policy"])
        assert "*" not in page.headers["content-security-policy"]
        # no-referrer заставил бы браузер прислать на POST формы `Origin: null`
        assert page.headers["referrer-policy"] == "same-origin"
        for origin in ("https://elsewhere.example", "null"):
            bad = await c.post(url, data={"ticket": "test-ticket", "actor": "dot"},
                               headers={"Origin": origin})
            assert bad.status_code == 403
        good = await c.post(url, data={"ticket": "test-ticket", "actor": "dot"},
                            headers={"Origin": PUBLIC_ORIGIN}, follow_redirects=False)
        assert good.status_code == 303


@pytest.mark.asyncio
async def test_dashboard_to_mcp_internal_consent_round_trip(monkeypatch, sqlite_db):
    monkeypatch.setenv("ROOM_OAUTH_ENABLED", "1")
    monkeypatch.setenv("ROOM_OAUTH_ACTORS", "dot")
    monkeypatch.setenv("ROOM_OAUTH_KEY", "unit-test-room-key-not-production-0123456789")
    monkeypatch.setenv("INTERNAL_SECRET", "test-internal-secret")
    provider = RoomOAuthProvider()
    info = OAuthClientInformationFull(
        client_id="bridge-client", client_secret="test-secret",
        redirect_uris=[AnyUrl("https://chatgpt.com/oauth/callback")],
    )
    await provider.register_client(info)
    consent = await provider.authorize(info, AuthorizationParams(
        state="bridge-state", scopes=SCOPES, code_challenge="x" * 43,
        redirect_uri=AnyUrl("https://chatgpt.com/oauth/callback"),
        redirect_uri_provided_explicitly=True, resource=RESOURCE))
    ticket = parse_qs(urlparse(consent).query)["ticket"][0]

    async with _client() as mcp:
        async def forward(method: str, *, ticket: str, actor: str | None = None):
            headers = {"X-Internal-Secret": "test-internal-secret"}
            if method == "GET":
                return await mcp.get("/oauth/internal/consent", params={"ticket": ticket},
                                     headers=headers)
            return await mcp.post("/oauth/internal/consent",
                                  json={"ticket": ticket, "actor": actor}, headers=headers)

        monkeypatch.setattr(room_oauth_routes, "_internal", forward)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard_app),
                                     base_url=PUBLIC_ORIGIN) as dashboard:
            cookie, _ = issue_session()
            dashboard.cookies.set("vera3_session", cookie)
            page = await dashboard.get(f"/room/oauth/consent?ticket={ticket}")
            assert page.status_code == 200 and "bridge-client" in page.text
            approved = await dashboard.post(
                "/room/oauth/consent", data={"ticket": ticket, "actor": "dot"},
                headers={"Origin": PUBLIC_ORIGIN}, follow_redirects=False)
            assert approved.status_code == 303
            code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
    assert (await provider.load_authorization_code(info, code)).subject == "dot"


@pytest.mark.asyncio
async def test_sdk_dcr_pkce_http_flow(monkeypatch, sqlite_db):
    monkeypatch.setenv("ROOM_OAUTH_ENABLED", "1")
    monkeypatch.setenv("ROOM_OAUTH_ACTORS", "dot")
    monkeypatch.setenv("ROOM_OAUTH_KEY", "unit-test-room-key-not-production-0123456789")
    monkeypatch.setenv("INTERNAL_SECRET", "test-internal-secret")
    verifier = "test-verifier-" + "a" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()
                                      ).decode().rstrip("=")
    async with _client() as c:
        registered = await c.post("/register", json={
            "redirect_uris": ["https://chatgpt.com/oauth/callback"],
            "client_name": "ChatGPT test",
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": "room:read room:write",
        })
        assert registered.status_code == 201, registered.text
        cred = registered.json()
        started = await c.get("/authorize", params={
            "response_type": "code", "client_id": cred["client_id"],
            "redirect_uri": "https://chatgpt.com/oauth/callback",
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": "sdk-state", "scope": "room:read room:write",
            "resource": RESOURCE,
        }, follow_redirects=False)
        assert started.status_code in (302, 303), started.text
        ticket = parse_qs(urlparse(started.headers["location"]).query)["ticket"][0]
        approved = await c.post("/oauth/internal/consent",
                                json={"ticket": ticket, "actor": "dot"},
                                headers={"X-Internal-Secret": "test-internal-secret"})
        assert approved.status_code == 200
        code = parse_qs(urlparse(approved.json()["redirect_uri"]).query)["code"][0]
        token = await c.post("/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": "https://chatgpt.com/oauth/callback",
            "client_id": cred["client_id"], "client_secret": cred["client_secret"],
            "code_verifier": verifier, "resource": RESOURCE,
        })
        assert token.status_code == 200, token.text
        assert token.json()["access_token"]
        first = token.json()
        refreshed = await c.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
            "client_id": cred["client_id"], "client_secret": cred["client_secret"],
            "resource": RESOURCE,
        })
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["access_token"] != first["access_token"]
        revoked = await c.post("/revoke", data={
            "token": refreshed.json()["refresh_token"],
            "client_id": cred["client_id"], "client_secret": cred["client_secret"],
        })
        assert revoked.status_code == 200, revoked.text
        assert await RoomOAuthProvider().load_access_token(
            refreshed.json()["access_token"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_uri", [
    "http://chatgpt.com/oauth/callback",
    "https://chatgpt.com;script-src */cb",
    "https://user:pw@chatgpt.com/cb",
])
async def test_dashboard_consent_refuses_unsafe_callback_origin(monkeypatch, redirect_uri):
    monkeypatch.setenv("ROOM_OAUTH_ENABLED", "1")

    async def fake_internal(method: str, *, ticket: str, actor: str | None = None):
        return httpx.Response(200, json={
            "client_id": "c", "client_name": "x", "redirect_uri": redirect_uri,
            "scopes": SCOPES, "actors": ["dot"]})

    monkeypatch.setattr(room_oauth_routes, "_internal", fake_internal)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard_app),
                                 base_url=PUBLIC_ORIGIN) as c:
        cookie, _ = issue_session()
        c.cookies.set("vera3_session", cookie)
        page = await c.get("/room/oauth/consent?ticket=t")
    assert page.status_code == 400
    assert "chatgpt.com" not in page.headers["content-security-policy"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["[1, 2]", '"ticket"', "null", "{not json"])
async def test_internal_consent_rejects_non_object_body(monkeypatch, sqlite_db, payload):
    # тело-не-объект падало на body.get(...) → 500 вместо внятного отказа
    monkeypatch.setenv("ROOM_OAUTH_ENABLED", "1")
    monkeypatch.setenv("ROOM_OAUTH_KEY", "unit-test-room-key-not-production-0123456789")
    monkeypatch.setenv("INTERNAL_SECRET", "test-internal-secret")
    async with _client() as c:
        r = await c.post("/oauth/internal/consent", content=payload.encode(),
                         headers={"X-Internal-Secret": "test-internal-secret",
                                  "Content-Type": "application/json"})
    assert r.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"client_name": "x" * 201},
    {"redirect_uris": [AnyUrl(f"https://chatgpt.com/cb{i}") for i in range(6)]},
    {"redirect_uris": [AnyUrl("https://chatgpt.com/" + "a" * 500)]},
    {"client_uri": AnyUrl("https://chatgpt.com/" + "b" * 4000)},
])
async def test_dcr_rejects_oversized_client_metadata(monkeypatch, sqlite_db, overrides):
    monkeypatch.setenv("ROOM_OAUTH_KEY", "unit-test-room-key-not-production-0123456789")
    fields = {"client_id": "big", "client_secret": "test-secret",
              "redirect_uris": [AnyUrl("https://chatgpt.com/oauth/callback")], **overrides}
    with pytest.raises(RegistrationError, match="too large"):
        await RoomOAuthProvider().register_client(OAuthClientInformationFull(**fields))


@pytest.mark.asyncio
async def test_dcr_accepts_normal_chatgpt_client(monkeypatch, sqlite_db):
    monkeypatch.setenv("ROOM_OAUTH_KEY", "unit-test-room-key-not-production-0123456789")
    info = OAuthClientInformationFull(
        client_id="normal", client_secret="test-secret", client_name="ChatGPT",
        token_endpoint_auth_method="client_secret_post",
        redirect_uris=[AnyUrl("https://chatgpt.com/connector_platform_oauth_redirect")])
    await RoomOAuthProvider().register_client(info)
    assert (await RoomOAuthProvider().get_client("normal")).client_name == "ChatGPT"


_ROOM_KEY = "unit-test-room-key-not-production-0123456789"
_STRONG = "s" * 40


@pytest.mark.parametrize(("actors", "env_name", "env_value"), [
    ("dot,claude", "ROOM_TOKENS", f"claude:{_STRONG}"),
    ("codex", "MCP_TOKENS", f"codex:{_STRONG}x"),
])
def test_oauth_actor_must_not_reuse_a_static_token_name(monkeypatch, actors, env_name,
                                                        env_value):
    # OAuth-клиент с именем статического агента писал бы в комнату как он:
    # история, курсоры и аренды перестали бы различать их (ревью, F5 CLI)
    from vera_mcp.room_oauth import validate_config

    monkeypatch.setenv("ROOM_OAUTH_ENABLED", "1")
    monkeypatch.setenv("ROOM_OAUTH_KEY", _ROOM_KEY)
    monkeypatch.setenv("ROOM_OAUTH_ACTORS", actors)
    monkeypatch.setenv(env_name, env_value)
    with pytest.raises(ValueError, match="already used by a static token"):
        validate_config()


def test_oauth_actor_distinct_from_static_names_starts(monkeypatch):
    from vera_mcp.room_oauth import validate_config

    monkeypatch.setenv("ROOM_OAUTH_ENABLED", "1")
    monkeypatch.setenv("ROOM_OAUTH_KEY", _ROOM_KEY)
    monkeypatch.setenv("ROOM_OAUTH_ACTORS", "dot")
    monkeypatch.setenv("ROOM_TOKENS", f"claude:{_STRONG},codex:{_STRONG}y")
    monkeypatch.setenv("MCP_TOKENS", f"claude:{_STRONG}z")
    validate_config()
