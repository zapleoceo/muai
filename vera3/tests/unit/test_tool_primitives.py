"""vera_shared.tools - примитивы инструмента (Tool, FunctionTool, HTTPTool).

Реестр (ToolRegistry/default_registry) удалён 2026-11: в нём не было ни
одной регистрации и ни одного читателя (инструменты агента brain-search
живут в brain_search/agent_tools.py).

Важное в HTTPTool - ошибка удалённого сервиса приходит словарём, а не
исключением: агент разбирает результат как данные.
"""
from __future__ import annotations

import pytest
from vera_shared.tools import FunctionTool, Tool, ToolSpec


def _spec(name: str = "echo") -> ToolSpec:
    return ToolSpec(name=name, description="эхо",
                    params_schema={"type": "object",
                                   "properties": {"text": {"type": "string"}}})


def test_tool_abc_requires_exec():
    """Голый Tool инстанцировать нельзя — иначе можно зарегистрировать
    объект без exec и узнать об этом только в рантайме агента."""
    with pytest.raises(TypeError):
        Tool()          # type: ignore[abstract]


@pytest.mark.asyncio
async def test_function_tool_passes_params_through_by_name():
    got = {}

    async def fn(*, chat_id: int, limit: int = 10):
        got.update(chat_id=chat_id, limit=limit)
        return {"ok": True}

    tool = FunctionTool(_spec("dump"), fn)

    assert await tool.exec(chat_id=42) == {"ok": True}
    assert got == {"chat_id": 42, "limit": 10}


# ─── HTTPTool: тот же контракт, но через сеть ───────────────────────────────
# Агент не должен отличать локальный инструмент от удалённого, поэтому и
# ошибки удалённого обязаны приходить словарём, а не исключением.


@pytest.mark.asyncio
async def test_http_tool_posts_to_derived_url_and_returns_json(respx_mock):
    from vera_shared.tools import HTTPTool

    route = respx_mock.post("http://ub:8000/tools/list_dialogs").respond(
        200, json={"count": 3})
    tool = HTTPTool(_spec("telegram.list_dialogs"), "http://ub:8000/")

    assert await tool.exec(limit=200) == {"count": 3}
    # имя с точкой режется по первой: агент видит telegram.list_dialogs,
    # а удалённый сервис публикует /tools/list_dialogs
    assert route.called
    assert route.calls[0].request.url.path == "/tools/list_dialogs"


@pytest.mark.asyncio
async def test_http_tool_sends_internal_secret(respx_mock, monkeypatch):
    import vera_shared.tools.http_client as hc
    from vera_shared.tools import HTTPTool

    monkeypatch.setattr(hc, "INTERNAL_SECRET", "s3cret")
    route = respx_mock.post("http://ub:8000/tools/x").respond(200, json={})

    await HTTPTool(_spec("x"), "http://ub:8000").exec()

    assert route.calls[0].request.headers["X-Internal-Secret"] == "s3cret"


@pytest.mark.asyncio
async def test_http_tool_error_status_becomes_error_dict(respx_mock):
    from vera_shared.tools import HTTPTool

    respx_mock.post("http://ub:8000/tools/x").respond(503, text="ingestor busy")

    out = await HTTPTool(_spec("x"), "http://ub:8000").exec()

    assert out["error"] == "HTTP 503"
    assert "ingestor busy" in out["body"]
