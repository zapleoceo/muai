"""LLM circuit breaker — не долбить брокер запросами, обречёнными на отказ.

Найдено в логах брокера (2026-07-16): 75% chat:fast-джобов Веры за 48ч
умирали об «daily budget cap reached», и каждая ретраилась брокером 8-9 раз
— тысячи мусорных джобов, из-за которых живые ждали в очереди по 4-5 минут.

Механика: при фатальном классе ошибки (кап бюджета / нет провайдера)
записываем в app_control кулдаун per-capability; до его истечения chat()/
chat_async() отказывают МГНОВЕННО (LLMCoolingDown), не создавая джобу.
Воркеры (триаж, media) проверяют кулдаун перед claim'ом и спят — очередь
брокера остаётся чистой для тех, у кого бюджет есть.

- «daily budget cap reached» → короткий кулдаун-проба BUDGET_CAP_COOLDOWN_MIN
  (настройка /settings, дефолт 30 мин), но не дальше следующего 00:00 UTC,
  когда брокер сбрасывает дневной кап.
- «no provider available» → кулдаун NO_PROVIDER_COOLDOWN_MIN (настройка
  /settings, дефолт 30 мин) — пул может ожить (ключ выйдет из cooldown).

ПОЧЕМУ кап тоже короткий (инцидент 2026-07-31): раньше budget_cap ставил
блокировку до полуночи UTC. Ошибка прилетела в 00:25 — и vision встал на
23.5 часа, хотя это был разовый отказ одной минуты (свободные gemini-ключи
были в минутном кулдауне, платный резерв упёрся в свой кап, openrouter
отдал RateLimitError). Через несколько минут пул ожил, но Вера уже не
слала запросов и очередь стояла полсуток при простаивающем брокере.
Сообщение брокера «retry after 00:00 UTC» относится к КОНКРЕТНОМУ ключу,
а не ко всей capability, поэтому доверять ему как сроку блокировки нельзя.
Проба раз в 30 мин — это ~48 лишних джоб в сутки в худшем случае (против
тысяч, ради чего брейкер и делался), зато потолок простоя 30 мин, а не 24ч.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from vera_shared.control import (
    BUDGET_CAP_COOLDOWN_MIN,
    NO_PROVIDER_COOLDOWN_MIN,
    get_control,
    get_int_setting,
    set_control,
)

log = logging.getLogger(__name__)

_KEY_PREFIX = "llm_cooldown:"


def classify_broker_error(message: str) -> str:
    """'budget_cap' | 'no_provider' | 'other' — по тексту ошибки джобы."""
    m = (message or "").lower()
    if "daily budget cap reached" in m:
        return "budget_cap"
    if "no provider available" in m:
        return "no_provider"
    return "other"


def next_utc_midnight(now: datetime) -> datetime:
    """Брокер сбрасывает дневной кап в 00:00 UTC."""
    base = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return (base + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


async def note_llm_failure(capability: str, error_message: str) -> str:
    """Записать кулдаун, если ошибка фатального класса. Возвращает класс."""
    kind = classify_broker_error(error_message)
    now = datetime.now(UTC)
    if kind == "budget_cap":
        # Короткая проба, но не дальше сброса капа в 00:00 UTC (если полночь
        # ближе, чем интервал пробы — ждём ровно до неё).
        minutes = await get_int_setting(BUDGET_CAP_COOLDOWN_MIN, 30)
        until = min(next_utc_midnight(now), now + timedelta(minutes=minutes))
    elif kind == "no_provider":
        minutes = await get_int_setting(NO_PROVIDER_COOLDOWN_MIN, 30)
        until = now + timedelta(minutes=minutes)
    else:
        return kind
    await set_control(f"{_KEY_PREFIX}{capability}", until.isoformat())
    log.warning("LLM circuit OPEN for %s until %s (%s)", capability, until, kind)
    return kind


async def llm_cooldown_remaining_s(capability: str) -> float:
    """Секунд до конца кулдауна capability; 0 — можно звонить."""
    raw = await get_control(f"{_KEY_PREFIX}{capability}", "")
    if not raw:
        return 0.0
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return max(0.0, (until - datetime.now(UTC)).total_seconds())


async def reset_llm_cooldown(capability: str) -> None:
    """Успешный вызов закрывает circuit досрочно (пул ожил раньше срока)."""
    raw = await get_control(f"{_KEY_PREFIX}{capability}", "")
    if raw:
        await set_control(f"{_KEY_PREFIX}{capability}", "")
        log.info("LLM circuit CLOSED for %s (successful call)", capability)
