"""Карточка человека и пара «слева — справа» для страницы дублей."""
from __future__ import annotations

from typing import Any

from dashboard.render import esc, tg_link

SAMPLES_SHOWN = 2
CHATS_SHOWN = 3


def msg_count(dossier: dict[str, Any] | None) -> int:
    return int((dossier or {}).get("msg_count") or 0)


def _name(d: dict[str, Any]) -> str:
    name = esc(d.get("name") or "—")
    link = tg_link(d.get("username"), d.get("tg_id"))
    if not link:
        return name
    target = ' target="_blank" rel="noopener"' if link.startswith("http") else ""
    return f'<a href="{esc(link)}"{target}>{name}</a>'


def _chats(d: dict[str, Any]) -> str:
    places = d.get("top_chats") or d.get("top_places") or []
    if not places:
        return '<span class="muted">сообщений нет</span>'
    return " · ".join(f'{esc((c or "—")[:28])} <span class="muted">({n})</span>'
                      for c, n in places[:CHATS_SHOWN])


def person_card(entity_id: int, d: dict[str, Any] | None, badge: str = "") -> str:
    """Кто это: аватар, имя со ссылкой в Telegram, проект, чаты, пара фраз."""
    d = d or {}
    project = f'<span class="chip">{esc(d["dom_project"])}</span>' if d.get("dom_project") else ""
    tag = f'<span class="badge">{esc(badge)}</span>' if badge else ""
    handle = f' <span class="muted">@{esc(d["username"])}</span>' if d.get("username") else ""
    samples = "".join(f'<blockquote>«{esc(s)}»</blockquote>'
                      for s in (d.get("samples") or [])[:SAMPLES_SHOWN])
    return (
        f'<div class="person">'
        f'<div class="person-head"><img src="/entities/{entity_id}/avatar" width="36" height="36" '
        f'alt="" loading="lazy"><div><strong>{_name(d)}</strong>{handle}'
        f'<div class="muted small">#{entity_id} · {msg_count(d)} сообщ.</div></div>{tag}</div>'
        f'<div class="chips">{project}</div>'
        f'<div class="small">{_chats(d)}</div>{samples}</div>'
    )


def order_pair(a: int, b: int, dossiers: dict[int, dict]) -> tuple[int, int]:
    """Слева — тот, у кого больше сообщений: его карточка и останется."""
    return (a, b) if msg_count(dossiers.get(a)) >= msg_count(dossiers.get(b)) else (b, a)


def pair_html(left: int, right: int, dossiers: dict[int, dict], head: str, actions: str) -> str:
    return (
        f'<article class="pair-card">{head}'
        f'<div class="pair">{person_card(left, dossiers.get(left), "останется")}'
        f'{person_card(right, dossiers.get(right), "вольётся в левую")}</div>'
        f'<div class="actions">{actions}</div></article>'
    )


def group_html(candidate_ids: list[int], dossiers: dict[int, dict], head: str, form: str) -> str:
    cards = "".join(person_card(i, dossiers.get(i)) for i in candidate_ids)
    return f'<article class="pair-card">{head}<div class="cand-grid">{cards}</div>{form}</article>'
