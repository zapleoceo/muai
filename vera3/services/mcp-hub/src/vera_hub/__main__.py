"""Точка входа контейнера: `python -m vera_hub`."""
from __future__ import annotations

import logging
import os

import uvicorn
from vera_shared.hub.secrets import hub_key

from vera_hub.app import build_app

GRACEFUL_SHUTDOWN_S = 20


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    # httpx на INFO печатает полный URL каждого запроса, а у auth_kind=url секрет и есть URL
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    hub_key()  # без ключа секреты апстримов не расшифровать: падаем на старте, а не на вызове
    uvicorn.run(build_app(), host="0.0.0.0", port=8000, proxy_headers=True,
                forwarded_allow_ips="*", timeout_keep_alive=75,
                timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S)


if __name__ == "__main__":
    main()
