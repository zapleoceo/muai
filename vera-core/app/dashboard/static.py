from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse

router = APIRouter()

_HTML_PATH = Path(__file__).parent / "index.html"


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    try:
        return HTMLResponse(_HTML_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse("<h1>Dashboard missing</h1>", status_code=500)


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nDisallow: /\n")
