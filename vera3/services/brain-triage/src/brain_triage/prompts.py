"""Prompt templates for single-event and group-batch triage calls.

2026-07-08: extracted from what used to be two independently-hand-written
copies (single-event TRIAGE_PROMPT_TEMPLATE + TRIAGE_BATCH_PROMPT_HEADER) —
same context/rules text, drifting risk on every edit. Also compressed
wording (verbose multi-line JSON block + 2-3 examples per ready_subtype
case -> one-line JSON shape + one example each): confirmed live via
Cerebras' own API response that `usage.prompt_tokens` is IDENTICAL whether
a repeated prefix hits their server-side cache or not (cached_tokens=640/750
vs 0/750, same total) — our daily token quota is charged full price
regardless of caching, so the only real lever is a shorter prompt. This was
~750-950 tokens of IDENTICAL text on every single triage call (~16-17k
calls/day) — enough alone to saturate cerebras' whole 14-key daily budget.
All field names/enum values/semantics are unchanged — TRIAGE_JSON_SCHEMA /
TRIAGE_BATCH_JSON_SCHEMA (the enforced structure for schema-capable
providers, see schemas.py) and postprocess_triage's validation/
normalization are untouched; this only compresses the human-readable
instructions.
"""
from __future__ import annotations

from vera_shared.db.models import EventRow

_TRIAGE_CONTEXT = (
    "Контекст Димы: Branch Director IT STEP Academy Jakarta (апрель 2026, "
    "проект itstep), переезд в Индонезию/виза, совладелец бара Veranda во "
    "Вьетнаме (проект veranda), жена Маша и дочь Лиза (family), босс "
    "Дмитрий Егоров (yegorov@itstep.org)."
)
_TRIAGE_RULES = (
    "Правила project: itstep — академия в Джакарте (группы/студенты/"
    "должники/лиды/команда); veranda — бар во Вьетнаме (смены/заказы/"
    "выручка/поставки); family — Маша/Лиза/родители; personal — личные "
    "дела Димы; news — новости/рассылки; other — всё прочее.\n"
    "Правила nature: world_event — факт мира; my_intent — Дима сам "
    "формулирует запрос/идею.\n"
    'ready_subtype ТОЛЬКО если needs_action=true: "deal" — явное намерение '
    'купить курс + готов действовать сейчас (пример: "хочу записаться, вот '
    'номер +62..."); "openhouse" — интерес к Open House 29 июня, не покупка '
    '(пример: "когда опен хаус? хочу прийти"); иначе null.'
)
_TRIAGE_TOPICS = (
    "финансы, должники, расписание, найм, продажи, маркетинг, crm, бар, "
    "меню, поставки, персонал, зарплата, виза, переезд, семья, здоровье, "
    "новости, война, политика, техника, недвижимость, документы"
)

TRIAGE_PROMPT_TEMPLATE = """Ты — Вера, цифровая память Димы. Прочитай событие и извлеки структуру.

""" + _TRIAGE_CONTEXT + """

Событие (источник={source}, account={account}, occurred_at={occurred_at}):
---
{content}
---

Верни СТРОГО JSON: {{"importance": <0-100>, "project": "<itstep|veranda|family|""" \
    """personal|news|other>", "nature": "<world_event|my_intent>", "topics": [<2-4 """ \
    """рус. тега нижний регистр из: """ + _TRIAGE_TOPICS + """>], "people_mentioned": """ \
    """[<люди>], "signals": [{{"type": "task|event|news|offer|question|decision|""" \
    """anomaly", "summary": "<кратко>", "date": "<ISO|null>"}}], "needs_action": """ \
    """<bool>, "ready_subtype": <null|"deal"|"openhouse">}}

""" + _TRIAGE_RULES + """

ВАЖНО: только JSON, без префиксов и комментариев."""

TRIAGE_BATCH_PROMPT_HEADER = """Ты — Вера, цифровая память Димы. Ниже — {n} коротких сообщений
из ОДНОГО группового чата. Разбери КАЖДОЕ по отдельности и верни результат
для каждого, привязанный к его event_id.

""" + _TRIAGE_CONTEXT + """

События:
{events_block}

Верни СТРОГО JSON: {{"results": [{{"event_id": <int>, "importance": <0-100>, """ \
    """"project": "<itstep|veranda|family|personal|news|other>", "nature": """ \
    """"<world_event|my_intent>", "topics": [<2-4 рус. тега нижний регистр из: """ \
    + _TRIAGE_TOPICS + """>], "people_mentioned": [<люди>], "signals": [{{"type": """ \
    """"task|event|news|offer|question|decision|anomaly", "summary": "<кратко>", """ \
    """"date": "<ISO|null>"}}], "needs_action": <bool>, "ready_subtype": """ \
    """<null|"deal"|"openhouse">}}, ...]}} — ОДИН объект на КАЖДЫЙ event_id выше, """ \
    """ничего не пропускай.

""" + _TRIAGE_RULES + """

ВАЖНО: только JSON, без префиксов и комментариев. КАЖДЫЙ event_id из списка
выше должен появиться РОВНО ОДИН раз в results."""


def _event_block(row: EventRow) -> str:
    """Один блок в батч-промпте — event_id явно в заголовке, чтобы LLM могла
    привязать свой результат к правильному событию."""
    content = (row.content_text or "")[:2000]
    return (
        f"[event_id={row.id}] источник={row.source} account={row.account or '—'} "
        f"occurred_at={row.occurred_at.isoformat() if row.occurred_at else '—'}\n"
        f"---\n{content}\n---"
    )


def build_batch_prompt(rows: list[EventRow]) -> str:
    events_block = "\n\n".join(_event_block(r) for r in rows)
    return TRIAGE_BATCH_PROMPT_HEADER.format(n=len(rows), events_block=events_block)
