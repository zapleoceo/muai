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


Triples = dict[int, tuple[int, str, int]]
_ID_KEYS = ("subject_entity_id", "object_entity_id", "subject_id", "object_id",
            "entity_a", "entity_b", "entity_id")


def relationship_ids(rows: list[McpAuditRow]) -> list[int]:
    """Записи связей, концы которых в строке журнала не сохранены (правка из SQL/MCP без снимка)."""
    ids: set[int] = set()
    for r in rows:
        if r.target_kind != "relationship":
            continue
        rel = (r.args or {}).get("relationship_id") or r.target_id
        if isinstance(rel, int) and not _triple_from_row(r):
            ids.add(rel)
    return sorted(ids)


def entity_ids(rows: list[McpAuditRow], triples: Triples | None = None) -> list[int]:
    found: set[int] = set()
    for r in rows:
        for snap in (r.before, r.after, r.args):
            for key in _ID_KEYS:
                value = (snap or {}).get(key)
                if isinstance(value, int):
                    found.add(value)
    for r in rows:
        if r.target_kind == "merge":
            report = r.before or {}
            found.update(i for i in [report.get("keep_id"), *report.get("drop_ids", [])] if isinstance(i, int))
    for subject, _, obj in (triples or {}).values():
        found.update((subject, obj))
    return sorted(found)


def _name(names: dict[int, str], entity_id: Any) -> str:
    if not isinstance(entity_id, int):
        return "неизвестный человек"
    return esc(names.get(entity_id, f"человек №{entity_id}"))


def _triple_from_row(row: McpAuditRow) -> tuple[int, str, int] | None:
    snaps = (row.after, row.before) if row.tool == "relationship_move" else (row.before, row.after)
    for snap in snaps:
        s, p, o = (snap or {}).get("subject_entity_id"), (snap or {}).get("predicate"), \
            (snap or {}).get("object_entity_id")
        if isinstance(s, int) and isinstance(o, int) and p:
            return s, str(p), o
    a = row.args or {}
    if isinstance(a.get("subject_id"), int) and isinstance(a.get("object_id"), int) and a.get("predicate"):
        return a["subject_id"], str(a["predicate"]), a["object_id"]
    return None


def _relationship_text(row: McpAuditRow, names: dict[int, str], triples: Triples) -> str:
    triple = _triple_from_row(row)
    if triple is None:
        rel = (row.args or {}).get("relationship_id") or row.target_id
        triple = triples.get(rel) if isinstance(rel, int) else None
    verb = {"relationship_retire": "Связь погашена",
            "relationship_move": "Связь перенесена",
            "relationship_revive": "Связь возвращена"}.get(row.tool, "Связь задана")
    if triple is None:
        return f"{verb}: запись №{esc(row.target_id)}"
    s, p, o = triple
    return f"{verb}: {_name(names, s)} — {esc(predicate_label(p))} — {_name(names, o)}"


def describe_entry(row: McpAuditRow, names: dict[int, str], triples: Triples | None = None) -> str:
    if row.undo_of is not None:
        return f"Откат записи №{row.undo_of}"
    if row.target_kind == "relationship":
        return _relationship_text(row, names, triples or {})
    if row.target_kind == "suppression":
        after = row.after or {}
        return (f"«Работает с» отвергнуто: {_name(names, after.get('entity_a'))} — "
                f"{_name(names, after.get('entity_b'))}")
    if row.target_kind == "merge":
        report = row.before or {}
        drops = ", ".join(_name(names, i) for i in report.get("drop_ids", []))
        return f"Объединены карточки: {_name(names, report.get('keep_id'))} ← {drops}"
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


def entry_html(row: McpAuditRow, names: dict[int, str], triples: Triples | None = None) -> str:
    done = " undone" if row.status == "undone" else ""
    who = esc(CLIENT_RU.get(row.client, row.client))
    return (f'<div class="journal-row{done}"><div><div class="j-what">{describe_entry(row, names, triples)}</div>'
            f'<div class="j-meta">№{row.id} · {who} · {local_dt(row.created_at)}</div></div>'
            f'{_action(row)}</div>')


def journal_body(rows: list[McpAuditRow], names: dict[int, str], triples: Triples | None = None) -> str:
    head = ('<div class="page-head"><div><h1>Журнал правок</h1>'
            '<p>Всё, что менялось вручную или через агентов. Любую правку можно вернуть.</p></div></div>')
    if not rows:
        return head + ('<div class="empty"><strong>Правок пока нет</strong>'
                       'Разорванные связи и правки агентов появятся здесь.</div>')
    items = "".join(entry_html(r, names, triples) for r in rows)
    return head + f'<article>{items}</article>{_JOURNAL_SCRIPT}'
