"""Runtime control flags — a tiny key/value table workers poll each loop.

Currently used for the backfill pause switch: the dashboard sets
`backfill_paused=1`, and brain-triage + media-worker skip claiming work
until it's cleared. Survives restarts (it's in Postgres), so a pause
holds across deploys.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from vera_shared.db.engine import get_session

BACKFILL_PAUSED = "backfill_paused"
BACKFILL_MAX_PER_HOUR = "backfill_max_per_hour"

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


async def reserve_backfill_allowance(want: int) -> int | None:
    """Атомарная резервация минутного бюджета. None — капа нет; иначе 0..want.

    Старый путь (backfill_minute_allowance = read-then-claim) гонялся между
    репликами: каждая читала одинаковый «остаток» и забирала его целиком —
    N реплик × per_min burst вместо ровного темпа. Здесь атомарный инкремент
    счётчика минуты в app_control (row-lock сериализует реплики). Счётчик
    считает ЗАРЕЗЕРВИРОВАННЫЕ события — group-батчинг делает реальных
    LLM-вызовов меньше, перерасхода не бывает."""
    cap = await get_backfill_max_per_hour()
    if cap <= 0:
        return None
    per_min = max(1, round(cap / 60))
    key = f"backfill_used:{datetime.now(UTC):%Y%m%d%H%M}"
    async with get_session() as s:
        await s.execute(text(
            "INSERT INTO app_control (key, value, updated_at) "
            "VALUES (:k, '0', CURRENT_TIMESTAMP) "
            "ON CONFLICT (key) DO NOTHING"), {"k": key})
        new_total = (await s.execute(text(
            "UPDATE app_control "
            "SET value = CAST(CAST(value AS INTEGER) + :w AS TEXT), "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE key = :k "
            "RETURNING CAST(value AS INTEGER)"), {"k": key, "w": want})).scalar()
        await s.execute(text(
            "DELETE FROM app_control "
            "WHERE key LIKE 'backfill_used:%' AND key < :k"), {"k": key})
    prev = int(new_total) - want
    return max(0, min(want, per_min - prev))


# ─── Runtime settings registry ──────────────────────────────────────────────
# Параметры, редактируемые из дашборда (раздел «настройки»). Живут в app_control,
# читаются на лету — менять можно без передеплоя. Монитор (bash) читает те же
# ключи из app_control напрямую.

MONITOR_THROTTLE_MIN = "monitor_throttle_min"
MONITOR_FAIL_STREAK = "monitor_fail_streak"
MONITOR_OK_STREAK = "monitor_ok_streak"
MONITOR_TG_SILENCE_H = "monitor_tg_silence_h"
TRIAGE_BACKLOG_WARN = "triage_backlog_warn"
TRIAGE_BACKLOG_HUGE = "triage_backlog_huge"
MONITOR_BACKLOG_ENABLED = "monitor_backlog_enabled"
CLUSTER_LABEL_DEADLINE_S = "cluster_label_deadline_s"
CLUSTER_LABEL_RETRIES = "cluster_label_retries"
GRAPH_HUB_PERCENTILE = "graph_hub_percentile"
NO_PROVIDER_COOLDOWN_MIN = "no_provider_cooldown_min"
BUDGET_CAP_COOLDOWN_MIN = "budget_cap_cooldown_min"
MEDIA_MIN_OWN_MESSAGES = "media_min_own_messages"


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
    Setting(MONITOR_FAIL_STREAK, "Провалов подряд до алерта", "2", "проверок",
            "Сколько раз подряд проверка должна упасть, чтобы монитор написал. "
            "Монитор крутится раз в 5 мин, поэтому 2 = авария видна через ~10 мин, "
            "а разовая моргнувшая проверка молчит. 1 = старое поведение (шумное: "
            "ночью шли пары «⚠️ нет событий» → «✅ восстановлено» по кругу)."),
    Setting(MONITOR_OK_STREAK, "Успехов подряд до «восстановлено»", "3", "проверок",
            "Сколько раз подряд проверка должна пройти, чтобы монитор объявил "
            "восстановление. Без этого хватало одной удачной выборки: 02.09.2026 "
            "память скакала 31% ↔ 95% каждые 10-20 минут, и на каждый скачок "
            "уходила пара «⚠️ RAM 93%» → «✅ RAM back to 31%» — 14 сообщений за "
            "5 часов при одной непрерывной аварии."),
    Setting(MONITOR_TG_SILENCE_H, "Окно тишины Telegram", "3", "ч",
            "За сколько часов должно не быть НИ ОДНОГО telegram-события, чтобы "
            "считать юзербот отвалившимся. Ночью поток падает до 1-6 сообщений "
            "в час и пустой час — норма, поэтому 1ч давал ложные тревоги."),
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
    Setting(CLUSTER_LABEL_DEADLINE_S, "Ожидание LLM-ярлыка кластера", "240", "с",
            "Сколько ждать ответа брокера на подпись кластера графа. Free-пул "
            "бывает занят — фоновой задаче можно ждать дольше интерактивных 120с."),
    Setting(CLUSTER_LABEL_RETRIES, "Повторы LLM-ярлыка кластера", "2", "",
            "Сколько раз повторить запрос ярлыка при таймауте/сбое, прежде чем "
            "оставить заглушку «кластер N»."),
    Setting(NO_PROVIDER_COOLDOWN_MIN, "Пауза LLM при «нет провайдера»", "30", "мин",
            "Circuit breaker: если брокер отвечает «no provider available», Вера "
            "не шлёт новые запросы этой capability столько минут (пул может ожить, "
            "когда ключ выйдет из кулдауна)."),
    Setting(BUDGET_CAP_COOLDOWN_MIN, "Пауза LLM при «кап бюджета»", "30", "мин",
            "То же для ответа «daily budget cap reached». Пауза-проба: брокер "
            "сообщает о капе КОНКРЕТНОГО ключа, а не всей capability, поэтому "
            "ждать до 00:00 UTC нельзя — 31.07 такая блокировка остановила "
            "распознавание фото на 23 часа при живом пуле. Дальше ближайшей "
            "полуночи UTC пауза всё равно не уходит."),
    Setting(MEDIA_MIN_OWN_MESSAGES, "Порог участия для распознавания фото", "5",
            "сообщ.",
            "Фото из ГРУППЫ идут на распознавание, только если ты написал в ней "
            "хотя бы столько сообщений. Личка — всегда, вещательные каналы — "
            "никогда. Заменило ручной денилист по названиям чатов: он не поймал "
            "«Быть Или» (1792 автора, ни одного твоего сообщения, 196 фото в "
            "очереди). 0 = распознавать фото из всех групп."),
    Setting(GRAPH_HUB_PERCENTILE, "Порог хабов графа (перцентиль)", "99", "%",
            "Узлы со степенью выше этого перцентиля (напр. «Дима», связанный со "
            "всеми) исключаются из разбиения на сообщества и приписываются к "
            "сообществу большинства соседей — иначе весь граф слипается в один "
            "кластер. 100 = не исключать никого."),
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
