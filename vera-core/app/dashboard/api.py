from datetime import datetime, timedelta

from fastapi import APIRouter, Cookie, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Agent, Task, Token

from app.config import get_settings
from app.dashboard.auth import (
    issue_session,
    require_owner,
    verify_telegram_auth,
)
from app.dashboard.balances import get_live_balances

router = APIRouter()


def _set_session_cookie(resp: Response) -> None:
    cookie, ttl = issue_session()
    resp.set_cookie(
        "vera_session", cookie,
        max_age=ttl, httponly=True, secure=True, samesite="strict", path="/",
    )


@router.get("/api/tg_login")
async def tg_login(request: Request) -> Response:
    settings = get_settings()
    data = dict(request.query_params)
    user_id = verify_telegram_auth(data)
    if user_id is None:
        return Response("invalid telegram signature", status_code=403)
    if user_id != settings.owner_telegram_id:
        return Response(f"access denied for user {user_id}", status_code=403)
    resp = RedirectResponse(url="/")
    _set_session_cookie(resp)
    return resp


@router.get("/api/logout")
async def logout() -> Response:
    resp = RedirectResponse(url="/")
    resp.delete_cookie("vera_session", path="/", secure=True)
    return resp


@router.get("/api/whoami")
async def whoami(_=Depends(require_owner)) -> dict:
    return {"role": "owner"}


@router.get("/api/csrf")
async def csrf(vera_session: str | None = Cookie(default=None)) -> dict:
    """Read-only endpoint that returns the per-session CSRF token.
    Frontend calls this once after login and stores in memory, sends as
    X-CSRF header on every mutating request."""
    from app.dashboard.auth import _verify, issue_csrf
    settings = get_settings()
    if not vera_session or _verify(vera_session, settings.session_secret) is None:
        from fastapi import HTTPException
        raise HTTPException(401, "unauthorized")
    return {"csrf": issue_csrf(vera_session)}


@router.get("/api/stats")
async def stats(_=Depends(require_owner)) -> dict:
    async with get_session() as session:
        total = (await session.execute(select(func.count(Task.id)))).scalar() or 0

        since_24h = datetime.utcnow() - timedelta(hours=24)
        last_24h = (await session.execute(
            select(func.count(Task.id)).where(Task.created_at >= since_24h)
        )).scalar() or 0

        avg_dur = (await session.execute(
            select(func.avg(Task.duration_ms)).where(Task.duration_ms.isnot(None))
        )).scalar()

        agents_online = (await session.execute(
            select(func.count(Agent.id)).where(Agent.status == "online")
        )).scalar() or 0

        token_total = (await session.execute(
            select(func.count(Token.id)).where(Token.is_active == True)
        )).scalar() or 0

    return {
        "tasks_total": total,
        "tasks_24h": last_24h,
        "avg_duration_ms": round(avg_dur) if avg_dur else None,
        "agents_online": agents_online,
        "tokens_active": token_total,
    }


@router.get("/api/tasks")
async def tasks(_=Depends(require_owner), limit: int = 50) -> list[dict]:
    async with get_session() as session:
        result = await session.execute(
            select(Task).order_by(Task.id.desc()).limit(limit)
        )
        rows = result.scalars().all()
    return [
        {
            "id": t.id,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "source": t.source,
            "user_id": t.user_id,
            "input_text": t.input_text,
            "final_result": t.final_result,
            "agents_used": t.agents_used or [],
            "quality_score": t.quality_score,
            "duration_ms": t.duration_ms,
            "status": t.status,
        }
        for t in rows
    ]


@router.get("/api/tokens")
async def tokens(_=Depends(require_owner)) -> list[dict]:
    async with get_session() as session:
        result = await session.execute(select(Token).order_by(Token.provider, Token.id))
        rows = result.scalars().all()
    now = datetime.utcnow()
    return [
        {
            "id": t.id,
            "provider": t.provider,
            "label": t.label,
            "capabilities": t.capabilities or [],
            "is_active": t.is_active,
            "daily_used": t.daily_used,
            "daily_limit": t.daily_limit,
            "in_cooldown": bool(t.cooldown_until and t.cooldown_until > now),
            "cooldown_until": t.cooldown_until.isoformat() if t.cooldown_until else None,
            "error_count": t.error_count,
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
            "tokens_in": t.tokens_in or 0,
            "tokens_out": t.tokens_out or 0,
            "cost_usd": float(t.cost_usd or 0.0),
        }
        for t in rows
    ]


@router.get("/api/balances")
async def balances(_=Depends(require_owner)) -> dict:
    live = await get_live_balances()

    async with get_session() as session:
        result = await session.execute(select(Token))
        rows = result.scalars().all()

    by_provider: dict[str, dict] = {}
    for t in rows:
        p = by_provider.setdefault(t.provider, {
            "spent_usd": 0.0, "tokens_in": 0, "tokens_out": 0,
            "requests": 0, "tokens_count": 0, "tokens_active": 0,
        })
        p["spent_usd"] += float(t.cost_usd or 0.0)
        p["tokens_in"] += t.tokens_in or 0
        p["tokens_out"] += t.tokens_out or 0
        p["requests"] += t.daily_used or 0
        p["tokens_count"] += 1
        if t.is_active:
            p["tokens_active"] += 1

    for p, info in by_provider.items():
        if p in live:
            info["live_balance"] = live[p]

    return by_provider


@router.get("/api/agents")
async def agents(_=Depends(require_owner)) -> list[dict]:
    async with get_session() as session:
        result = await session.execute(select(Agent))
        rows = result.scalars().all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "http_url": a.http_url,
            "status": a.status,
            "tools": a.tools or [],
            "capabilities": a.capabilities or [],
            "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
        }
        for a in rows
    ]
