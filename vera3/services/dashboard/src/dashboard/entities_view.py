"""Страница «Дубли»: предложения Веры, точные совпадения, совпадения по имени.
Ни запросов, ни состояния — только разметка из готовых данных."""
from __future__ import annotations

from typing import Any

from dashboard.duplicates_repo import DuplicatesData
from dashboard.entities_cards import group_html, order_pair, pair_html
from dashboard.render import esc
from dashboard.ui.components import collapsible

CONFIRM_MERGE = ("Объединить эти карточки? Все связи и алиасы перейдут в левую, "
                 "вторая исчезнет. Отменить можно только скриптом на сервере.")
CONFIRM_BULK_EMAIL = ("Объединить ВСЕ пары с одинаковым рабочим email? "
                      "Группы из трёх и больше не трогаем. Отменить массово нельзя.")
CONFIRM_BULK_USERNAME = ("Объединить ВСЕ однозначные пары с одним @username? "
                         "Отменить массово нельзя.")


def merge_form(keeper: int, merged: int, label: str = "Объединить") -> str:
    return (f'<form method="post" action="/entities/merge" data-confirm="{esc(CONFIRM_MERGE)}">'
            f'<input type="hidden" name="keeper_id" value="{keeper}">'
            f'<input type="hidden" name="merged_id" value="{merged}">'
            f'<button type="submit">{esc(label)}</button></form>')


def _options(candidates: list[dict], selected: int) -> str:
    return "".join(
        f'<option value="{c["id"]}"{" selected" if i == selected else ""}>'
        f'{esc(c["name"])} · #{c["id"]}</option>' for i, c in enumerate(candidates))


def select_form(candidates: list[dict]) -> str:
    """Для групп из трёх и больше: выбрать, кого оставить и кого влить."""
    return (
        f'<form method="post" action="/entities/merge" class="select-merge" '
        f'data-confirm="{esc(CONFIRM_MERGE)}">'
        f'<label>Оставить <select name="keeper_id">{_options(candidates, 0)}</select></label>'
        f'<label>Влить в неё <select name="merged_id">{_options(candidates, 1)}</select></label>'
        f'<button type="submit">Объединить</button></form>')


def _suggestion_actions(sg_id: int, keep_left: str, keep_right: str) -> str:
    def button(action: str, label: str, css: str = "") -> str:
        cls = f' class="{css}"' if css else ""
        return f'<button type="submit" name="action" value="{action}"{cls}>{esc(label)}</button>'
    return (f'<form method="post" action="/entities/suggestion" class="actions-row" '
            f'data-confirm="{esc(CONFIRM_MERGE)}">'
            f'<input type="hidden" name="suggestion_id" value="{sg_id}">'
            f'{button(keep_left, "Объединить")}'
            f'{button(keep_right, "Оставить правую", "outline secondary")}</form>'
            f'<form method="post" action="/entities/suggestion" class="actions-row">'
            f'<input type="hidden" name="suggestion_id" value="{sg_id}">'
            f'{button("reject", "Это разные люди", "secondary")}</form>')


def _verdict(sg: dict[str, Any]) -> str:
    sure = "Скорее всего один человек" if sg["verdict"] == "same" else "Возможно, один человек"
    return (f'<h4>{sure} · {int(sg["confidence"] * 100)}%</h4>'
            f'<p class="muted small">Вера: {esc(sg["reason"])}</p>')


def _suggestion_card(sg: dict[str, Any], dossiers: dict[int, dict]) -> str:
    a, b = sg["entity_a"], sg["entity_b"]
    if dossiers.get(a) is None or dossiers.get(b) is None:
        return ""
    left, right = order_pair(a, b, dossiers)
    keep = {a: "accept_a", b: "accept_b"}
    return pair_html(left, right, dossiers, _verdict(sg),
                     _suggestion_actions(sg["id"], keep[left], keep[right]))


