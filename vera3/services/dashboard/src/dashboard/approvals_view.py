"""Страница «Подтверждения»: разрушительные правки агентов и факты, записанные агентом."""
from __future__ import annotations

import json

from vera_shared.db.models import EventRow
from vera_shared.db.models_mcp import McpAuditRow

from dashboard.render import esc, local_dt

TOOL_RU = {"hide_event": "Скрыть событие", "relationship_retire": "Снять связь",
           "entity_merge": "Объединить карточки"}

_SCRIPT = """<script>
document.addEventListener('click', function(e){
  var b = e.target.closest('[data-decide]');
  if (!b) return;
  b.disabled = true;
  VeraUI.post('/api/approvals/decide', {kind: b.dataset.kind, id: Number(b.dataset.id),
                                        approve: b.dataset.decide === 'yes'})
    .then(function(){ location.reload(); })
    .catch(function(err){ b.disabled = false; VeraUI.toast('Не вышло: ' + err.message, {kind: 'err'}); });
});
</script>"""


def _buttons(kind: str, item_id: int) -> str:
    return (f'<div><button type="button" class="sm" data-decide="yes" data-kind="{kind}" '
            f'data-id="{item_id}">Подтвердить</button> '
            f'<button type="button" class="secondary sm" data-decide="no" data-kind="{kind}" '
            f'data-id="{item_id}">Отклонить</button></div>')


def _proposal_details(row: McpAuditRow) -> str:
    preview = (row.after or {}).get("preview") or {}
    args = json.dumps(row.args or {}, ensure_ascii=False)
    if row.tool == "entity_merge" and preview:
        drops = ", ".join(f"{esc(d.get('name'))} (#{esc(d.get('id'))})"
                          for d in preview.get("drops", []))
        keep = preview.get("keep") or {}
        return (f"Оставить {esc(keep.get('name'))} (#{esc(keep.get('id'))}), "
                f"влить: {drops}. Причина: {esc((row.args or {}).get('reason'))}")
    if row.tool == "hide_event" and preview:
        return (f"Событие #{esc(row.target_id)} ({esc(preview.get('source'))}): "
                f"{esc(preview.get('excerpt'))}")
    return esc(args)


def proposal_html(row: McpAuditRow) -> str:
    return (f'<div class="journal-row"><div><div class="j-what">'
            f'{esc(TOOL_RU.get(row.tool, row.tool))}: {_proposal_details(row)}</div>'
            f'<div class="j-meta">№{row.id} · предложил {esc(row.client)} · '
            f'{local_dt(row.created_at)}</div></div>{_buttons("proposal", row.id)}</div>')


def memory_html(row: EventRow) -> str:
    return (f'<div class="journal-row"><div><div class="j-what">{esc(row.content_text)}</div>'
            f'<div class="j-meta">событие #{row.id} · записал агент поиска · '
            f'{local_dt(row.occurred_at)}</div></div>{_buttons("memory", row.id)}</div>')


def approvals_body(proposals: list[McpAuditRow], memories: list[EventRow]) -> str:
    head = ('<div class="page-head"><div><h1>Подтверждения</h1>'
            '<p>Агенты читают чужие письма и чаты, поэтому разрушительные правки и '
            'факты, записанные агентом, ждут вашего решения. Подтверждённое можно '
            'вернуть в журнале правок.</p></div></div>')
    if not proposals and not memories:
        return head + '<div class="empty"><strong>Ждущих решения нет</strong></div>'
    parts = [head]
    if proposals:
        parts.append("<h2>Правки агентов</h2><article>"
                     + "".join(proposal_html(r) for r in proposals) + "</article>")
    if memories:
        parts.append("<h2>Факты от агента поиска</h2><article>"
                     + "".join(memory_html(r) for r in memories) + "</article>")
    return "".join(parts) + _SCRIPT
