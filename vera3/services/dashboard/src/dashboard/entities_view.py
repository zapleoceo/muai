"""Очередь проверки дублей `/entities/duplicates`: одна пара за раз, три крупные кнопки.
Ни запросов, ни состояния — только разметка из готовых данных."""
from __future__ import annotations

from typing import Any

from dashboard.duplicates_repo import QueueItem
from dashboard.entities_cards import person_card
from dashboard.render import esc

COUNT_RU = (("entity_aliases_moved", "алиасов"), ("relationships_moved", "связей"),
            ("relationships_deleted", "дублей связей склеится"), ("memberships_moved", "участий в группах"),
            ("memberships_deleted", "дублей участий склеится"), ("entity_avatars_moved", "фото"))
REVIEW_COPY = ("Объединить — один человек с двумя карточками. «Разные люди» — Вера больше не предложит эту пару. "
               "Любое объединение можно вернуть в журнале.")

REVIEW_SCRIPT = """<script>
document.addEventListener('keydown', function(e){
  if (e.metaKey || e.ctrlKey || e.altKey || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  var k = {y:'merge', n:'reject', s:'skip', ArrowRight:'skip'}[e.key.length === 1 ? e.key.toLowerCase() : e.key];
  var b = k && document.querySelector('[data-key=' + k + ']');
  if (b){ e.preventDefault(); b.click(); }
});
document.addEventListener('click', function(e){
  var b = e.target.closest('[data-undo]');
  if (!b) return;
  b.disabled = true;
  VeraUI.post('/api/journal/undo', {audit_ids: [Number(b.dataset.undo)]})
    .then(function(){ VeraUI.toast('Объединение возвращено', {kind: 'ok'}); setTimeout(function(){ location.href = '/entities/duplicates'; }, 700); })
    .catch(function(err){ b.disabled = false; VeraUI.toast('Не вернуть: ' + err.message, {kind: 'err'}); });
});
</script>"""


def analysis_status(analysis: dict[str, Any]) -> str:
    if analysis["running"]:
        return '<p class="pill warn">Вера анализирует… обновите страницу через минуту</p>'
    last = analysis["last"]
    note = (f'<span class="muted small"> Последний прогон: {last["judged"]} пар '
            f'(один человек {last["same"]}, не уверена {last["unsure"]}, '
            f'разные {last["different"]}).</span>' if last else "")
    return (
        '<form method="post" action="/entities/analyze" class="inline"><button type="submit" class="secondary">'
        'Запустить анализ Веры</button></form> '
        '<form method="post" action="/entities/roster-sync" class="inline">'
        '<button type="submit" class="secondary" title="Юзербот медленно опросит участников '
        'проектных групп и добавит молчунов в граф: ~20 с на чат, чтобы не словить бан.">'
        f'Подтянуть участников групп</button></form>{note}')


def _stats(summary: dict[str, Any] | None) -> str:
    if not summary:
        return ""
    seen = f' · активность {esc(summary["last_seen_at"][:10])}' if summary.get("last_seen_at") else ""
    email = f' · {esc(summary["email"])}' if summary.get("email") else ""
    return (f'<div class="muted small">{summary["aliases"]} алиасов · {summary["relationships"]} связей · '
            f'{summary["groups"]} групп{email}{seen}</div>')


def _moves(counts: dict[str, int] | None, blockers: list[str]) -> str:
    if blockers:
        return f'<p class="warning">{esc("; ".join(blockers))} — такое объединение делается только вручную.</p>'
    parts = [f"<b>{counts[k]}</b> {label}" for k, label in COUNT_RU if counts and counts.get(k)]
    return f'<p class="muted">Переедет в главную карточку: {", ".join(parts) or "только сама карточка"}.</p>'


