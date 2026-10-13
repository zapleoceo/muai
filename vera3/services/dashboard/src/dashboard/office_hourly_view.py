"""Owner-facing hourly Room report inside the existing dashboard shell."""
from __future__ import annotations

from collections import Counter, defaultdict
from urllib.parse import quote

from dashboard.office_hourly_service import (
    HourlyCard,
    HourlyReport,
    next_question_reminder,
)
from dashboard.render import esc, local_dt

CSS = """<style>
.oh-head{display:flex;justify-content:space-between;gap:1rem;align-items:end;flex-wrap:wrap}
.oh-head p,.oh-note{color:var(--muted)}.oh-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(12rem,1fr));gap:.7rem;margin:1.2rem 0}
.oh-tile,.oh-card{border:1px solid var(--line);border-radius:var(--r-md);padding:1rem;background:var(--surface)}
.oh-tile strong{display:block;font-size:1.8rem;color:var(--text-strong)}.oh-tile span{font-size:.8rem;color:var(--muted)}
.oh-list{display:grid;gap:.7rem}.oh-card h3{margin:.1rem 0 .5rem;font-size:1rem}.oh-card p{margin:.3rem 0}
.oh-meta{font-size:.78rem;color:var(--muted)}.oh-pill{display:inline-block;border-radius:1rem;padding:.2rem .6rem;font-size:.75rem;background:var(--bg-inset);margin:.2rem .4rem .2rem 0}
.oh-blocker{border-color:var(--warn)}.oh-fields{display:grid;grid-template-columns:8rem 1fr;gap:.3rem .7rem;margin:.8rem 0;font-size:.85rem}
.oh-fields dt{color:var(--muted)}.oh-fields dd{margin:0;overflow-wrap:anywhere}.oh-projects{display:grid;grid-template-columns:repeat(auto-fit,minmax(16rem,1fr));gap:.7rem}
.oh-projects article{border:1px solid var(--line);border-radius:var(--r-md);padding:.8rem}.oh-projects h3{margin:.1rem 0 .5rem;font-size:.95rem}
@media(max-width:600px){.oh-fields{grid-template-columns:1fr}.oh-fields dt{margin-top:.4rem}}
</style>"""


def _task_url(card: HourlyCard) -> str:
    return f"/tasks/{quote(card.row.room, safe='')}/{quote(card.row.task_id, safe='')}"


def _result_line(card: HourlyCard) -> str:
    event = card.last_confirmed
    if event is None:
        return esc(card.evidence)
    return f'{esc(card.evidence)} · {local_dt(event.at)} · {esc(event.text or event.kind)}'


def _blocker(card: HourlyCard, now) -> str:
    q = card.question
    question = esc(q.question) if q else "Конкретный вопрос владельцу не зафиксирован"
    reminder = (f'Следующий шестичасовой контроль: {local_dt(next_question_reminder(q, now))}'
                if q else "Повторять нечего до формулировки вопроса")
    return (f'<dl class="oh-fields"><dt>Вопрос</dt><dd>{question}</dd>'
            '<dt>Рекомендация</dt><dd>Не зафиксирована в источнике</dd>'
            '<dt>Варианты</dt><dd>Не зафиксированы в источнике</dd>'
            '<dt>Последствия</dt><dd>Не зафиксированы в источнике</dd>'
            f'<dt>Следующий шаг</dt><dd>{esc(card.row.next_action or "Не зафиксирован")}</dd>'
            f'<dt>Контроль</dt><dd>{reminder}</dd></dl>')


def _card(card: HourlyCard, now) -> str:
    row = card.row
    extra = _blocker(card, now) if card.info.open_question else ""
    label = (f'{card.status} · {card.info.blocker_confidence}'
             if card.info.blocker_confidence else card.status)
    reason = "".join(f'<p>{title}: {esc(value)}</p>' for title, value in (
        ("Причина ожидания", card.info.wait_reason), ("Зависимость", card.info.dependency))
        if value)
    return (f'<article class="oh-card{" oh-blocker" if extra else ""}">'
            f'<span class="oh-pill">{esc(label)}</span>'
            f'<span class="oh-meta">{esc(row.project or "Без проекта")} · {esc(row.room)}/{esc(row.task_id)}</span>'
            f'<h3><a href="{_task_url(card)}">{esc(row.title or row.task_id)}</a></h3>'
            f'<p>Ответственный: {esc(row.responsible or row.lease_holder or "не указан")}</p>'
            f'<p>Следующее действие: {esc(row.next_action or "не указано")}</p>'
            f'{reason}'
            f'<p class="oh-meta">{esc(card.coordination_note)}</p>'
            f'<p class="oh-meta">Сопоставление с внешним runtime: {esc(card.runtime_mapping)}</p>'
            f'<p class="oh-meta">Последний зафиксированный результат: {_result_line(card)}</p>'
            f'{extra}</article>')


