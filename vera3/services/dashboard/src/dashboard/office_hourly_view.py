"""Owner-facing hourly Room report inside the existing dashboard shell."""
from __future__ import annotations

from collections import Counter, defaultdict
from urllib.parse import quote

from dashboard.office_card_details import safe_text
from dashboard.office_hourly_service import (
    HourlyCard,
    HourlyReport,
)
from dashboard.office_runtime_summary import runtime_count, source_summaries
from dashboard.office_status import CANCELLED, DONE, STATUSES
from dashboard.render import esc, local_dt


def _projects(report: HourlyReport) -> str:
    groups: dict[str, list[HourlyCard]] = defaultdict(list)
    for card in report.cards:
        groups[card.row.project or "Без проекта"].append(card)
    fresh = bool(report.runtime and report.runtime.is_fresh(report.as_of))
    parts = []
    for project, cards in sorted(groups.items(), key=lambda x: x[0].casefold()):
        counts = Counter(c.status for c in cards)
        active = [c for c in cards if c.status == "В работе"]
        active_text = ", ".join(esc(c.row.title or c.row.task_id) for c in active[:2]) or "нет подтверждённой живой аренды"
        overflow = f' · ещё {len(active) - 2} в работе — проверьте лимит' if len(active) > 2 else ""
        seen = fresh and any(e.project.casefold() == project.casefold()
                             for e in report.runtime.entries)
        count = runtime_count(report.runtime, report.as_of, project=project) if seen else None
        external_text = count.text if count else "не проверено"
        external_warning = " · проверьте лимит 1–2" if count and count.confirmed > 2 else ""
        parts.append(f'<article><h3>{esc(project)}</h3><p>Room lease: {len(active)} · '
                     f'проверенные runtime в работе: {external_text}{external_warning} · '
                     f'очередь: {counts["Новая / в очереди"]} · блокер: {counts["Блокер"]} · '
                     f'ревью: {counts["Ревью"]}</p>'
                     f'<p class="oh-meta">Фокус Room: {active_text}{overflow}</p></article>')
    return "".join(parts) or '<p class="oh-note">Открытых карточек нет.</p>'


def _runtime(report: HourlyReport) -> str:
    snapshot = report.runtime
    if snapshot is None:
        return ('<h2>Внешние исполнения</h2><p class="oh-note">Проверенной сводки runtime нет. '
                'Число фактически работающих задач неизвестно; Room lease учитываются отдельно.</p>')
    fresh = snapshot.is_fresh(report.as_of)
    active = sum(entry.state == "active" for entry in snapshot.entries)
    summaries = source_summaries(snapshot, report.as_of)
    total = runtime_count(snapshot, report.as_of)
    historical = [e for e in snapshot.entries if not snapshot.declared(e.source)]
    if not fresh:
        count = f'последняя проверка: {active} активных; текущий счёт неизвестен'
    elif total.confirmed:
        tail = f'; не подтверждено: {total.unproven}' if total.unproven else ''
        number = (str(total.confirmed) if total.complete
                  else f'не менее {total.confirmed} (покрытие неполное)')
        count = f'Подтверждённые активные потоки: {number}{tail}'
    elif total.proven_zero:
        count = 'Активных потоков нет (покрытие полное)'
    elif historical:
        count = 'Текущий счёт неизвестен: сообщение без покрытия источников'
    else:
        count = 'Активные потоки не подтверждены; нуль не утверждаем без полного покрытия'
    by_source = "".join(f'<li>{esc(x.text)}; наблюдение {local_dt(x.observed_at, empty="не указано")}</li>'
                        for x in summaries)
    labels = {"active": "В работе", "completed": "Этап завершён",
              "waiting": "Ожидание", "unknown": "Неизвестно"}
    note = (f'<p class="oh-meta">Историческое наблюдение на {local_dt(snapshot.checked_at)} '
            f'(не текущее состояние): записей без заявленного покрытия источника — '
            f'{len(historical)}; в текущие счётчики не входят.</p>' if historical else "")
    items = "".join(
        f'<article class="oh-card"><span class="oh-pill">'
        f'{"Историческое · " if not snapshot.declared(entry.source) else ""}'
        f'{labels[entry.state]}</span>'
        f'<h3>{esc(entry.project)}</h3><p>{esc(entry.detail)}</p>'
        f'<p class="oh-meta">Источник: {esc(entry.source)} {esc(entry.source_id or "без id")}; '
        'связь с карточкой Room не подтверждена</p></article>'
        for entry in snapshot.entries)
    return (f'<h2>Внешние исполнения</h2><p class="oh-note">{count}. '
            f'Проверено dot: {local_dt(snapshot.checked_at)}; запись Room '
            f'{esc(snapshot.message_id)}. Room «В работе» = {report.counts["В работе"]} '
            'живых lease. Общую сумму без подтверждённой связи не выводим.</p>'
            f'{note}<ul class="oh-meta">{by_source}</ul>'
            f'<div class="oh-projects">{items}</div>')