def analysis_status(analysis: dict[str, Any]) -> str:
    if analysis["running"]:
        return '<p class="pill warn">Вера анализирует… обновите страницу через минуту</p>'
    last = analysis["last"]
    note = (f'<span class="muted small"> Последний прогон: {last["judged"]} пар '
            f'(один человек {last["same"]}, не уверена {last["unsure"]}, '
            f'разные {last["different"]}).</span>' if last else "")
    return (
        '<form method="post" action="/entities/analyze" class="inline"><button type="submit">'
        'Запустить анализ Веры</button></form> '
        '<form method="post" action="/entities/roster-sync" class="inline">'
        '<button type="submit" class="secondary" title="Юзербот медленно опросит участников '
        'проектных групп и добавит молчунов в граф: ~20 с на чат, чтобы не словить бан.">'
        f'Подтянуть участников групп</button></form>{note}')


def vera_section(data: DuplicatesData, analysis: dict[str, Any]) -> str:
    cards = "".join(_suggestion_card(sg, data.dossiers) for sg in data.suggestions)
    empty = ('' if cards or analysis["running"]
             else '<p class="muted">Пока предложений нет — запустите анализ.</p>')
    return ('<section><h3>Вера предлагает объединить</h3>'
            '<p class="muted">Вера сравнила похожие имена (Маша и Maria, Оля и Ольга) по чатам, '
            'стилю и проектам, в том числе между почтой и Telegram. Без вашего подтверждения '
            f'ничего не объединяется.</p><p>{analysis_status(analysis)}</p>{cards}{empty}</section>')


def _bulk(action: str, label: str, confirm: str) -> str:
    return (f'<form method="post" action="{action}" data-confirm="{esc(confirm)}">'
            f'<button type="submit" class="danger-solid">{esc(label)}</button></form>')


def _username_group(g: dict, dossiers: dict[int, dict]) -> str:
    ids = [c["id"] for c in g["candidates"]]
    head = f'<h4>@{esc(g["username"])} · профилей: {g["size"]}</h4>'
    if len(ids) == 2:
        left, right = order_pair(ids[0], ids[1], dossiers)
        return pair_html(left, right, dossiers, head, merge_form(left, right))
    return group_html(ids, dossiers, head, select_form(g["candidates"]))


def exact_section(data: DuplicatesData) -> str:
    groups = "".join(_username_group(g, data.dossiers) for g in data.collisions)
    return (
        '<section><h3>Точные совпадения</h3>'
        '<p class="muted">Рабочий email и @username уникальны, поэтому два профиля с одним '
        'адресом — это один человек, попавший в граф дважды. Однозначные пары объединяются '
        'кнопкой; группы из трёх и больше — вручную.</p>'
        '<div class="bulk">'
        f'{_bulk("/entities/merge-email-dupes", "Объединить все дубли по email", CONFIRM_BULK_EMAIL)}'
        f'{_bulk("/entities/merge-collisions", "Объединить все пары по @username", CONFIRM_BULK_USERNAME)}'
        f'</div>{groups}</section>')


def _name_group(g: dict, dossiers: dict[int, dict]) -> str:
    more = f'<p class="muted small">…и ещё {g["hidden"]}</p>' if g["hidden"] else ""
    head = f'<h4>«{esc(g["normalized"])}» · кандидатов: {g["size"]}</h4>'
    ids = [c["id"] for c in g["candidates"]]
    return group_html(ids, dossiers, head, more + select_form(g["candidates"]))


def name_section(data: DuplicatesData) -> str:
    if not data.name_groups:
        return '<p class="muted">Совпадений по одному имени нет.</p>'
    body = (
        '<p class="warning">Почти всегда это разные люди: у каждой группы одно имя, но разные '
        'Telegram-аккаунты (21 «Дима» — 21 человек). Чаты и фразы в карточках помогают отличить. '
        'Объединяйте только настоящие дубли.</p>'
        + "".join(_name_group(g, data.dossiers) for g in data.name_groups))
    title = (f"Совпадения только по имени: групп {data.name_groups_total} "
             f"(показано {len(data.name_groups)})")
    return collapsible(title, body)


def duplicates_body(data: DuplicatesData, analysis: dict[str, Any], merged: int | None) -> str:
    notice = (f'<p class="pill ok">Объединено: карточка #{merged} влита в оставшуюся.</p>'
              if merged else "")
    return (f'<h2>Дубли</h2><p class="crumb"><a href="/graph">← к людям</a></p>{notice}'
            f'{vera_section(data, analysis)}{exact_section(data)}{name_section(data)}')