def _projects(report: HourlyReport) -> str:
    groups: dict[str, list[HourlyCard]] = defaultdict(list)
    for card in report.cards:
        groups[card.row.project or "Без проекта"].append(card)
    external: dict[str, Counter[str]] = {}
    if report.runtime and report.runtime.is_fresh(report.as_of):
        for entry in report.runtime.entries:
            key = entry.project.casefold()
            external.setdefault(key, Counter())[entry.state] += 1
    parts = []
    for project, cards in sorted(groups.items(), key=lambda x: x[0].casefold()):
        counts = Counter(c.status for c in cards)
        active = [c for c in cards if c.status == "В работе"]
        active_text = ", ".join(esc(c.row.title or c.row.task_id) for c in active[:2]) or "нет подтверждённой живой аренды"
        overflow = f' · ещё {len(active) - 2} в работе — проверьте лимит' if len(active) > 2 else ""
        observed = external.get(project.casefold())
        external_text = (f'{observed["active"]} подтверждено'
                         + (f', {observed["unknown"]} неизвестно' if observed["unknown"] else "")
                         if observed is not None else "не проверено")
        external_warning = (" · проверьте лимит 1–2" if observed and observed["active"] > 2
                            else "")
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
    count = (f'Подтверждённые активные потоки: {active}' if fresh else
             f'последняя проверка: {active} активных; текущий счёт неизвестен')
    labels = {"active": "В работе", "completed": "Этап завершён",
              "waiting": "Ожидание", "unknown": "Неизвестно"}
    items = "".join(
        f'<article class="oh-card"><span class="oh-pill">{labels[entry.state]}</span>'
        f'<h3>{esc(entry.project)}</h3><p>{esc(entry.detail)}</p>'
        f'<p class="oh-meta">Источник: Codex thread {esc(entry.thread_id)}; '
        'связь с карточкой Room не подтверждена</p></article>'
        for entry in snapshot.entries)
    return (f'<h2>Внешние исполнения</h2><p class="oh-note">{count}. '
            f'Проверено dot: {local_dt(snapshot.checked_at)}; запись Room '
            f'{esc(snapshot.message_id)}. Room «В работе» = {report.counts["В работе"]} '
            'живых lease. Общую сумму без подтверждённой связи не выводим.</p>'
            f'<div class="oh-projects">{items}</div>')


def report_body(report: HourlyReport) -> str:
    counts = report.counts
    tiles = "".join(f'<article class="oh-tile"><strong>{counts[label]}</strong><span>{label}</span></article>'
                    for label in ("Новая / в очереди", "В работе", "Блокер", "Ревью"))
    events = "".join(
        f'<li>{local_dt(e.at)} · <a href="/tasks/{quote(e.room, safe="")}/{quote(e.task_id, safe="")}">'
        f'{esc(e.task_id)}</a> · {esc(e.text or e.kind)}</li>' for e in report.results)
    results = f'<ul>{events}</ul>' if events else '<p class="oh-note">Зафиксированных результатов за час нет.</p>'
    cards = "".join(_card(c, report.as_of) for c in report.cards)
    return (f'{CSS}<div class="oh-head"><div><h1>Офис · часовой отчёт</h1>'
            f'<p>Окно: {local_dt(report.window_start)} — {local_dt(report.as_of)}. '
            'Страница обновляется каждые пять минут.</p></div>'
            f'<div class="oh-meta">Проверено: {local_dt(report.as_of)}</div></div>'
            '<p class="oh-note">Один Room ID — одна карточка. Статус исполнения не выводится из количества open/blocked. '
            'Для Broker, Orchestra и других внешних runtime без точной связи с Room ID сопоставление неизвестно; '
            'их не прибавляем к карточному счётчику. Свежесть результата указана отдельно от статуса.</p>'
            f'{_runtime(report)}'
            f'<h2>Статусы карточек Room</h2><div class="oh-grid">{tiles}</div>'
            f'<p class="oh-meta">Незакрытых карточек: {len(report.cards)}. '
            f'Внешняя связь не подтверждена: {sum(c.runtime_mapping == "Не установлена" for c in report.cards)}. '
            'Готовые и отменённые не входят в отчёт: ни в списки, ни в показатели. Данные сохранены в Room.</p>'
            f'<h2>Результаты за час по Room</h2><p class="oh-note">Это записи исполнителей с точным временем; '
            f'независимое подтверждение проверяйте по ссылке на карточку.</p>{results}'
            f'<h2>Проекты</h2><div class="oh-projects">{_projects(report)}</div>'
            f'<h2>Карточки кроме готовых</h2><div class="oh-list">{cards}</div>'
            '<script>setTimeout(function(){location.reload()},300000)</script>')
