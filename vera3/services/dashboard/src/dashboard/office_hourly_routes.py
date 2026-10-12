"""Owner-only live hourly report within the Vera dashboard."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from dashboard.office_hourly_service import load_report
from dashboard.office_hourly_view import report_body
from dashboard.render import _render, owner_or_redirect

router = APIRouter()


@router.get("/office/hourly", response_class=HTMLResponse)
async def office_hourly(request: Request) -> HTMLResponse:
    if (resp := owner_or_redirect(request)) is not None:
        return resp
    return HTMLResponse(_render("office", report_body(await load_report())),
                        headers={"Cache-Control": "private, no-store"})