def _form(action: str, item: QueueItem, keep: int, n: int, key: str, label: str, css: str) -> str:
    sid = f'<input type="hidden" name="suggestion_id" value="{item.suggestion_id}">' if item.suggestion_id else ""
    return (f'<form method="post" action="{action}" class="big-form">'
            f'<input type="hidden" name="a" value="{item.a}"><input type="hidden" name="b" value="{item.b}">'
            f'<input type="hidden" name="keep" value="{keep}"><input type="hidden" name="n" value="{n}">{sid}'
            f'<button type="submit" class="big {css}" data-key="{key}">{esc(label)}</button></form>')


def _why(item: QueueItem) -> str:
    sure = ""
    if item.confidence is not None:
        sure = ("Скорее всего один человек" if item.verdict == "same" else "Возможно, один человек") + \
               f" · {int(item.confidence * 100)}% · "
    return f'<p class="why"><b>Почему предложено:</b> {sure}{esc(item.reason)}</p>'


def pair_view(item: QueueItem, n: int, keep: int, dossiers: dict[int, dict], summaries: dict[int, dict],
              plan: dict[str, Any], swapped: bool) -> str:
    drop = item.b if keep == item.a else item.a
    cards = "".join(
        f'<div class="queue-side">{person_card(i, dossiers.get(i), badge)}{_stats(summaries.get(i))}</div>'
        for i, badge in ((keep, "останется главной"), (drop, "вольётся")))
    swap = f'/entities/duplicates?n={n}' + ("" if swapped else "&sw=1")
    return (f'{_why(item)}<div class="pair queue-pair">{cards}</div>'
            f'<p><a class="chip" href="{swap}">⇄ Поменять местами</a></p>'
            f'{_moves(plan.get("counts"), plan.get("blockers") or [])}')


def _head(progress: str) -> str:
    return (f'<div class="page-head"><div><h1>Проверка дублей</h1><p class="muted">{esc(progress)}</p></div>'
            '<a class="chip" href="/graph">← к людям</a></div>')


NOTICES = {"blocked": "Объединение не выполнено: в паре владелец или карточка с узлами личности — такое делается только вручную.",
           "gone": "Объединение не выполнено: одной из карточек уже нет (её, видимо, объединили раньше)."}


def review_body(queue: list[QueueItem], n: int, pair_html: str, keep: int | None, merged: int | None,
                analysis: dict[str, Any], notice: str | None = None) -> str:
    undo = (f'<p class="pill ok">Карточки объединены. '
            f'<button type="button" class="ghost sm" data-undo="{merged}">Вернуть</button></p>' if merged else "")
    undo += f'<p class="warning">{esc(NOTICES[notice])}</p>' if notice in NOTICES else ""
    tools = f'<details><summary>Вера и участники групп</summary><p>{analysis_status(analysis)}</p></details>'
    if not queue:
        return (_head("Очередь пуста") + undo
                + '<div class="empty"><strong>Предложений нет</strong>Остальных людей объединяйте из карточки в «Людях»: '
                'кнопка «Это тот же человек…» с поиском.</div>' + tools + REVIEW_SCRIPT)
    item = queue[n]
    bar = f'<div class="bar"><div class="bar-fill" style="width:{int((n + 1) / len(queue) * 100)}%"></div></div>'
    skip = (f'<a role="button" class="secondary big" data-key="skip" href="/entities/duplicates?n={(n + 1) % len(queue)}">'
            f'Пропустить</a>')
    buttons = (_form("/entities/queue/merge", item, keep or item.a, n, "merge", "Это один человек", "")
               + _form("/entities/queue/reject", item, keep or item.a, n, "reject", "Разные люди", "secondary") + skip)
    return (f'{_head(f"{n + 1} из {len(queue)}")}{bar}{undo}{pair_html}<div class="big-actions">{buttons}</div>'
            f'<p class="muted small">Клавиши: <kbd>Y</kbd> один человек · <kbd>N</kbd> разные · <kbd>S</kbd> / <kbd>→</kbd> пропустить. '
            f'{esc(REVIEW_COPY)}</p>{tools}{REVIEW_SCRIPT}')
