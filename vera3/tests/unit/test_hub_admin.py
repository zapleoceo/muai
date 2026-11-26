"""Админка MCP-хаба (/hub) и атрибуция агента за хабом в /mcp."""
from __future__ import annotations

import pytest
from dashboard.app import app
from dashboard.auth import COOKIE_NAME
from fastapi.testclient import TestClient
from hub_support import HUB_KEY, SECRET
from vera_hub.auth import AgentAuth
from vera_mcp.hub_delegate import delegated_client
from vera_shared.hub.repo_agents import list_agents
from vera_shared.hub.repo_upstreams import list_upstreams

client = TestClient(app)
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
UPSTREAM = {"project": "aib", "slug": "video", "title": "Broker video", "transport": "http",
            "url": "https://aib.example/mcp/video", "auth_kind": "bearer", "enabled": "on",
            "timeout_s": "30", "max_concurrency": "2", "secret": SECRET}


def _cookie() -> dict[str, str]:
    from dashboard.auth_routes import _set_session_cookie
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    return {COOKIE_NAME: resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("HUB_SECRET_KEY", HUB_KEY)


def test_hub_needs_the_owner():
    assert client.get("/hub", follow_redirects=False).status_code == 303
    assert client.post("/hub/upstream", data=UPSTREAM, headers=SAME_ORIGIN).status_code == 401
    assert client.post("/hub/agent", data={"name": "x"}, headers=SAME_ORIGIN).status_code == 401


def test_cross_site_post_is_refused_for_the_owner():
    r = client.post("/hub/upstream", data=UPSTREAM, cookies=_cookie(),
                    headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_owner_adds_upstream_and_secret_is_never_shown(sqlite_db):
    r = client.post("/hub/upstream", data=UPSTREAM, cookies=_cookie(), headers=SAME_ORIGIN,
                    follow_redirects=False)
    assert r.status_code == 303
    stored = (await list_upstreams())[0]
    assert stored.secret_enc.startswith("enc1:")
    page = client.get("/hub", cookies=_cookie())
    assert page.status_code == 200
    assert SECRET not in page.text and stored.secret_enc not in page.text
    assert "aib__video__*" in page.text and "задан" in page.text
    assert page.headers["cache-control"] == "no-store"
    bad = client.post("/hub/upstream", data=UPSTREAM | {"slug": "Bad!"}, cookies=_cookie(),
                      headers=SAME_ORIGIN)
    assert bad.status_code == 400 and SECRET not in bad.text
    nan = client.post("/hub/upstream", data=UPSTREAM | {"timeout_s": "x"}, cookies=_cookie(),
                      headers=SAME_ORIGIN)
    assert nan.status_code == 400


@pytest.mark.asyncio
async def test_owner_creates_agent_sees_token_once_and_rotates(sqlite_db):
    r = client.post("/hub/agent", cookies=_cookie(), headers=SAME_ORIGIN,
                    data={"name": "dot", "allow": ["sniffer", "vera/room"], "note": "Dot"})
    assert r.status_code == 200 and "vhub_" in r.text
    token = r.text.split("<pre class='token'>")[1].split("</pre>")[0]
    agent = (await list_agents())[0]
    assert agent.allow == ["sniffer", "vera/room"]
    page = client.get("/hub", cookies=_cookie()).text
    assert token not in page and agent.token_prefix in page
    upd = client.post("/hub/agent", cookies=_cookie(), headers=SAME_ORIGIN, follow_redirects=False,
                      data={"id": str(agent.id), "allow": ["aib"], "note": "x"})
    assert upd.status_code == 303
    assert (await list_agents())[0].enabled is False and (await list_agents())[0].allow == ["aib"]
    rot = client.post("/hub/agent/rotate", cookies=_cookie(), headers=SAME_ORIGIN,
                      data={"id": str(agent.id)})
    assert rot.status_code == 200 and token not in rot.text and "vhub_" in rot.text
    missing = client.post("/hub/agent/rotate", cookies=_cookie(), headers=SAME_ORIGIN,
                          data={"id": "999"})
    assert missing.status_code == 400
    dup = client.post("/hub/agent", cookies=_cookie(), headers=SAME_ORIGIN, data={"name": "dot"})
    assert dup.status_code == 400


def test_delegated_client_only_for_hub_token(monkeypatch):
    hdr = [(b"x-vera-hub-agent", b"dot")]
    assert delegated_client("hub", hdr) == "hub/dot"
    assert delegated_client("claude", hdr) == "claude", "чужой токен заголовком не меняется"
    assert delegated_client("hub", [(b"x-vera-hub-agent", b"Evil/../x")]) == "hub"
    assert delegated_client("hub", []) == "hub"
    monkeypatch.setenv("HUB_DELEGATE_CLIENTS", "gateway")
    assert delegated_client("hub", hdr) == "hub"


@pytest.mark.asyncio
async def test_agent_auth_caches_and_rejects_bad_headers():
    lookups: list[str] = []
    touched: list[int] = []

    class Row:
        id, name, allow = 1, "probe", ["sniffer"]

    async def lookup(token):
        lookups.append(token)
        return Row() if token == "good" else None

    async def touch(agent_id):
        touched.append(agent_id)
    auth = AgentAuth(lookup, touch)
    assert (await auth.resolve("Bearer good")).allow == ("sniffer",)
    assert (await auth.resolve("Bearer good")).name == "probe"
    assert lookups == ["good"] and touched == [1]
    assert await auth.resolve("Bearer bad") is None
    assert await auth.resolve("Bearer bad") is None
    assert lookups == ["good", "bad"]
    for header in (None, "", "Basic good", "Bearer ", "Bearer " + "x" * 300):
        assert await auth.resolve(header) is None
