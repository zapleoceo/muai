"""Граница LLM: секрет не уходит провайдеру ни одним путём (маскирует client.py).

CANARY — синтетический токен, не настоящий секрет. Тесты подменяют слой HTTP
брокера (`*_via_broker`) и проверяют то, что реально улетело бы наружу.
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from vera_shared.llm import client
from vera_shared.llm.broker_client import BrokerCallFailed, _short
from vera_shared.llm.client import chat_async, embed
from vera_shared.llm.redact_boundary import redact_messages

os.environ.setdefault("TOKEN_SECRET", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")

CANARY = "ghp_" + "CANARY" * 6
BODY = f"deploy notes token={CANARY} ok"
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"


@pytest.fixture
def wire(monkeypatch):
    """Что ушло бы брокеру: {'chat': [messages...], 'embed': [input...]}."""
    sent: dict[str, list[Any]] = {"chat": [], "embed": []}

    async def fake_chat(**kw):
        sent["chat"].append(kw["messages"])
        return "{}", {}

    async def fake_embed(text):
        sent["embed"].append(text)
        return [[0.0]]

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(client, "broker_enabled", lambda: True)
    monkeypatch.setattr(client, "chat_async_via_broker", fake_chat)
    monkeypatch.setattr(client, "chat_via_broker", fake_chat)
    monkeypatch.setattr(client, "embed_via_broker", fake_embed)
    for name in ("_circuit_precheck", "_circuit_reset", "_circuit_note"):
        monkeypatch.setattr(client, name, noop)
    return sent


def _wire_text(sent: dict[str, list[Any]]) -> str:
    return json.dumps(sent, ensure_ascii=False)


@pytest.mark.asyncio
async def test_chat_async_masks_string_and_text_parts_leaves_images(wire):
    messages = [
        {"role": "user", "content": BODY},
        {"role": "user", "content": [{"type": "text", "text": BODY},
                                     {"type": "image_url", "image_url": {"url": PNG}}]},
    ]
    await chat_async(messages)
    out = wire["chat"][0]
    assert CANARY not in _wire_text(wire)
    assert out[1]["content"][1] == {"type": "image_url", "image_url": {"url": PNG}}


@pytest.mark.asyncio
async def test_chat_async_does_not_mutate_callers_messages(wire):
    parts = [{"type": "text", "text": BODY}]
    messages = [{"role": "user", "content": BODY}, {"role": "user", "content": parts}]
    await chat_async(messages)
    assert messages[0]["content"] == BODY and parts[0]["text"] == BODY


@pytest.mark.asyncio
async def test_redact_input_false_passes_raw(wire):
    await chat_async([{"role": "user", "content": BODY}], redact_input=False)
    assert CANARY in _wire_text(wire)


@pytest.mark.asyncio
async def test_embedding_input_is_masked(wire):
    await embed(BODY)
    await embed([BODY, "plain"])
    assert CANARY not in _wire_text(wire)
    assert wire["embed"][1][1] == "plain"


def test_redact_messages_keeps_non_text_content():
    msgs = [{"role": "user", "content": None}, {"role": "tool", "tool_call_id": "1"}]
    assert redact_messages(msgs) == msgs


@pytest.mark.asyncio
async def test_brain_search_history_and_synthesis_prompt(wire):
    from brain_search.models import HistoryItem
    from brain_search.synthesis import _history_block, build_prompt
    prompt = build_prompt(question="q", self_ctx="", context=BODY, notes="",
                          history_block=_history_block([HistoryItem(role="user", content=BODY)], "x"))
    assert CANARY in prompt
    await chat_async([{"role": "user", "content": prompt}], capability="chat:smart")
    assert CANARY not in _wire_text(wire)


@pytest.mark.asyncio
async def test_triage_single_and_batch_prompt(wire, monkeypatch):
    from brain_triage import triage_calls
    from brain_triage.prompts import build_batch_prompt
    monkeypatch.setattr(triage_calls, "resolve_triage_capability", AsyncMock(return_value="chat:fast"))
    row = SimpleNamespace(id=1, source="telegram", account="a", occurred_at=None, content_text=BODY)
    assert CANARY in build_batch_prompt([row])
    await chat_async([{"role": "user", "content": build_batch_prompt([row])}])
    with contextlib.suppress(Exception):  # разбор ответа-заглушки не предмет теста
        await triage_calls.triage_one(row)
    assert len(wire["chat"]) == 2 and CANARY not in _wire_text(wire)


@pytest.mark.asyncio
async def test_rel_extract_prompt(wire, sqlite_db, monkeypatch):
    from vera_shared.graph.rel_extract import extract_and_store
    await extract_and_store(1, BODY)
    assert wire["chat"] and CANARY not in _wire_text(wire)


@pytest.mark.asyncio
async def test_pair_roles_evidence_prompt(wire):
    from vera_shared.graph.pair_roles_prompt import render_prompt
    from vera_shared.graph.pair_roles_types import Evidence, PackMessage, PairSide
    ev = Evidence(PairSide("A", 1, "Игорь", True), PairSide("B", 2, "Виктор"), {},
                  (PackMessage("m1", "2026-03-01", "dm", "B", BODY),), "d")
    prompt = render_prompt(ev)
    assert CANARY in prompt
    await chat_async([{"role": "user", "content": prompt}], capability="structured")
    assert CANARY not in _wire_text(wire)


@pytest.mark.asyncio
async def test_fold_and_voice_distill(wire):
    from gateway.voice import Utterance
    from gateway.voice_distill import distill
    await distill([Utterance(at=0.0, stream="mic", text=BODY)], app="x", title="t")
    assert wire["chat"] and CANARY not in _wire_text(wire)


@pytest.mark.asyncio
async def test_claude_distill_goes_through_fold(wire):
    from gateway.claude_distill import distill
    turns = [{"role": "user", "text": BODY, "content": BODY, "at": 0.0}]
    await distill(turns, project="p", branch="b")
    assert CANARY not in _wire_text(wire)


def test_broker_error_body_is_masked_in_message():
    assert CANARY not in _short(f"upstream echoed {BODY}")


def test_save_event_log_masks_upstream_body(caplog):
    from bot_telegram.brain import save_event
    reply = MagicMock(status_code=500, text=BODY)
    http = MagicMock()
    http.__aenter__.return_value.post = AsyncMock(return_value=reply)
    import asyncio
    with patch("bot_telegram.brain.httpx.AsyncClient", return_value=http), \
            caplog.at_level(logging.WARNING):
        asyncio.run(save_event(1, 1, "user", "hi"))
    assert "HTTP 500" in caplog.text and CANARY not in caplog.text


def test_search_proxy_log_masks_upstream_body(caplog):
    from dashboard.app import app
    from dashboard.auth import COOKIE_NAME
    from dashboard.auth_routes import _set_session_cookie
    from fastapi.testclient import TestClient
    from starlette.responses import Response
    resp = Response()
    _set_session_cookie(resp)
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    reply = MagicMock(status_code=502, text=BODY)
    http = MagicMock()
    http.__aenter__.return_value.post = AsyncMock(return_value=reply)
    with patch("dashboard.search_routes.httpx.AsyncClient", return_value=http), \
            caplog.at_level(logging.WARNING):
        TestClient(app).post("/search-ui", data={"q": "x"}, cookies={COOKIE_NAME: cookie})
    assert "HTTP 502" in caplog.text and CANARY not in caplog.text


@pytest.mark.asyncio
async def test_broker_failure_still_wrapped(wire, monkeypatch):
    monkeypatch.setattr(client, "chat_async_via_broker",
                        AsyncMock(side_effect=BrokerCallFailed("x")))
    with pytest.raises(client.LLMCallFailed):
        await chat_async([{"role": "user", "content": "hi"}])
