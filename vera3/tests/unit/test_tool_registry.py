"""ToolRegistry — то, через что агент вызывает инструменты.

Пакет `vera_shared/tools` стоял на 0% покрытия, причём ВНУТРИ измеряемого
гейтом `vera_shared`: репозиторный порог — среднее, поэтому нулевой пакет
ехал бесплатно на хорошо покрытых соседях. Ровно тот случай, ради которого
пороги теперь по пакетам (scripts/check_coverage.py).

Важное здесь — как реестр ведёт себя при СБОЕ: `exec()` обязан вернуть
словарь с ошибкой, а не бросить. Агент разбирает результат как данные, и
исключение оттуда уронило бы весь его цикл вместо одного шага.
"""
from __future__ import annotations

import asyncio

import pytest
from vera_shared.tools import FunctionTool, Tool, ToolRegistry, ToolSpec
from vera_shared.tools.registry import default_registry


def _spec(name: str = "echo") -> ToolSpec:
    return ToolSpec(name=name, description="эхо",
                    params_schema={"type": "object",
                                   "properties": {"text": {"type": "string"}}})


def _echo_tool(name: str = "echo") -> FunctionTool:
    async def fn(**params):
        return {"got": params}
    return FunctionTool(_spec(name), fn)


@pytest.mark.asyncio
async def test_register_get_and_exec():
    reg = ToolRegistry()
    reg.register(_echo_tool())

    assert reg.all_names() == ["echo"]
    assert reg.get("echo") is not None
    assert await reg.exec("echo", {"text": "привет"}) == {"got": {"text": "привет"}}


@pytest.mark.asyncio
async def test_unknown_tool_returns_error_not_raises():
    """Агент разбирает результат как данные — бросок уронил бы весь цикл."""
    reg = ToolRegistry()
    reg.register(_echo_tool())

    out = await reg.exec("нет-такого", {})

    assert "unknown tool" in out["error"]
    assert out["available"] == ["echo"]


@pytest.mark.asyncio
async def test_tool_exception_is_returned_as_error():
    reg = ToolRegistry()

    async def boom(**_):
        raise ValueError("сломалось")

    reg.register(FunctionTool(_spec("bad"), boom))

    out = await reg.exec("bad", {})

    assert out["error"] == "ValueError: сломалось"


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_reported():
    """Инструмент ходит наружу (HTTP к юзерботу), и без потолка один зависший
    вызов держал бы шаг агента до самого конца его дедлайна."""
    reg = ToolRegistry()

    async def slow(**_):
        await asyncio.sleep(3600)

    reg.register(FunctionTool(_spec("slow"), slow))

    out = await reg.exec("slow", {}, timeout=0.05)

    assert "timed out after 0.05s" in out["error"]


@pytest.mark.asyncio
async def test_reregistration_overwrites_and_warns(caplog):
    reg = ToolRegistry()
    reg.register(_echo_tool())

    async def other(**_):
        return {"v": 2}

    with caplog.at_level("WARNING"):
        reg.register(FunctionTool(_spec("echo"), other))

    assert "already registered" in caplog.text
    assert await reg.exec("echo", {}) == {"v": 2}
    assert reg.all_names() == ["echo"], "перерегистрация не должна плодить запись"


def test_specs_filtered_by_prefix():
    reg = ToolRegistry()
    for n in ("tg_dialogs", "tg_members", "graph_lookup"):
        reg.register(_echo_tool(n))

    assert {s.name for s in reg.specs()} == {"tg_dialogs", "tg_members", "graph_lookup"}
    assert {s.name for s in reg.specs(prefix="tg_")} == {"tg_dialogs", "tg_members"}
    assert reg.specs(prefix="нет_") == []


def test_missing_tool_is_none_not_keyerror():
    assert ToolRegistry().get("нет") is None


def test_default_registry_is_process_wide_singleton():
    assert default_registry() is default_registry()


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

    reg = ToolRegistry()
    reg.register(FunctionTool(_spec("dump"), fn))

    assert await reg.exec("dump", {"chat_id": 42}) == {"ok": True}
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
    # имя с точкой режется по первой: реестр видит telegram.list_dialogs,
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


@pytest.mark.asyncio
async def test_http_tool_failure_surfaces_through_registry_as_data(respx_mock):
    """Сеть отвалилась — реестр обязан вернуть словарь, а не уронить шаг агента."""
    import httpx as _httpx
    from vera_shared.tools import HTTPTool

    respx_mock.post("http://ub:8000/tools/x").mock(
        side_effect=_httpx.ConnectError("нет связи"))

    reg = ToolRegistry()
    reg.register(HTTPTool(_spec("x"), "http://ub:8000"))

    out = await reg.exec("x", {})

    assert out["error"].startswith("ConnectError")