def scope_note(report: HourlyReport) -> str:
    total = report.hidden_excluded + report.hidden_inactive + report.hidden_runtime
    if not total:
        return ""
    return (f'<p class="oh-meta" id="oh-scope-note">Скрыто фильтром Office: {total} (SIN/STEP и '
            f'проекты вне мониторинга: {report.hidden_excluded}; не в работе в '
            f'{esc(report.active_only_label)}: {report.hidden_inactive}; '
            f'внешние runtime вне мониторинга: {report.hidden_runtime}).</p>')




KANBAN_CSS = """<style>
.kb-head{display:flex;justify-content:space-between;align-items:end;gap:1rem;flex-wrap:wrap}
.kb-head>*{min-width:0}.kb-head h1{margin-bottom:.25rem}.kb-sub,.kb-meta,.kb-empty{color:var(--muted);overflow-wrap:anywhere}
.kb-board,.kb-col,.kb-card,.kb-dialog{box-sizing:border-box;max-width:100%}
.kb-filter{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.kb-filter a{display:inline-block;padding:.45rem .75rem;border:1px solid var(--line);border-radius:2rem}
.kb-filter a[aria-current=page]{background:var(--bg-inset);color:var(--text-strong);font-weight:700}
.kb-board{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.8rem;align-items:start;margin:1.2rem 0;width:100%;min-width:0}
.kb-col{min-width:0;background:var(--bg-inset);border:1px solid var(--line);border-radius:var(--r-md);padding:.7rem}
.kb-col h2{font-size:1rem;margin:.2rem 0 .8rem}.kb-col h2 span{color:var(--muted);font-size:.85rem}
.kb-stack{display:grid;gap:.6rem}.kb-card{display:block;width:100%;text-align:left;color:var(--text-strong);background:var(--surface);border:1px solid var(--line);border-radius:var(--r-md);padding:.8rem;cursor:pointer;font:inherit}
.kb-card:hover,.kb-card:focus-visible{border-color:var(--accent-line);outline:2px solid var(--accent-line);outline-offset:1px}
.kb-card strong,.kb-card small{display:block}.kb-card strong{line-height:1.3}.kb-card small{margin-top:.4rem;color:var(--muted)}
.kb-card p{margin:.55rem 0 0;font-size:.86rem;overflow-wrap:anywhere}.kb-card .kb-risk{color:var(--warn)}
.kb-fresh{font-size:.75rem;color:var(--muted);margin-top:.4rem}.kb-info{margin:1rem 0}
.kb-info summary{cursor:pointer}.kb-dialog{width:min(60rem,calc(100% - 2rem));max-height:min(90dvh,55rem);padding:0;border:1px solid var(--line);border-radius:var(--r-md);background:var(--surface);color:var(--text-strong);box-shadow:0 2rem 5rem #0007}
.kb-dialog::backdrop{background:#101820b8}.kb-dialog-head{position:sticky;top:0;background:var(--surface);display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:1rem 1.2rem;border-bottom:1px solid var(--line);z-index:1}
.kb-dialog-head h2{margin:0;font-size:1.2rem}.kb-close{min-width:2.4rem;min-height:2.4rem;font-size:1.3rem;cursor:pointer}
.kb-dialog-content{padding:1.2rem;overflow-wrap:anywhere}.kb-fields{display:grid;grid-template-columns:minmax(9rem,12rem) 1fr;gap:.55rem 1rem}.kb-fields dt{color:var(--muted)}.kb-fields dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}
.kb-history{display:grid;gap:.55rem;padding:0;list-style:none}.kb-history li{border-top:1px solid var(--line);padding:.5rem 0;white-space:pre-wrap;overflow-wrap:anywhere}
body:has(.kb-dialog[open]){overflow:hidden}
@media(max-width:1100px){.kb-board{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:650px){.kb-board{grid-template-columns:1fr}.kb-dialog{width:100%;max-width:100%;max-height:100dvh;height:100dvh;border-radius:0}.kb-fields{grid-template-columns:1fr}.kb-fields dt{margin-top:.5rem}}
</style>"""

