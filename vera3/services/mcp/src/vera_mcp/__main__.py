"""Точка входа контейнера: `python -m vera_mcp`."""
from __future__ import annotations

import logging
import os

import uvicorn

from vera_mcp.auth import validate_tokens
from vera_mcp.server import build_app

log = logging.getLogger("vera_mcp")


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    names = sorted(set(validate_tokens().values()))
    if names:
        log.info("MCP clients configured: %s", ", ".join(names))
    else:
        log.error("MCP_TOKEN/MCP_TOKENS не заданы — все запросы будут отвергнуты (401)")
    # nginx стоит перед контейнером: доверяем его X-Forwarded-* только из сети docker
    uvicorn.run(build_app(), host="0.0.0.0", port=8000, proxy_headers=True,
                forwarded_allow_ips="*", timeout_keep_alive=75)


if __name__ == "__main__":
    main()
