"""HTML for the Office card detail block (pure rendering of CardDetails)."""
from __future__ import annotations

from dashboard.office_card_details import UNCHANGED, CardDetails, RefLink
from dashboard.office_status import StatusInfo
from dashboard.render import esc, local_dt


def _ref(ref: RefLink) -> str:
    label = f'{esc(ref.kind)}: {esc(ref.ref)}'
    if ref.url:
        return f'<a href="{esc(ref.url)}" rel="noopener noreferrer">{label}</a>'
    return label


def _delta(details: CardDetails) -> str:
    delta = details.delta
    if delta.unchanged:
        return UNCHANGED
    parts = []
    if delta.transitions:
        parts.append("переходы: " + ", ".join(
            f'{local_dt(at)} {esc(label)}' for at, label in delta.transitions))
    if delta.new_artifacts:
        parts.append(f'новых артефактов: {delta.new_artifacts}')
    return "; ".join(parts)


def details_html(details: CardDetails, info: StatusInfo) -> str:
    art = details.artifact
    rows = [("Артефакт", f'{local_dt(art.at)} · {esc(art.text or art.kind)}' if art
             else "Подтверждённого результата в Room нет")]
    if art and art.refs:
        rows.append(("Ссылки", "; ".join(_ref(r) for r in art.refs)))
    ex = details.executor
    rows.append(("Исполнитель", f'{esc(ex.name)} ({esc(ex.label)})' if ex else "не указан"))
    rows.append(("Следующий шаг", esc(details.next_action or "не указан")))
    if details.dependencies:
        rows.append(("Зависит от", "; ".join(
            f'{esc(d.task_id)} — {esc(d.status)}' if d.task_id else esc(d.status)
            for d in details.dependencies)))
    if info.dependency:
        rows.append(("Ожидание", esc(info.dependency)))
    if info.wait_reason:
        rows.append(("Причина", esc(info.wait_reason)))
    if info.waiting:
        rows.append(("Ожидание решения/проверки", "да; не блокер без явного свидетельства"))
        rows.append(("Кто действует", esc(info.decision_owner or "неизвестно")))
        rows.append(("Актуальность ожидания", esc(info.wait_freshness or "unknown")))
    rows.append(("За час", _delta(details)))
    body = "".join(f'<dt>{t}</dt><dd>{v}</dd>' for t, v in rows)
    return f'<dl class="oh-fields oh-details">{body}</dl>'