_USER_LABELS = {"Ревью": "На проверке"}
_FRESHNESS_LABELS = {"fresh": "свежие данные", "stale": "устаревшие данные",
                     "unknown": "неизвестно"}


def _user_label(status: str) -> str:
    return _USER_LABELS.get(status, status)


def _short(value: str | None, limit: int = 105) -> str:
    return safe_text(" ".join((value or "").split()), limit)


def _kanban_card(card: HourlyCard) -> str:
    row = card.row
    executor = (card.details.executor.name if card.details and card.details.executor else
                row.responsible or row.lease_holder or "не назначен")
    action = (card.question.question if card.info.open_question and card.question else
              card.info.wait_reason if card.status == "Блокер" else row.next_action)
    action = _short(action or card.info.dependency or "Следующий шаг не указан")
    url = f"/office/hourly/detail/{quote(row.room, safe='')}/{quote(row.task_id, safe='')}"
    risk = ' class="kb-risk"' if card.status == "Блокер" else ""
    confidence = (f'<div class="kb-fresh">Основание блокера: {esc(card.info.blocker_confidence)}</div>'
                  if card.info.blocker_confidence else '')
    return (f'<button type="button" class="kb-card" data-detail="{esc(url)}" '
            f'aria-label="Открыть задачу {esc(_short(row.title or row.task_id))}">'
            f'<strong>{esc(_short(row.title or row.task_id))}</strong>'
            f'<small>{esc(row.project or "Без проекта")} · {esc(executor)}</small>'
            f'<p{risk}>{esc(action)}</p>'
            f'<div class="kb-fresh">Актуальность: {esc(_FRESHNESS_LABELS.get(card.info.freshness, "неизвестно"))}</div>'
            f'{confidence}</button>')


def _kanban_board(report: HourlyReport, show_closed: bool) -> str:
    columns = []
    for status in STATUSES:
        cards = [c for c in report.cards if c.status == status]
        if status in (DONE, CANCELLED) and not show_closed:
            body = '<p class="kb-empty">Скрыто фильтром</p>'
        else:
            body = ''.join(_kanban_card(c) for c in cards) or '<p class="kb-empty">Нет задач</p>'
        columns.append(f'<section class="kb-col" aria-label="{esc(_user_label(status))}">'
                       f'<h2>{esc(_user_label(status))} <span>{len(cards) if show_closed or status not in (DONE, CANCELLED) else "—"}</span></h2>'
                       f'<div class="kb-stack">{body}</div></section>')
    return '<div class="kb-board">' + ''.join(columns) + '</div>'


def _scope_decisions(report: HourlyReport) -> str:
    if not report.scope_message_id:
        return ('<p class="kb-meta">Сверка решений владельца об отмене не загружена; '
                'доска показывает исходные статусы Room, полнота охвата неизвестна.</p>')
    rows = report.pending_cancellations or []
    if not rows:
        return (f'<p class="kb-meta">Сверка отмен по записи Room '
                f'{esc(report.scope_message_id)}: ожидающих записи отмен нет.</p>')
    items = []
    for row, source in sorted(rows, key=lambda pair: pair[0].task_id):
        url = f"/office/hourly/detail/{quote(row.room, safe='')}/{quote(row.task_id, safe='')}"
        items.append(f'<li><button type="button" class="kb-card" data-detail="{esc(url)}">'
                     f'<strong>{esc(_short(row.title or row.task_id))}</strong>'
                     f'<small>{esc(row.project or "Без проекта")} · запись Room: {esc(row.status)}'
                     f' · источник {esc(source)}</small></button></li>')
    return (f'<details class="kb-info"><summary>Отмена владельца ожидает записи в Room: {len(rows)}</summary>'
            f'<p class="kb-meta">По записи Room {esc(report.scope_message_id)} эти карточки исключены '
            'из рабочей доски. Их исходный статус и история сохранены; отмену должен записать '
            'штатный actor создателя.</p>'
            f'<ul class="kb-history">{"".join(items)}</ul></details>')


