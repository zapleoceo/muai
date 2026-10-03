"""Страница «Журнал правок»: что менял владелец (дашборд) и агенты (MCP), с откатом."""
from __future__ import annotations

from typing import Any

from vera_shared.db.models_mcp import McpAuditRow

from dashboard.graph_labels import predicate_label
from dashboard.render import esc, local_dt

UNDOABLE_KINDS = frozenset({"relationship", "suppression", "entity", "alias", "event", "merge"})
CLIENT_RU = {"dashboard": "дашборд"}

_JOURNAL_SCRIPT = """<script>
document.addEventListener('click', function(e){
  var b = e.target.closest('[data-undo]');
  if (!b) return;
  b.disabled = true;
  VeraUI.post('/api/journal/undo', {audit_ids: [Number(b.dataset.undo)]})
    .then(function(){ location.reload(); })
    .catch(function(err){ b.disabled = false; VeraUI.toast('Не вернуть: ' + err.message, {kind: 'err'}); });
});
</script>"""


def entity_ids(rows: list[McpAuditRow]) -> list[int]:
    found: set[int] = set()
    for r in rows:
        for snap in (r.before, r.after, r.args):
            for key in ("subject_entity_id", "object_entity_id", "entity_a", "entity_b", "entity_id"):
                value = (snap or {}).get(key)
                if isinstance(value, int):
                    found.add(value)
    return sorted(found)


def _name(names: dict[int, str], entity_id: Any) -> str:
    return esc(names.get(entity_id, f"#{entity_id}"))


def _relationship_text(row: McpAuditRow, names: dict[int, str]) -> str:
    snap = row.before or row.after or {}
    who = (f"{_name(names, snap.get('subject_entity_id'))} — "
           f"{esc(predicate_label(str(snap.get('predicate', ''))))} — "
           f"{_name(names, snap.get('object_entity_id'))}")
    if row.tool == "relationship_retire":
        return f"Связь погашена: {who}"
    return f"Связь задана: {who}"


def describe_entry(row: McpAuditRow, names: dict[int, str]) -> str:
    if row.undo_of is not None:
        return f"Откат записи №{row.undo_of}"
    if row.target_kind == "relationship":
        return _relationship_text(row, names)
    if row.target_kind == "suppression":
        after = row.after or {}
        return (f"«Работает с» отвергнуто: {_name(names, after.get('entity_a'))} — "
                f"{_name(names, after.get('entity_b'))}")
    if row.target_kind == "merge":
        return "Слияние сущностей"
    if row.target_kind == "entity":
        return f"Переименование: {esc((row.before or {}).get('name'))} → {esc((row.after or {}).get('name'))}"
    return f"{esc(row.tool)} · {esc(row.target_kind)} #{esc(row.target_id)}"


def _action(row: McpAuditRow) -> str:
    if row.undo_of is not None:
        return ""
    if row.status == "undone":
        return '<span class="pill off">возвращено</span>'
    if row.target_kind in UNDOABLE_KINDS:
        return f'<button type="button" class="secondary sm" data-undo="{row.id}">Вернуть</button>'
    return ""


def entry_html(row: McpAuditRow, names: dict[int, str]) -> str:
    done = " undone" if row.status == "undone" else ""
    who = esc(CLIENT_RU.get(row.client, row.client))
    return (f'<div class="journal-row{done}"><div><div class="j-what">{describe_entry(row, names)}</div>'
            f'<div class="j-meta">№{row.id} · {who} · {local_dt(row.created_at)}</div></div>'
            f'{_action(row)}</div>')


def journal_body(rows: list[McpAuditRow], names: dict[int, str]) -> str:
    head = ('<div class="page-head"><div><h1>Журнал правок</h1>'
            '<p>Всё, что менялось вручную или через агентов. Любую правку можно вернуть.</p></div></div>')
    if not rows:
        return head + ('<div class="empty"><strong>Правок пока нет</strong>'
                       'Разорванные связи и правки агентов появятся здесь.</div>')
    items = "".join(entry_html(r, names) for r in rows)
    return head + f'<article>{items}</article>{_JOURNAL_SCRIPT}'
