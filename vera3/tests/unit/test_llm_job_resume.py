"""vera_shared.llm.client — недосчитанная брокером джоба не теряется.

Дедлайн опроса вышел, а брокер джобу ещё считает (12.09.2026: vision по 25
минут в очереди брокера). Раньше это был обычный LLMCallFailed без job_id,
и следующая попытка отправляла тот же payload заново — двойной счёт модели.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from vera_shared.db import models  # noqa: F401 — registers app_control on Base
from vera_shared.llm.circuit import llm_cooldown_remaining_s


@pytest.mark.asyncio
async def test_pending_surfaces_job_id_and_keeps_circuit_closed(sqlite_db):
    import vera_shared.llm.client as client_mod
    from vera_shared.llm.broker_client import BrokerJobPending
    from vera_shared.llm.client import LLMCallFailed, LLMJobPending, chat_async

    boom = BrokerJobPending(482778, 900.0)
    with patch.object(client_mod, "chat_async_via_broker", AsyncMock(side_effect=boom)), \
         patch.object(client_mod, "broker_enabled", lambda: True), \
         pytest.raises(LLMJobPending) as exc:
        await chat_async(messages=[{"role": "user", "content": "x"}], capability="vision")
    assert exc.value.job_id == 482778
    assert isinstance(exc.value, LLMCallFailed)          # старые except-ветки ловят
    # брокер жив, просто медленный — capability не закрывается на 30 минут
    assert await llm_cooldown_remaining_s("vision") == 0


@pytest.mark.asyncio
async def test_resume_job_id_is_forwarded_to_the_broker_client(sqlite_db):
    import vera_shared.llm.client as client_mod
    from vera_shared.llm.client import chat_async

    with patch.object(client_mod, "chat_async_via_broker",
                      AsyncMock(return_value=("ok", {}))) as broker, \
         patch.object(client_mod, "broker_enabled", lambda: True):
        await chat_async(messages=[{"role": "user", "content": "x"}],
                         capability="vision", resume_job_id=7)
    assert broker.await_args.kwargs["resume_job_id"] == 7
