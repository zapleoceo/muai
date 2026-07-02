"""Runtime control flags — a tiny key/value table workers poll each loop.

Currently used for the backfill pause switch: the dashboard sets
`backfill_paused=1`, and brain-triage + media-worker skip claiming work
until it's cleared. Survives restarts (it's in Postgres), so a pause
holds across deploys.
"""
from __future__ import annotations

from sqlalchemy import text

from vera_shared.db.engine import get_session

BACKFILL_PAUSED = "backfill_paused"
BACKFILL_MAX_PER_HOUR = "backfill_max_per_hour"

# usage_log.workflow values that count as backfill LLM requests for the
# rate limit. Heavy calls only — embeds (voyage, cheap/batched) excluded.
_RATE_WORKFLOWS = ("triage", "media_vision", "media_voice")


async def get_control(key: str, default: str = "") -> str:
    async with get_session() as s:
        row = (await s.execute(
            text("SELECT value FROM app_control WHERE key = :k"), {"k": key}
        )).scalar_one_or_none()
    return row if row is not None else default


async def set_control(key: str, value: str) -> None:
    async with get_session() as s:
        await s.execute(text("""
            INSERT INTO app_control (key, value, updated_at)
            VALUES (:k, :v, now())
            ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = now()
        """), {"k": key, "v": value})


async def is_backfill_paused() -> bool:
    return (await get_control(BACKFILL_PAUSED, "0")) == "1"


async def set_backfill_paused(paused: bool) -> None:
    await set_control(BACKFILL_PAUSED, "1" if paused else "0")


async def get_backfill_max_per_hour() -> int:
    """Backfill request cap per hour. 0 = unlimited (no throttle)."""
    try:
        return max(0, int(await get_control(BACKFILL_MAX_PER_HOUR, "0")))
    except ValueError:
        return 0


async def set_backfill_max_per_hour(n: int) -> None:
    await set_control(BACKFILL_MAX_PER_HOUR, str(max(0, int(n))))


async def _requests_last_minute() -> int:
    """Heavy backfill requests (triage + media) in the trailing 60s, global
    across all workers/replicas — usage_log is the shared source of truth."""
    async with get_session() as s:
        return (await s.execute(text(
            "SELECT COUNT(*) FROM usage_log "
            "WHERE workflow = ANY(:wf) AND created_at > now() - interval '60 seconds'"
        ), {"wf": list(_RATE_WORKFLOWS)})).scalar() or 0


async def backfill_minute_allowance() -> int | None:
    """How many more backfill requests may run THIS minute, for smooth even
    pacing. None → unlimited (no cap set). 0 → rate reached, hold.

    Even-tempo: the hourly cap is spread to a per-minute budget; workers claim
    at most `allowance` items per cycle so the rate stays flat instead of
    bursting. Live events share the same budget (they also write workflow=
    'triage'), so the cap bounds total triage/media throughput, not just the
    historical backfill."""
    cap = await get_backfill_max_per_hour()
    if cap <= 0:
        return None
    per_min = max(1, round(cap / 60))
    used = await _requests_last_minute()
    return max(0, per_min - used)


# ─── Runtime settings registry ──────────────────────────────────────────────
# Параметры, редактируемые из дашборда (раздел «настройки»). Живут в app_control,
# читаются на лету — менять можно без передеплоя. Монитор (bash) читает те же
# ключи из app_control напрямую.

MONITOR_THROTTLE_MIN = "monitor_throttle_min"
TRIAGE_BACKLOG_WARN = "triage_backlog_warn"
TRIAGE_BACKLOG_HUGE = "triage_backlog_huge"
MONITOR_BACKLOG_ENABLED = "monitor_backlog_enabled"


class Setting:
    """Описание настраиваемого параметра для UI + документации."""
    def __init__(self, key: str, label: str, default: str, unit: str,
                 desc: str, kind: str = "int"):
        self.key = key
        self.label = label
        self.default = default
        self.unit = unit
        self.desc = desc
        self.kind = kind  # int | bool


# Порядок = порядок в UI.
SETTINGS: list[Setting] = [
    Setting(MONITOR_THROTTLE_MIN, "Пауза между повторами алерта", "30", "мин",
            "Как часто монитор повторяет ОДИН И ТОТ ЖЕ алерт (напр. «backlog "
            "HUGE»). Было захардкожено 30 мин — отсюда сообщение каждые полчаса. "
            "Поставь 180 = раз в 3 часа, 1440 = раз в сутки."),
    Setting(MONITOR_BACKLOG_ENABLED, "Алерты про backlog триажа", "1", "",
            "Слать ли вообще алерты «Triage backlog большой». Во время разбора "
            "исторического бэкфила очередь заведомо большая — можно выключить (0), "
            "чтобы не спамило, и включить (1) когда бэкфил разобран.", kind="bool"),
    Setting(TRIAGE_BACKLOG_WARN, "Порог WARN очереди триажа", "5000", "событий",
            "Выше этого числа pending-событий монитор шлёт мягкое предупреждение."),
    Setting(TRIAGE_BACKLOG_HUGE, "Порог HUGE очереди триажа", "10000", "событий",
            "Выше этого — алерт «backlog HUGE». Держи заметно выше текущего "
            "бэклога, если он рассасывается штатно."),
    Setting(BACKFILL_MAX_PER_HOUR, "Лимит триажа (запросов/час)", "0", "req/ч",
            "Ровный темп триажа: 0 = без лимита (максимальная скорость). Ставь "
            "число, чтобы сгладить нагрузку на брокер (напр. 6000 = 100/мин)."),
]


async def get_settings_values() -> dict[str, str]:
    """Текущие значения всех настроек (с дефолтами для незаданных)."""
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT key, value FROM app_control WHERE key = ANY(:keys)"
        ), {"keys": [x.key for x in SETTINGS]})).all()
    stored = dict(rows)
    return {x.key: stored.get(x.key, x.default) for x in SETTINGS}


async def get_int_setting(key: str, default: int) -> int:
    try:
        return int(await get_control(key, str(default)))
    except ValueError:
        return default
