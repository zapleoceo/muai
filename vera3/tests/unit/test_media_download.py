"""ingestor_telegram.media_download — size cap BEFORE download + timeout.

Fixes the 2026-07-19 OOM: oversize media used to be fully buffered into RAM
before the 25 MB check; a stuck download orphaned a coroutine that leaked."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "ingestor-telegram", "src"))

from ingestor_telegram.media_download import (  # noqa: E402
    MAX_MEDIA_BYTES,
    MediaDownloadTimeout,
    MediaTooLarge,
    download_capped,
)


def _msg(*, file_size, payload=b"data", hang=False):
    """Fake Telethon message: .file.size from metadata + async download_media."""
    calls = {"downloaded": False}

    async def download_media(file=None):
        calls["downloaded"] = True
        if hang:
            await asyncio.sleep(10)
        return payload

    file = None if file_size is None else SimpleNamespace(size=file_size)
    msg = SimpleNamespace(file=file, download_media=download_media)
    return msg, calls


@pytest.mark.asyncio
async def test_oversize_rejected_without_downloading():
    msg, calls = _msg(file_size=MAX_MEDIA_BYTES + 1)
    with pytest.raises(MediaTooLarge) as exc:
        await download_capped(msg)
    assert calls["downloaded"] is False          # НИ байта не скачано
    assert exc.value.size == MAX_MEDIA_BYTES + 1


@pytest.mark.asyncio
async def test_under_cap_downloads_and_returns_bytes():
    msg, calls = _msg(file_size=1000, payload=b"hello")
    assert await download_capped(msg) == b"hello"
    assert calls["downloaded"] is True


@pytest.mark.asyncio
async def test_unknown_size_still_capped_after_download():
    # msg.file.size отсутствует → скачиваем, но пост-проверка ловит перебор
    big = b"x" * (MAX_MEDIA_BYTES + 10)
    msg, _ = _msg(file_size=None, payload=big)
    with pytest.raises(MediaTooLarge):
        await download_capped(msg)


@pytest.mark.asyncio
async def test_unknown_size_small_ok():
    msg, _ = _msg(file_size=None, payload=b"tiny")
    assert await download_capped(msg) == b"tiny"


@pytest.mark.asyncio
async def test_none_payload_returns_none():
    msg, _ = _msg(file_size=100, payload=None)
    assert await download_capped(msg) is None


@pytest.mark.asyncio
async def test_stuck_download_times_out():
    msg, _ = _msg(file_size=100, hang=True)
    with pytest.raises(MediaDownloadTimeout):
        await download_capped(msg, timeout_s=0.05)
