"""Помесячная агрегация числовых полей из «отчётных» чатов (SMS-дайджесты).

Зачем отдельно от обычного retrieval: запрос вида «отчёт заказов помесячно
за 2026 год» требует ТОЧНЫХ сумм по всем сообщениям периода, а не пересказ
top-N событий LLM'ом (при 79 сообщениях в чате top-15/60 обрежет большинство
месяцев, а LLM всё равно не умеет складывать числа надёжно). Здесь суммы
считает код по всем строкам без ограничения limit — обычный LLM-путь эти
запросы не покрывает и не должен пытаться.

Формат чата НЕ гарантирован стабильным: ранние сообщения — свободный текст
(здесь их числа не парсим, только считаем как "неструктурированные"), с
какого-то момента источник начинает слать `key: value` построчно после '---'.

По умолчанию — детальная таблица по ВСЕМ числовым полям (не придумываем,
что «главное» само). Но Дима 2026-07-08 явно уточнил: «заказы» в этом чате —
это поле `b/n` (движение денег, IDR). См. FIELD_INTENT_MAP: при слове «заказ»
в запросе без явного «детально» — короткий формат «месяц: сумма» по b/n.
"""
from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import text
from vera_shared.db.engine import get_session

log = logging.getLogger(__name__)

TZ_OFFSET_H = int(os.environ.get("VERA_TZ_OFFSET_H", "7"))

_REPORT_TRIGGERS = (
    "отчет", "отчёт", "помесячно", "по месяцам", "статистик", "report",
)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Бизнес-термин → поле в отчёте. Задано Димой явно (2026-07-08): «заказы» в
# чате «Jakarta: sms report» — это b/n (движение денег), не счётчики contract.
# Специфично для конкретного чата/формата — не universal-эвристика.
FIELD_INTENT_MAP: dict[str, str] = {
    "заказ": "b/n",
    "order": "b/n",
}
_DETAIL_TRIGGERS = ("детальн", "подробн", "все поля", "полностью", "по всем показател")


def detect_target_field(q: str) -> str | None:
    """Слово из запроса → конкретное поле отчёта (None = показать все поля)."""
    ql = q.lower()
    if any(t in ql for t in _DETAIL_TRIGGERS):
        return None
    for term, field in FIELD_INTENT_MAP.items():
        if term in ql:
            return field
    return None

# "key : value" строка после разделителя '---' в теле события. Значение —
# число с пробелами-разделителями тысяч, опциональным знаком и десятичной точкой.
_KV_LINE_RE = re.compile(
    r"^([A-Za-zА-Яа-яёЁ0-9,<>/\s]{2,40}?)\s*:\s*([+\-]?\s*[\d][\d\s]*(?:\.\d+)?)\s*$",
    re.MULTILINE,
)


def detect_report_request(q: str) -> tuple[bool, int | None]:
    """(нужен ли помесячный отчёт, год если указан явно)."""
    ql = q.lower()
    if not any(t in ql for t in _REPORT_TRIGGERS):
        return False, None
    m = _YEAR_RE.search(ql)
    return True, (int(m.group(1)) if m else None)


async def find_report_chat(q: str) -> tuple[str, str] | None:
    """Найти конкретный чат по подстроке его названия в запросе.

    Матчим против project_membership.label (kind='chat') — там живут точные
    названия рабочих чатов. Без явного совпадения агрегацию не запускаем:
    угадывать «какой чат имелся в виду» по общему проекту небезопасно (поля
    отчёта специфичны для конкретного источника, не для всего проекта).
    """
    ql = q.lower()
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT key, label FROM project_membership WHERE kind='chat'"
        ))).all()
    best: tuple[str, str] | None = None
    for chat_id, label in rows:
        if label and label.lower() in ql and (best is None or len(label) > len(best[1])):
            best = (chat_id, label)
    return best


def _parse_kv(body: str) -> dict[str, float]:
    """key→число из тела после '---'. Свободный текст просто не даст матчей."""
    tail = body.split("---", 1)[1] if "---" in body else ""
    out: dict[str, float] = {}
    for key, raw_val in _KV_LINE_RE.findall(tail):
        key = " ".join(key.split()).strip().lower()
        val = raw_val.replace(" ", "")
        try:
            out[key] = float(val)
        except ValueError:
            continue
    return out


