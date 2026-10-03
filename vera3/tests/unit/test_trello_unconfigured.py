"""Trello без ключей: один WARNING и тишина, а не ERROR каждые 10 минут."""
from __future__ import annotations

import logging
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "ingestor-trello", "src"))

from ingestor_trello import client, poller  # noqa: E402


class _Idle(Exception):
    """Вместо вечного ожидания: Event().wait() достигнут."""


class _FakeEvent:
    async def wait(self) -> None:
        raise _Idle


@pytest.mark.parametrize("env", [
    {}, {"TRELLO_API_KEY": "k"}, {"TRELLO_TOKEN": "t"},
])
def test_credentials_missing(monkeypatch, env):
    monkeypatch.delenv("TRELLO_API_KEY", raising=False)
    monkeypatch.delenv("TRELLO_TOKEN", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert not client.credentials_configured()


def test_credentials_present(monkeypatch):
    monkeypatch.setenv("TRELLO_API_KEY", "k")
    monkeypatch.setenv("TRELLO_TOKEN", "t")
    assert client.credentials_configured()


@pytest.mark.asyncio
async def test_main_loop_idles_with_single_warning(monkeypatch, caplog):
    monkeypatch.delenv("TRELLO_API_KEY", raising=False)
    monkeypatch.delenv("TRELLO_TOKEN", raising=False)
    init = AsyncMock()
    polling = AsyncMock()
    with (patch.object(poller, "init_engine", init),
          patch.object(poller, "poll_forever", polling),
          patch.object(poller.asyncio, "Event", _FakeEvent),
          caplog.at_level(logging.WARNING, logger="trello"),
          pytest.raises(_Idle)):
        await poller.main_loop()
    init.assert_not_awaited()
    polling.assert_not_awaited()
    records = [r for r in caplog.records if r.name == "trello"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
