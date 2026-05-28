"""Instagram account management + auto-reply rules API.

Connect flow:
  POST /api/instagram/accounts  {username, password}
  → instagrapi login → session saved encrypted → status=ok
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import IgAccount, IgAutoReply

from app.dashboard.auth import require_owner

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/instagram")


# ── accounts ─────────────────────────────────────────────────────────────────

@router.get("/accounts")
async def list_accounts(_=Depends(require_owner)) -> list[dict]:
    async with get_session() as s:
        rows = (await s.execute(
            select(IgAccount).order_by(IgAccount.id)
        )).scalars().all()
    return [_account_dict(a) for a in rows]


@router.post("/accounts")
async def connect_account(payload: dict, _=Depends(require_owner)) -> dict:
    """Login via instagrapi and persist encrypted session."""
    username = (payload.get("username") or "").strip().lstrip("@")
    password = (payload.get("password") or "").strip()
    if not username or not password:
        raise HTTPException(400, "username and password required")

    from app.instagram.client import login, evict
    try:
        enc_session, user_id = await login(username, password)
    except Exception as exc:
        err = str(exc)
        # Persist error state so dashboard shows it
        async with get_session() as s:
            row = (await s.execute(
                select(IgAccount).where(IgAccount.username == username)
            )).scalar_one_or_none()
            if row is None:
                row = IgAccount(username=username)
                s.add(row)
            row.status = "error"
            row.last_error = err[:500]
            row.updated_at = datetime.utcnow()
            await s.commit()
        raise HTTPException(400, f"Instagram login failed: {err}")

    async with get_session() as s:
        row = (await s.execute(
            select(IgAccount).where(IgAccount.username == username)
        )).scalar_one_or_none()
        if row is None:
            row = IgAccount(username=username)
            s.add(row)
        row.access_token_enc = enc_session
        row.business_account_id = user_id    # reuse field for instagrapi user_id
        row.status = "ok"
        row.last_error = None
        row.enabled = True
        row.updated_at = datetime.utcnow()
        await s.commit()
        await s.refresh(row)

    log.info("Instagram @%s connected (user_id=%s)", username, user_id)
    return _account_dict(row)


@router.delete("/accounts/{username}")
async def delete_account(username: str, _=Depends(require_owner)) -> dict:
    from app.instagram.client import evict
    evict(username)
    async with get_session() as s:
        row = (await s.execute(
            select(IgAccount).where(IgAccount.username == username)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "account not found")
        await s.delete(row)
        await s.commit()
    return {"ok": True}


# ── auto-reply rules ──────────────────────────────────────────────────────────

@router.get("/autoreplies")
async def list_autoreplies(
    account: str | None = None, _=Depends(require_owner)
) -> list[dict]:
    async with get_session() as s:
        q = select(IgAutoReply).order_by(IgAutoReply.id)
        if account:
            q = q.where(IgAutoReply.account_username == account)
        rows = (await s.execute(q)).scalars().all()
    return [_rule_dict(r) for r in rows]


@router.post("/autoreplies")
async def create_autoreply(payload: dict, _=Depends(require_owner)) -> dict:
    account = (payload.get("account_username") or "").strip()
    keywords = payload.get("trigger_keywords") or []
    template = (payload.get("response_template") or "").strip()
    if not account or not keywords or not template:
        raise HTTPException(400, "account_username, trigger_keywords, response_template required")
    async with get_session() as s:
        row = IgAutoReply(
            account_username=account,
            trigger_keywords=[k.strip().lower() for k in keywords if k.strip()],
            response_template=template,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
    return _rule_dict(row)


@router.patch("/autoreplies/{rule_id}")
async def update_autoreply(rule_id: int, payload: dict,
                           _=Depends(require_owner)) -> dict:
    async with get_session() as s:
        row = await s.get(IgAutoReply, rule_id)
        if row is None:
            raise HTTPException(404, "rule not found")
        if "enabled" in payload:
            row.enabled = bool(payload["enabled"])
        if "response_template" in payload:
            row.response_template = payload["response_template"]
        if "trigger_keywords" in payload:
            row.trigger_keywords = [k.strip().lower()
                                    for k in payload["trigger_keywords"] if k.strip()]
        await s.commit()
        await s.refresh(row)
    return _rule_dict(row)


@router.delete("/autoreplies/{rule_id}")
async def delete_autoreply(rule_id: int, _=Depends(require_owner)) -> dict:
    async with get_session() as s:
        row = await s.get(IgAutoReply, rule_id)
        if row is None:
            raise HTTPException(404, "rule not found")
        await s.delete(row)
        await s.commit()
    return {"ok": True}


# ── helpers ───────────────────────────────────────────────────────────────────

def _account_dict(a: IgAccount) -> dict:
    return {
        "id": a.id,
        "username": a.username,
        "display_name": a.display_name,
        "has_session": bool(a.access_token_enc),
        "user_id": a.business_account_id,
        "enabled": a.enabled,
        "status": a.status,
        "last_polled_at": a.last_polled_at.isoformat() if a.last_polled_at else None,
        "last_error": a.last_error,
        "poll_interval_sec": a.poll_interval_sec,
    }


def _rule_dict(r: IgAutoReply) -> dict:
    return {
        "id": r.id,
        "account_username": r.account_username,
        "trigger_keywords": r.trigger_keywords,
        "response_template": r.response_template,
        "enabled": r.enabled,
        "match_count": r.match_count,
        "last_matched_at": r.last_matched_at.isoformat() if r.last_matched_at else None,
    }
