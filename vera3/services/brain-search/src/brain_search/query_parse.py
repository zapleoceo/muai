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
from dataclasses import dataclass
from datetime import datetime, timedelta

from vera_shared.projects.rules import QUERY_TRIGGERS, project_from_query
from vera_shared.timeutil import utc_naive_now

from brain_search.quoted_query import split_quoted_query

TZ_OFFSET_H = int(os.environ.get("VERA_TZ_OFFSET_H", "7"))


# ─── Проекты: «по проекту Itstep» → events.project, а не текст «itstep» ──────
# Источник истины — vera_shared.projects.rules (QUERY_TRIGGERS). Колонку
# events.project проставляют триаж и sync_projects: на проде заполнена у
# 99.8% событий, так что отдельные реестры ящиков и чатов не нужны.

@dataclass(frozen=True)
class ProjectScope:
    name: str
    triggers: tuple[str, ...] = ()


def resolve_project(q: str) -> ProjectScope | None:
    """Определить упомянутый проект. None — если не упомянут."""
    _focus, scope = split_quoted_query(q)
    name = project_from_query(scope)
    if name is None:
        return None
    return ProjectScope(name=name, triggers=QUERY_TRIGGERS[name])


# ─── Намерение «сводка/что сделано» → шире выборка, синтез по сути ───────────
_SUMMARY_TRIGGERS = (
    "саммари", "сводк", "сделано", "что было", "вытяни", "все переписк",
    "всю переписк", "что полезн", "итог", "резюме", "обзор", "дайджест",
    "summary", "что происходил", "отчет", "отчёт", "помесячно", "по месяцам",
    "статистик",
)


def is_summary_query(q: str) -> bool:
    _focus, scope = split_quoted_query(q)
    ql = scope.lower()
    return any(t in ql for t in _SUMMARY_TRIGGERS)

# Понижающие веса: источники-«намерения», а не события мира.
# perplexity — промпты Димы к Perplexity AI: вопрос ≠ выполненная работа.
# vera_chat — разговоры с самой Верой: не дублировать их как «факты дня».
SOURCE_WEIGHTS: dict[str, float] = {
    "perplexity": 0.25,
    "vera_chat": 0.5,
    # vera_memory — 1.0, как у первичных событий. Было 1.2: вместе с
    # importance/200 и бонусом account выведенный факт обгонял первичное
    # свидетельство в 7 запросах из 10 (замер 2026-10-03), хотя он лишь
    # пересказ; повышенный вес к тому же усиливал бы запись агента из
    # недоверенного текста писем.
}

#: Сообщения Telegram-ботов (сервисные, оплаты, чат-менеджеры) — шум рядом
#: с людьми и рабочими письмами, но не выбрасываем: в «Veranda transactions»
#: единственный автор — бот оплат.
BOT_AUTHOR_WEIGHT = 0.4

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
_NOT_LETTER_BEFORE = r"(?<![\wа-яёіїєґ])"
_NOT_LETTER_AFTER = r"(?![\wа-яёіїєґ])"


def _day_words(*words: str) -> re.Pattern[str]:
    alt = "|".join(words)
    return re.compile(f"{_NOT_LETTER_BEFORE}(?:{alt}){_NOT_LETTER_AFTER}")


_RELATIVE_DAY = [
    (_day_words("позавчера", "позавчора", r"day\s+before\s+yesterday",
                r"kemarin\s+dulu"), 2),
    (_day_words("вчера", "вчора", "yesterday", "kemarin"), 1),
    (_day_words("сегодня", "сьогодні", "today", r"hari\s+ini"), 0),
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
# dd.mm без года — только с двумя цифрами в обеих частях («09.06»): «3.5» и
# «1.5 млн» — число, а не дата. Версии вроде v1.2.3 и суммы 1.250,5 тоже мимо.
_AMOUNT_UNIT = (r"(?!\s*(?:млн|млрд|тыс|тис|k\b|m\b|jt\b|juta|ribu|rb\b|"
                r"million|thousand|%|usd|\$|руб|₽))")
_DATE_NUM_RE = re.compile(
    r"(?<![\w.,])(?:(\d{4})-(\d{2})-(\d{2})"
    r"|(\d{2})\.(\d{2})(?![\d.,]\d)" + _AMOUNT_UNIT +
    r"|(\d{1,2})\.(\d{1,2})\.(\d{2,4}))(?![\w])"
)


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
    now_utc = now_utc or utc_naive_now()
    _focus, scope = split_quoted_query(q)
    ql = scope.lower()

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
            else:  # 09.06 или 9.6.2026
                day = int(m.group(4) or m.group(6))
                month = int(m.group(5) or m.group(7))
                year_raw = m.group(8)
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