def report_body(report: HourlyReport, show_closed: bool = False) -> str:
    active = ' aria-current="page"'
    filters = (f'<nav class="kb-filter" aria-label="Фильтр задач">'
               f'<a href="/office/hourly"{active if not show_closed else ""}>Текущие</a>'
               f'<a href="/office/hourly?show_closed=true"{active if show_closed else ""}>С готовыми и отменёнными</a></nav>')
    summary = (f'<details class="kb-info"><summary>Сводка и источник данных</summary>'
               f'<p>Карточки — данные Room на {local_dt(report.as_of)}. Актуальность показана отдельно от статуса.</p>'
               f'{scope_note(report)}{_runtime(report)}{_projects(report)}</details>')
    js = """<script>
(()=>{const dialog=document.getElementById('kb-detail');const content=document.getElementById('kb-detail-content');const title=document.getElementById('kb-detail-title');const close=document.getElementById('kb-detail-close');let trigger=null;let pending=null;
document.addEventListener('click',async(event)=>{const button=event.target.closest('button[data-detail]');if(!button)return;trigger=button;title.textContent=button.querySelector('strong').textContent;content.textContent='Загрузка…';dialog.showModal();close.focus();if(pending)pending.abort();pending=new AbortController();try{const response=await fetch(button.dataset.detail,{credentials:'same-origin',signal:pending.signal});if(!response.ok)throw new Error('Детали недоступны ('+response.status+')');content.innerHTML=await response.text()}catch(error){if(error.name!=='AbortError')content.textContent=error.message}});
close.addEventListener('click',()=>dialog.close());dialog.addEventListener('click',(event)=>{if(event.target===dialog)dialog.close()});dialog.addEventListener('close',()=>{if(pending)pending.abort();if(trigger)trigger.focus()});
setInterval(()=>{if(!dialog.open)location.reload()},300000);
})();</script>"""
    return (f'{KANBAN_CSS}<div class="kb-head"><div><h1>Office · задачи</h1>'
            f'<p class="kb-sub">Текущая координация · обновлено {local_dt(report.as_of)}</p></div>{filters}</div>'
            f'{_scope_decisions(report)}{_kanban_board(report, show_closed)}{summary}'
            '<dialog id="kb-detail" class="kb-dialog" aria-labelledby="kb-detail-title">'
            '<div class="kb-dialog-head"><h2 id="kb-detail-title">Задача</h2>'
            '<button type="button" id="kb-detail-close" class="kb-close" aria-label="Закрыть">×</button></div>'
            '<div id="kb-detail-content" class="kb-dialog-content"></div></dialog>'
            f'{js}')


def modal_body(item, events, questions) -> str:
    """Read-only detail fragment. Escape and redact every source field before injecting it."""
    row = item.row
    result = (f'{local_dt(row.last_progress_at)} · {esc(safe_text(row.last_progress_text, 4000))}'
              if row.last_progress_at else 'Подтверждённого результата нет')
    refs = ', '.join(f'{safe_text(str(ref.get("kind")), 40)}: {safe_text(str(ref.get("ref")), 300)}'
                     for ref in (row.refs or []) if isinstance(ref, dict)) or 'не указан'
    fields = [
        ('Описание', row.note or 'не указано'),
        ('Состояние записи Room', row.status),
        ('Исполнитель', row.responsible or row.lease_holder or 'не назначен'),
        ('Последний подтверждённый результат', result),
        ('Следующий шаг', row.next_action or 'не указан'),
        ('Блокер / ожидание', row.waiting_reason or 'не указан'),
        ('Зависимости', ', '.join(map(str, row.depends_on or [])) or 'нет'),
        ('Источник', refs),
        ('Room ID', f'{row.room}/{row.task_id}'),
    ]
    field_html = ''.join(f'<dt>{esc(label)}</dt><dd>{value if label == "Последний подтверждённый результат" else esc(safe_text(value, 8000))}</dd>'
                         for label, value in fields)
    question_html = ''.join(f'<li>{esc(safe_text(q.question, 2000))} · {esc(q.status)}</li>'
                            for q, _ in questions)
    history = ''.join(f'<li><span class="kb-meta">{local_dt(e.at)} · {esc(e.kind)} · {esc(e.agent or "—")}</span><br>{esc(safe_text(e.text, 2000))}</li>'
                      for e in reversed(events)) or '<li>Событий нет</li>'
    return (f'<dl class="kb-fields">{field_html}</dl>'
            f'<h3>Вопросы</h3><ul class="kb-history">{question_html or "<li>Нет открытых вопросов</li>"}</ul>'
            f'<h3>История (последние 200 событий)</h3><ol class="kb-history">{history}</ol>')
