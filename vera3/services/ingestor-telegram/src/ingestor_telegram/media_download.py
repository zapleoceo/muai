"""Bounded media download — the single choke point for pulling Telegram
media into memory.

Root cause of the 2026-07-19 OOM: every download site called
`download_media(file=bytes)`, which buffers the WHOLE file into RAM, and
only checked the 25 MB limit AFTER the download finished. Telegram audio
(podcasts/music) can be up to 2 GB; media-worker requests such a file for
whisper, the endpoint buffers it whole, and the box (3.7 GB, shared with
other projects) OOMs. Worse: media-worker gives up after its 60 s HTTP
timeout, but the server-side download coroutine keeps running, buffering
into memory nothing consumes anymore.

Fix: gate on `msg.file.size` (Telegram sends it in message metadata — no
bytes transferred) BEFORE downloading, and bound the download with a
wall-clock timeout so a stuck chunk-loop can't leak forever.
"""
from __future__ import annotations

import asyncio

MAX_MEDIA_BYTES = 25 * 1024 * 1024        # Whisper hard limit; also the RAM guard
# Kept just under media-worker's 60s HTTP timeout so a stuck download cancels
# itself server-side instead of orphaning a coroutine that keeps buffering
# after the client has already given up (the exact 2026-07-19 leak).
DOWNLOAD_TIMEOUT_S = 55.0


class MediaTooLarge(Exception):
    """Media exceeds the byte cap — refused without downloading."""

    def __init__(self, size: int):
        self.size = size
        super().__init__(f"too large: {size} bytes (>{MAX_MEDIA_BYTES} limit)")


class MediaDownloadTimeout(Exception):
    """Download did not finish within the wall-clock bound."""

    def __init__(self, timeout_s: float):
        self.timeout_s = timeout_s
        super().__init__(f"download timed out after {timeout_s:.0f}s")


async def download_capped(
    msg,
    *,
    max_bytes: int = MAX_MEDIA_BYTES,
    timeout_s: float = DOWNLOAD_TIMEOUT_S,
) -> bytes | None:
    """Download msg media into memory, size-capped and time-bounded.

    Rejects oversize via `msg.file.size` BEFORE any bytes move (raises
    MediaTooLarge). Bounds the transfer with a timeout (MediaDownloadTimeout).
    Returns the bytes, or None when there's nothing to download.
    """
    known = getattr(getattr(msg, "file", None), "size", None)
    if known is not None and known > max_bytes:
        raise MediaTooLarge(known)
    try:
        data = await asyncio.wait_for(msg.download_media(file=bytes), timeout_s)
    except asyncio.TimeoutError as e:
        raise MediaDownloadTimeout(timeout_s) from e
    if data is None:
        return None
    # Belt-and-suspenders: file.size absent (rare) → cap on the realized bytes.
    if len(data) > max_bytes:
        raise MediaTooLarge(len(data))
    return data