async def build_monthly_report(chat_id: str, chat_title: str,
                               year: int | None) -> dict[str, Any]:
    """Точная помесячная сумма числовых полей по всем событиям чата.

    Без LIMIT — отчёт обязан покрыть ВСЕ сообщения периода, иначе суммы врут.
    """
    where = ["metadata->>'chat_id' = :cid"]
    params: dict[str, Any] = {"cid": chat_id}
    if year is not None:
        where.append("occurred_at >= :y_start AND occurred_at < :y_end")
        # occurred_at в БД — naive UTC; сдвигаем границы локального года на TZ.
        params["y_start"] = datetime(year, 1, 1) - _tz_delta()
        params["y_end"] = datetime(year + 1, 1, 1) - _tz_delta()

    async with get_session() as s:
        rows = (await s.execute(text(f"""
            SELECT occurred_at, content_text FROM events
            WHERE {' AND '.join(where)}
            ORDER BY occurred_at ASC
        """), params)).all()

    months: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    counts: dict[str, int] = defaultdict(int)
    unstructured: dict[str, int] = defaultdict(int)
    all_keys: list[str] = []

    for occurred_at, body in rows:
        local_dt = occurred_at + _tz_delta()
        month_key = local_dt.strftime("%Y-%m")
        counts[month_key] += 1
        kv = _parse_kv(body)
        if not kv:
            unstructured[month_key] += 1
            continue
        for k, v in kv.items():
            if k not in all_keys:
                all_keys.append(k)
            if _is_snapshot_key(k):
                # Баланс/скор — не сумма, а значение НА КОНЕЦ месяца (события
                # идут по возрастанию времени, поэтому просто перезаписываем).
                months[month_key][k] = v
            else:
                months[month_key][k] += v

    return {
        "chat_title": chat_title,
        "year": year,
        "total_messages": len(rows),
        "months": dict(sorted(months.items())),
        "counts": dict(counts),
        "unstructured": dict(unstructured),
        "keys": all_keys,
    }


# Поля-снэпшоты (баланс/скор на момент сообщения) — сумма таких полей за
# месяц бессмысленна (никто не хочет "сумму остатков на разные дни"), нужно
# конечное значение месяца. Остальные поля (b/n, contract, expenses) —
# потоковые величины, для них сумма за месяц осмысленна.
_SNAPSHOT_KEY_HINTS = ("ost", "lead point", "остаток", "balance")


def _is_snapshot_key(key: str) -> bool:
    return any(h in key for h in _SNAPSHOT_KEY_HINTS)


def _tz_delta():
    from datetime import timedelta
    return timedelta(hours=TZ_OFFSET_H)


def render_simple_markdown(report: dict[str, Any], field: str) -> str:
    """Компактный формат «месяц — сумма» по одному конкретному полю."""
    if report["total_messages"] == 0:
        return (f"В чате «{report['chat_title']}» нет сообщений"
                + (f" за {report['year']} год." if report["year"] else "."))

    is_snap = _is_snapshot_key(field)
    lines = ["| Месяц | Сумма |", "|---|---|"]
    total = 0.0
    last_val: float | None = None
    have_field = False
    for month, vals in report["months"].items():
        v = vals.get(field)
        lines.append(f"| {month} | {f'{v:,.0f}'.replace(',', ' ') if v is not None else '—'} |")
        if v is not None:
            have_field = True
            total += v
            last_val = v
    if not have_field:
        return (f"В чате «{report['chat_title']}» не нашлось поля «{field}» "
                "в структурированных сообщениях за этот период.")
    total_display = last_val if is_snap else total
    lines.append(f"| **Итого** | **{f'{total_display:,.0f}'.replace(',', ' ')}** |")

    title = f"«{field}» по месяцам — «{report['chat_title']}»"
    if report["year"]:
        title += f" за {report['year']} год"
    unstructured_total = sum(report["unstructured"].values())
    note = (f"\n\n_{unstructured_total} сообщени(й) без числовых полей не учтены._"
            if unstructured_total else "")
    hint = "\n\n_Все показатели по этому чату — спроси «детально»._"
    return f"### {title}\n\n" + "\n".join(lines) + note + hint


def render_report_markdown(report: dict[str, Any]) -> str:
    if report["total_messages"] == 0:
        return (f"В чате «{report['chat_title']}» нет сообщений"
                + (f" за {report['year']} год." if report["year"] else "."))

    keys = report["keys"]
    col_labels = [f"{k} (на конец мес.)" if _is_snapshot_key(k) else k for k in keys]
    header = "| Месяц | Сообщений | " + " | ".join(col_labels) + " |"
    sep = "|---|---|" + "---|" * len(keys)
    lines = [header, sep]
    totals = defaultdict(float)
    last_snapshot: dict[str, float] = {}
    for month, vals in report["months"].items():
        row = [month, str(report["counts"].get(month, 0))]
        for k in keys:
            v = vals.get(k)
            row.append(f"{v:,.0f}".replace(",", " ") if v is not None else "—")
            if v is not None:
                if _is_snapshot_key(k):
                    last_snapshot[k] = v  # месяцы уже идут по возрастанию
                else:
                    totals[k] += v
        lines.append("| " + " | ".join(row) + " |")
    total_row = ["**Итого**", str(report["total_messages"])]
    for k in keys:
        if _is_snapshot_key(k):
            v = last_snapshot.get(k)
            total_row.append(f"**{v:,.0f}**".replace(",", " ") if v is not None else "—")
        else:
            total_row.append(f"**{totals[k]:,.0f}**".replace(",", " "))
    lines.append("| " + " | ".join(total_row) + " |")

    note = ""
    unstructured_total = sum(report["unstructured"].values())
    if unstructured_total:
        months_list = ", ".join(sorted(report["unstructured"].keys()))
        note = (f"\n\n_{unstructured_total} сообщени(й) без числовых полей "
                f"(свободный текст, не входят в суммы) — месяцы: {months_list}._")

    title = f"Отчёт по чату «{report['chat_title']}»"
    if report["year"]:
        title += f" за {report['year']} год"
    return f"### {title}\n\n" + "\n".join(lines) + note
