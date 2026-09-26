"""Human-readable Russian labels for relationship predicates on /graph."""
from __future__ import annotations

import html
import json

# member_of is synthetic: membership edges drawn alongside LLM-extracted facts.
PREDICATE_LABELS: dict[str, tuple[str, str]] = {
    "member_of": ("состоит в", "участник группы или канала"),
    "coworker_of": ("работает с", "коллеги, работают вместе"),
    "works_at": ("работает в", "сотрудник организации"),
    "friend_of": ("дружит с", "друзья"),
    "client_of": ("клиент", "клиент человека или компании"),
    "reports_to": ("подчиняется", "у кого в подчинении"),
    "boss_of": ("начальник", "руководит этим человеком"),
    "vendor_of": ("поставщик", "оказывает услуги или поставляет товар"),
    "spouse_of": ("супруг(а)", "муж или жена"),
    "parent_of": ("родитель", "мать или отец"),
    "child_of": ("ребёнок", "сын или дочь"),
    "co_founder_of": ("сооснователь", "сооснователь организации"),
    "lives_in": ("живёт в", "место проживания"),
}


def predicate_label(code: str) -> str:
    if code in PREDICATE_LABELS:
        return PREDICATE_LABELS[code][0]
    readable = code.removesuffix("_of").replace("_", " ").strip()
    return readable or code or "без типа"


def predicate_hint(code: str) -> str:
    return PREDICATE_LABELS[code][1] if code in PREDICATE_LABELS else code


def predicate_options_html(codes: list[str]) -> str:
    ordered = sorted(codes, key=lambda c: predicate_label(c).casefold())
    return "".join(
        f'<option value="{html.escape(c)}" title="{html.escape(predicate_hint(c))}">'
        f"{html.escape(predicate_label(c))}</option>"
        for c in ordered
    )


def predicate_labels_json(codes: list[str]) -> str:
    labels = {c: predicate_label(c) for c in codes}
    return json.dumps(labels, ensure_ascii=False).replace("</", "<\\/")
