"""`/hub` — админка MCP-хаба (только владелец): апстримы, агенты, журнал вызовов."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from vera_shared.hub.names import HubNameError
from vera_shared.hub.repo_agents import (
    create_agent,
    list_agents,
    recent_calls,
    rotate_agent,
    update_agent,
)
from vera_shared.hub.repo_upstreams import list_upstreams, save_upstream
from vera_shared.hub.secrets import HubKeyMissing

from dashboard.csrf import owner_post_gate
from dashboard.hub_forms import (
    form_id,
    grants_from_form,
    secret_from_form,
    upstream_from_form,
)
from dashboard.hub_view import hub_intro, upstreams_section
from dashboard.hub_view_agents import agents_section, calls_section, token_page
from dashboard.render import _render, esc, owner_or_redirect

router = APIRouter()
CALLS_SHOWN = 100
_NO_STORE = {"Cache-Control": "no-store"}


def _error(message: str) -> HTMLResponse:
    return HTMLResponse(_render("hub", f"<h2>Не сохранено</h2><p class='err'>{esc(message)}</p>"
                                       "<p><a href='/hub'>← к хабу</a></p>"),
                        status_code=400, headers=_NO_STORE)


@router.get("/hub", response_class=HTMLResponse)
async def hub_page(request: Request) -> Response:
    if (resp := owner_or_redirect(request)) is not None:
        return resp
    upstreams = await list_upstreams()
    body = (hub_intro() + upstreams_section(upstreams)
            + agents_section(await list_agents(), upstreams)
            + calls_section(await recent_calls(CALLS_SHOWN)))
    return HTMLResponse(_render("hub", body, wide=True), headers=_NO_STORE)


@router.post("/hub/upstream")
async def hub_save_upstream(request: Request) -> Response:
    if (resp := owner_post_gate(request)) is not None:
        return resp
    form = await request.form()
    secret, clear = secret_from_form(form)
    try:
        await save_upstream(upstream_from_form(form), form_id(form), secret=secret,
                            clear_secret=clear)
    except (HubNameError, LookupError, HubKeyMissing) as exc:
        return _error(str(exc))
    return RedirectResponse("/hub", status_code=303)


@router.post("/hub/agent")
async def hub_save_agent(request: Request) -> Response:
    if (resp := owner_post_gate(request)) is not None:
        return resp
    form = await request.form()
    grants = grants_from_form([str(v) for v in form.getlist("allow")])
    note = str(form.get("note") or "")[:500]
    agent_id = form_id(form)
    try:
        if agent_id is None:
            name = str(form.get("name") or "").strip()
            token = await create_agent(name, grants, note)
            return HTMLResponse(_render("hub", token_page(name, token)), headers=_NO_STORE)
        await update_agent(agent_id, grants, form.get("enabled") == "on", note)
    except (HubNameError, LookupError) as exc:
        return _error(str(exc))
    return RedirectResponse("/hub", status_code=303)


@router.post("/hub/agent/rotate")
async def hub_rotate_agent(request: Request) -> Response:
    if (resp := owner_post_gate(request)) is not None:
        return resp
    agent_id = form_id(await request.form())
    agents = {a.id: a.name for a in await list_agents()}
    if agent_id not in agents:
        return _error("agent not found")
    token = await rotate_agent(agent_id)
    return HTMLResponse(_render("hub", token_page(agents[agent_id], token)), headers=_NO_STORE)
