"""Разбор запроса: временной диапазон + веса источников.

Чинит баг «саммари за вчера по Itstep»: Вера отвечала по Perplexity-промптам
со всей истории, потому что (1) «вчера» не превращалось в фильтр по дате,
(2) source=perplexity ранжировался наравне с реальными событиями.

Время Димы — Asia/Jakarta (UTC+7). occurred_at в БД — naive UTC, поэтому
границы локального дня сдвигаем на -offset при переводе в UTC.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

TZ_OFFSET_H = int(os.environ.get("VERA_TZ_OFFSET_H", "7"))


# ─── Проекты: «по проекту Itstep» → реальные ящики + рабочие чаты ────────────
# Брать «по проекту X» как текстовый поиск слова X — неверно: рабочий чат
# «J Branch Internal» не содержит «itstep», но это сердце проекта Джакарта.
# Реестр редактируемый — Дима может уточнить состав чатов.

@dataclass
class ProjectScope:
    name: str
    account_like: list[str] = field(default_factory=list)   # ILIKE паттерны
    chats: list[str] = field(default_factory=list)          # точные chat_title


PROJECT_ALIASES: dict[str, dict] = {
    "itstep": {
        # distinctive триггеры — низкий риск ложного срабатывания
        "triggers": ["itstep", "it step", "it-step", "ит степ", "ит-степ",
                     "айтистеп", "джакарт", "jakarta", "j branch"],
        "account_like": ["itstep.org"],
        "chats": [
            "Старшие и отчеты",
            "J Branch Internal",
            "Studing Jakarta internal",
            "IT-Step x TEO",
            "Jakarta sales",
        ],
    },
    "veranda": {
        # стемы — ловят падежи: «веранде/веранды/веранду»
        "triggers": ["verand", "веранд"],
        "account_like": [],
        "chats": [
            "Veranda менеджмент",
            "Веранда сотрудники",
            "Veranda transactions",
            "Veranda AI",
            "GameZone & Veranda",
        ],
    },
}


def resolve_project(q: str) -> ProjectScope | None:
    """Определить упомянутый проект по триггерам. None — если не упомянут."""
    ql = q.lower()
    for name, cfg in PROJECT_ALIASES.items():
        if any(t in ql for t in cfg["triggers"]):
            return ProjectScope(
                name=name,
                account_like=list(cfg["account_like"]),
                chats=list(cfg["chats"]),
            )
    return None


# ─── Намерение «сводка/что сделано» → шире выборка, синтез по сути ───────────
_SUMMARY_TRIGGERS = (
    "саммари", "сводк", "сделано", "что было", "вытяни", "все переписк",
    "всю переписк", "что полезн", "итог", "резюме", "обзор", "дайджест",
    "summary", "что происходил",
)


def is_summary_query(q: str) -> bool:
    ql = q.lower()
    return any(t in ql for t in _SUMMARY_TRIGGERS)

# Понижающие веса: источники-«намерения», а не события мира.
# perplexity — промпты Димы к Perplexity AI: вопрос ≠ выполненная работа.
# vera_chat — разговоры с самой Верой: не дублировать их как «факты дня».
SOURCE_WEIGHTS: dict[str, float] = {
    "perplexity": 0.25,
    "vera_chat": 0.5,
    "vera_memory": 1.2,  # выведенные факты — наоборот ценнее
}

SOURCE_PROMPT_NOTE = (
    "4) События с source=perplexity — это ЗАПРОСЫ Димы к Perplexity AI "
    "(его намерения и вопросы), а НЕ выполненная работа и не факты. "
    "НИКОГДА не описывай их как «сделано/выполнено/подготовлено». "
    "События source=vera_chat — прошлые разговоры с тобой, тоже не факты мира.\n"
    "5) Если в вопросе есть период («вчера», «сегодня», «за неделю») — "
    "опирайся ТОЛЬКО на события с датами внутри периода; даты указаны в скобках."
)


def source_weight(source: str) -> float:
    return SOURCE_WEIGHTS.get(source, 1.0)


_LATIN_RE = re.compile(r"[A-Za-z]+")


def extract_account_terms(words: list[str], *, limit: int = 5) -> list[str]:
    """Из значимых слов запроса выбрать ИМЕНА СОБСТВЕННЫЕ для match по account.

    Маркер: латиница (Itstep, Veranda) или слово с Заглавной буквы.
    Generic-слова (саммари, вчера, проекту) исключаются — иначе они
    забивают слоты и матчат пол-базы по полю account.
    Возвращает lowercase-термы.
    """
    out: list[str] = []
    for w in words:
        if len(w) < 4:
            continue
        if w[0].isupper() or _LATIN_RE.fullmatch(w):
            out.append(w.lower())
        if len(out) >= limit:
            break
    return out


# Порядок важен + word boundary: «позавчера» содержит «вчера» как подстроку.
_RELATIVE_DAY = [
    (re.compile(r"(?<![а-яё])позавчера(?![а-яё])"), 2),
    (re.compile(r"(?<![а-яё])вчера(?![а-яё])"), 1),
    (re.compile(r"(?<![а-яё])сегодня(?![а-яё])"), 0),
]

# «за 3 дня», «за последние 5 дней», «3 дня назад»
_N_DAYS_RE = re.compile(
    r"(?:за\s+(?:последни[ехе]\s+)?|последни[ехе]\s+)?(\d{1,3})\s*(?:дн[еяй]|дней|суток)",
)
_WEEK_RE = re.compile(r"(?:за\s+|на\s+|эт[ауой]+\s+)?недел[юеи]")
_MONTH_RE = re.compile(r"(?:за\s+|эт[оа]т?\s+)?месяц")
# Явная дата: 9 июня / 09.06 / 2026-06-09
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_DATE_WORD_RE = re.compile(
    r"(\d{1,2})\s+(январ|феврал|март|апрел|ма[яе]|июн|июл|август|сентябр|октябр|ноябр|декабр)",
)
_DATE_NUM_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")


def _local_day_bounds_utc(now_utc: datetime, days_ago: int) -> tuple[datetime, datetime]:
    """[start, end) локального дня N дней назад, в naive UTC."""
    local_now = now_utc + timedelta(hours=TZ_OFFSET_H)
    local_day = (local_now - timedelta(days=days_ago)).date()
    local_start = datetime(local_day.year, local_day.month, local_day.day)
    start_utc = local_start - timedelta(hours=TZ_OFFSET_H)
    return start_utc, start_utc + timedelta(days=1)


def parse_time_range(q: str, *, now_utc: datetime | None = None) -> tuple[datetime, datetime] | None:
    """Найти временной диапазон в вопросе. None — если не упомянут.

    Возвращает (start_utc, end_utc) полуинтервал [start, end).
    """
    now_utc = now_utc or datetime.utcnow()
    ql = q.lower()

    for pattern, days_ago in _RELATIVE_DAY:
        if pattern.search(ql):
            return _local_day_bounds_utc(now_utc, days_ago)

    m = _DATE_WORD_RE.search(ql)
    if m:
        day = int(m.group(1))
        month_word = m.group(2)
        month = next((v for k, v in _MONTHS.items() if month_word.startswith(k)), None)
        if month and 1 <= day <= 31:
            local_now = now_utc + timedelta(hours=TZ_OFFSET_H)
            year = local_now.year
            # дата в будущем относительно сегодня → имелся в виду прошлый год
            try:
                candidate = datetime(year, month, day)
            except ValueError:
                return None
            if candidate.date() > local_now.date():
                candidate = datetime(year - 1, month, day)
            start_utc = candidate - timedelta(hours=TZ_OFFSET_H)
            return start_utc, start_utc + timedelta(days=1)

    m = _DATE_NUM_RE.search(ql)
    if m:
        try:
            if m.group(1):  # ISO 2026-06-09
                candidate = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:  # 09.06[.2026]
                day, month = int(m.group(4)), int(m.group(5))
                year_raw = m.group(6)
                local_now = now_utc + timedelta(hours=TZ_OFFSET_H)
                year = int(year_raw) if year_raw else local_now.year
                if year < 100:
                    year += 2000
                candidate = datetime(year, month, day)
                if not year_raw and candidate.date() > local_now.date():
                    candidate = datetime(year - 1, month, day)
            start_utc = candidate - timedelta(hours=TZ_OFFSET_H)
            return start_utc, start_utc + timedelta(days=1)
        except ValueError:
            return None

    m = _N_DAYS_RE.search(ql)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 365:
            start, _ = _local_day_bounds_utc(now_utc, n)
            _, end = _local_day_bounds_utc(now_utc, 0)
            return start, end

    if _WEEK_RE.search(ql):
        start, _ = _local_day_bounds_utc(now_utc, 7)
        _, end = _local_day_bounds_utc(now_utc, 0)
        return start, end

    if _MONTH_RE.search(ql):
        start, _ = _local_day_bounds_utc(now_utc, 30)
        _, end = _local_day_bounds_utc(now_utc, 0)
        return start, end

    return None
