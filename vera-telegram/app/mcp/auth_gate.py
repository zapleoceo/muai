import hashlib
import hmac
import time
from html import escape

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

_COOKIE = "mcp_owner"
_TTL = 600


def _sign(secret: str, expiry: int) -> str:
    sig = hmac.new(secret.encode(), str(expiry).encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def _valid(secret: str, value: str | None) -> bool:
    if not value or "." not in value:
        return False
    expiry, sig = value.rsplit(".", 1)
    if not expiry.isdigit():
        return False
    expected = hmac.new(secret.encode(), expiry.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig) and int(expiry) > time.time()


def _form(next_url: str, error: bool) -> HTMLResponse:
    note = '<p style="color:#c00">Wrong password</p>' if error else ""
    body = (
        "<!doctype html><meta charset=utf-8>"
        "<title>Vera Telegram — connect</title>"
        "<body style='font-family:system-ui;max-width:22rem;margin:4rem auto'>"
        "<h2>Connect Telegram to Claude</h2>"
        f"{note}"
        "<form method=post action='/owner-login'>"
        f"<input type=hidden name=next value='{escape(next_url)}'>"
        "<input type=password name=password placeholder='Owner password' "
        "autofocus style='width:100%;padding:.5rem;font-size:1rem'>"
        "<button style='margin-top:1rem;padding:.5rem 1rem'>Authorize</button>"
        "</form></body>"
    )
    return HTMLResponse(body, status_code=401 if error else 200)


class OwnerAuthGate:
    """Gate the OAuth /authorize endpoint behind an owner-password login."""

    def __init__(self, secret: str, password: str) -> None:
        self._secret = secret
        self._password = password

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path.rstrip("/")
        if path.endswith("/authorize") and request.method == "GET":
            if not _valid(self._secret, request.cookies.get(_COOKIE)):
                return _form(str(request.url), False)
        return await call_next(request)

    async def login(self, request: Request) -> Response:
        form = await request.form()
        password = str(form.get("password", ""))
        next_url = str(form.get("next", "/"))
        if not (self._password and hmac.compare_digest(password, self._password)):
            return _form(next_url, True)
        resp = RedirectResponse(next_url, status_code=303)
        resp.set_cookie(
            _COOKIE,
            _sign(self._secret, int(time.time()) + _TTL),
            max_age=_TTL,
            httponly=True,
            secure=True,
            samesite="lax",
        )
        return resp
