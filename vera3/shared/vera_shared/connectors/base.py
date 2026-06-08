"""Каждый источник реализует SourceConnector. Это контракт."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable

from vera_shared.events.schema import RawEvent


class ConnectorCapability(str, Enum):
    """Что коннектор умеет."""
    REALTIME = "realtime"        # webhook или polling даёт live updates
    BACKFILL = "backfill"        # умеет fetch_history за период
    BULK_IMPORT = "bulk_import"  # умеет парсить downloaded archive (ZIP/JSON)


class ConnectorAuthError(Exception):
    """Не получилось аутентифицироваться."""


class ConnectorFetchError(Exception):
    """Ошибка получения данных от источника."""


class SourceConnector(ABC):
    """Базовый интерфейс для любого источника событий.

    Реализации:
        - GmailConnector
        - TelegramConnector
        - ManychatConnector
        - TrelloConnector
        - ChatGPTArchiveImporter
        - и так далее
    """

    #: уникальное имя, должно совпадать с RawEvent.source
    name: str = ""

    #: какие capabilities поддерживает
    capabilities: set[ConnectorCapability] = set()

    def __init__(self, *, account: str | None = None, config: dict[str, Any] | None = None):
        """
        Args:
            account: какой аккаунт источника (для multi-account: разные Gmail и т.п.)
            config: per-instance конфигурация
        """
        self.account = account
        self.config = config or {}

    @abstractmethod
    async def authenticate(self, credentials: dict[str, Any]) -> None:
        """OAuth / API key / session token setup.

        Должен поднять ConnectorAuthError если не получилось.
        Кешировать клиент в self._client или подобное.
        """

    @abstractmethod
    async def fetch_history(
        self,
        start: datetime,
        end: datetime,
        **kwargs: Any,
    ) -> AsyncIterator[RawEvent]:
        """Backfill за период. Yields RawEvent по одному.

        Должен:
            - возвращать события в любом порядке
            - normalize в RawEvent
            - поддерживать pagination внутренне
            - не дублировать (вызывающий код всё равно делает dedup)
        """
        # type: ignore[empty-async]
        if False:
            yield RawEvent(  # type: ignore[misc]
                source="dummy", source_event_id="x", occurred_at=datetime.utcnow(),
            )

    async def subscribe_realtime(
        self,
        on_event: Callable[[RawEvent], Awaitable[None]],
    ) -> None:
        """Подписаться на live updates. По умолчанию NotImplemented.

        Реализация может:
            - запустить polling loop
            - принять webhook'и (handler external, эта функция возвращает сразу)
            - запустить websocket consumer
        """
        if ConnectorCapability.REALTIME not in self.capabilities:
            raise NotImplementedError(
                f"{self.name} does not support REALTIME"
            )
        raise NotImplementedError("subclass must override subscribe_realtime")

    async def parse_bulk_archive(
        self,
        file_path: str,
    ) -> AsyncIterator[RawEvent]:
        """Импорт из downloaded архива (ZIP/JSON)."""
        if ConnectorCapability.BULK_IMPORT not in self.capabilities:
            raise NotImplementedError(
                f"{self.name} does not support BULK_IMPORT"
            )
        # type: ignore[empty-async]
        if False:
            yield RawEvent(  # type: ignore[misc]
                source="dummy", source_event_id="x", occurred_at=datetime.utcnow(),
            )

    async def health_check(self) -> dict[str, Any]:
        """Status коннектора. Used by dashboard."""
        return {
            "name": self.name,
            "account": self.account,
            "capabilities": [c.value for c in self.capabilities],
            "authenticated": getattr(self, "_authenticated", False),
        }
