"""MCP-хаб: имена и права, проверка настроек, шифрование секретов, репозитории, CLI."""
from __future__ import annotations

import io

import pytest
from hub_support import HUB_KEY, SECRET
from sqlalchemy import select
from vera_shared.db.models_hub import HubAgentRow, HubUpstreamRow, upstream_dict
from vera_shared.hub import names
from vera_shared.hub.names import (
    HubNameError,
    allowed,
    normalize_allow,
    split_tool_name,
)
from vera_shared.hub.repo_agents import (
    agent_by_token,
    create_agent,
    insert_call,
    list_agents,
    recent_calls,
    rotate_agent,
    touch_agent,
    update_agent,
)
from vera_shared.hub.repo_upstreams import list_upstreams, save_upstream, set_health
from vera_shared.hub.secrets import HubKeyMissing, hub_key, seal, unseal
from vera_shared.hub.spec import UpstreamSpec


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("HUB_SECRET_KEY", HUB_KEY)


def test_tool_names_round_trip_and_reject_foreign():
    full = names.tool_name("sniffer", "catalog", "search_listings")
    assert full == "sniffer__catalog__search_listings"
    assert split_tool_name(full) == ("sniffer", "catalog", "search_listings")
    assert split_tool_name("vera__room__a__b") == ("vera", "room", "a__b")
    for bad in ("", "x", "nope__catalog__a", "vera__Bad_slug__a", "vera____a"):
        assert split_tool_name(bad) is None


def test_grants_are_explicit():
    assert normalize_allow([" Sniffer ", "vera/room", "", "vera/room"]) == ["sniffer", "vera/room"]
    for bad in ("*", "unknown", "vera/Room!", "vera/a"):
        with pytest.raises(HubNameError):
            normalize_allow([bad])
    assert allowed(["sniffer"], "sniffer", "personal")
    assert allowed(["vera/room"], "vera", "room")
    assert not allowed(["vera/room"], "vera", "memory")


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"project": "nope"}, "unknown project"),
    ({"slug": "A"}, "slug"),
    ({"timeout_s": 0}, "timeout_s"),
    ({"max_concurrency": 99}, "max_concurrency"),
    ({"transport": "ws"}, "transport"),
    ({"auth_kind": "env"}, "http auth_kind"),
    ({"url": "ftp://x"}, "url must"),
    ({"url": "https://u:p@x/"}, "credentials"),
    ({"url": ""}, "needs a url"),
    ({"auth_kind": "header", "auth_header": "Host"}, "auth_header"),
    ({"transport": "stdio", "auth_kind": "bearer"}, "stdio auth_kind"),
    ({"transport": "stdio", "command": []}, "command"),
    ({"transport": "stdio", "command": ["x"], "auth_kind": "env", "auth_header": "bad"}, "env"),
])
def test_spec_validation(kwargs, message):
    base = {"project": "aib", "slug": "video", "url": "https://aib.example/mcp/video"}
    with pytest.raises(HubNameError, match=message):
        UpstreamSpec(**(base | kwargs)).validate()


def test_spec_accepts_waiting_entries():
    UpstreamSpec("itstep", "crm", url="", auth_kind="url").validate()
    UpstreamSpec("personal", "google", url="", enabled=False).validate()
    UpstreamSpec("personal", "tg", transport="stdio", command=["tg-mcp"], auth_kind="env",
                 auth_header="TG_SESSION").validate()


def test_seal_requires_dedicated_key(monkeypatch):
    sealed = seal(SECRET)
    assert sealed.startswith("enc1:") and SECRET not in sealed
    assert unseal(sealed) == SECRET
    monkeypatch.setenv("HUB_SECRET_KEY", "short")
    with pytest.raises(HubKeyMissing):
        hub_key()


@pytest.mark.asyncio
async def test_upstream_secret_is_stored_sealed_and_write_only(sqlite_db):
    spec = UpstreamSpec("aib", "video", url="https://aib.example/mcp/video", auth_kind="bearer")
    uid = await save_upstream(spec, secret=SECRET)
    stored = (await list_upstreams())[0]
    assert stored.secret_enc.startswith("enc1:") and SECRET not in stored.secret_enc
    shown = upstream_dict(stored)
    assert shown["has_secret"] is True and "secret_enc" not in shown
    await save_upstream(spec, uid, secret=None)
    assert (await list_upstreams())[0].secret_enc == stored.secret_enc, "пусто = прежний секрет"
    await save_upstream(spec, uid, clear_secret=True)
    assert (await list_upstreams())[0].secret_enc is None
    await set_health(uid, "ok", "", 3)
    await set_health(999, "ok")
    assert (await list_upstreams())[0].tool_count == 3
    with pytest.raises(LookupError):
        await save_upstream(spec, 999)


@pytest.mark.asyncio
async def test_agent_token_shown_once_and_stored_hashed(sqlite_db):
    token = await create_agent("probe", ["sniffer/catalog"], "test")
    assert token.startswith("vhub_")
    async with sqlite_db() as session:
        stored = (await session.execute(select(HubAgentRow))).scalar_one()
    assert token not in repr(vars(stored)) and len(stored.token_hash) == 64
    found = await agent_by_token(token)
    assert found is not None and found.allow == ["sniffer/catalog"]
    with pytest.raises(HubNameError):
        await create_agent("probe", [])
    with pytest.raises(HubNameError):
        await create_agent("Bad Name", [])
    await update_agent(found.id, ["vera"], False, "off")
    assert await agent_by_token(token) is None, "выключенный агент не входит"
    await update_agent(found.id, ["vera"], True, "on")
    fresh = await rotate_agent(found.id)
    assert await agent_by_token(token) is None and (await agent_by_token(fresh)).name == "probe"
    await touch_agent(found.id)
    await touch_agent(999)
    assert (await list_agents())[0].last_seen_at is not None
    for missing in (update_agent(999, [], True, ""), rotate_agent(999)):
        with pytest.raises(LookupError):
            await missing


@pytest.mark.asyncio
async def test_call_log_round_trip(sqlite_db):
    await insert_call({"agent": "probe", "method": "tools/call", "tool": "a__b__c",
                       "status": "ok", "duration_ms": 5})
    assert [c.tool for c in await recent_calls()] == ["a__b__c"]


@pytest.mark.asyncio
async def test_cli_stores_secret_from_stdin_and_manages_agents(sqlite_db, monkeypatch, capsys):
    from vera_hub import cli
    async with sqlite_db() as session:
        session.add(HubUpstreamRow(project="vera", slug="room", url="http://mcp:8000/mcp",
                                   auth_kind="bearer", command=[]))

    async def _noop(*_a, **_k):
        return None
    monkeypatch.setattr(cli, "init_engine", _noop)
    monkeypatch.setattr(cli, "close_engine", _noop)
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    assert "encrypted" in await cli._run(cli._parser().parse_args(["secret", "vera/room"]))
    assert unseal((await list_upstreams())[0].secret_enc) == SECRET
    token = await cli._run(cli._parser().parse_args(["agent-create", "probe", "sniffer/catalog"]))
    assert token.startswith("vhub_")
    await cli._run(cli._parser().parse_args(["agent-set", "probe", "--disable"]))
    assert (await list_agents())[0].enabled is False
    await cli._run(cli._parser().parse_args(["agent-set", "probe", "--enable", "--allow", "vera"]))
    assert (await list_agents())[0].allow == ["vera"]
    with pytest.raises(SystemExit):
        await cli._run(cli._parser().parse_args(["agent-set", "ghost"]))
    with pytest.raises(SystemExit):
        await cli._run(cli._parser().parse_args(["secret", "vera/none"]))
