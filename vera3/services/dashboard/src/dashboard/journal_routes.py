"""`/journal` — журнал правок (`mcp_audit`) с откатом через `/api/journal/undo`."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from vera_shared.graph.connection_data import entity_cards, relationship_triples
from vera_shared.journal.audit import recent_rows

from dashboard.journal_view import entity_ids, journal_body, relationship_ids
from dashboard.render import _render, owner_or_redirect

router = APIRouter()
ENTRIES_SHOWN = 80


@router.get("/journal", response_class=HTMLResponse)
async def journal_page(request: Request):
    if (resp := owner_or_redirect(request)) is not None:
        return resp
    rows = await recent_rows(ENTRIES_SHOWN)
    triples = await relationship_triples(relationship_ids(rows))
    cards = await entity_cards(entity_ids(rows, triples))
    names = {i: c.name for i, c in cards.items()}
    return HTMLResponse(_render("journal", journal_body(rows, names, triples)))
