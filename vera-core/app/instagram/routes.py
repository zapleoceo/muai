"""Instagram account management + auto-reply rules API."""
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


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

@router.get("/accounts")
async def list_accounts(_=Depends(require_owner)) -> list[dict]:
    async with get_session() as s:
        rows = (await s.execute(select(IgAccount).order_by(IgAccount.id))).scalars().all()
    return [_account_dict(a) for a in rows]


@router.post("/accounts")
async def upsert_account(payload: dict, _=Depends(require_owner)) -> dict:
    username = (payload.get("username") or "").strip().lstrip("@")
    if not username:
        raise HTTPException(400, "username required")
    access_token = (payload.get("access_token") or "").strip()
    business_id = (payload.get("business_account_id") or "").strip()

    async with get_session() as s:
        row = (await s.execute(
            select(IgAccount).where(IgAccount.username == username)
        )).scalar_one_or_none()
        if row is None:
            row = IgAccount(username=username)
            s.add(row)
        if access_token:
            # Encrypt token same way as Gmail tokens
            try:
                from vera_shared.tokens.repository import _encrypt
                row.access_token_enc = _encrypt(access_token)
            except Exception:
                row.access_token_enc = access_token  # fallback plain if encrypt unavailable
        if business_id:
            row.business_account_id = business_id
        if payload.get("display_name"):
            row.display_name = payload["display_name"]
        if "enabled" in payload:
            row.enabled = bool(payload["enabled"])
        row.status = "ok" if row.access_token_enc else "disconnected"
        row.updated_at = datetime.utcnow()
        await s.commit()
        await s.refresh(row)
    log.info("Instagram account upserted: @%s status=%s", username, row.status)
    # Wire up MCP server for this account if token present
    if access_token and business_id:
        await _ensure_mcp_server(username, access_token, business_id)
    return _account_dict(row)


@router.delete("/accounts/{username}")
async def delete_account(username: str, _=Depends(require_owner)) -> dict:
    async with get_session() as s:
        row = (await s.execute(
            select(IgAccount).where(IgAccount.username == username)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "account not found")
        await s.delete(row)
        await s.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Auto-reply rules
# ---------------------------------------------------------------------------

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
async def toggle_autoreply(rule_id: int, payload: dict,
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
            row.trigger_keywords = [k.strip().lower() for k in payload["trigger_keywords"] if k.strip()]
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _account_dict(a: IgAccount) -> dict:
    return {
        "id": a.id,
        "username": a.username,
        "display_name": a.display_name,
        "has_token": bool(a.access_token_enc),
        "business_account_id": a.business_account_id,
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


async def _ensure_mcp_server(username: str, access_token: str,
                              business_id: str) -> None:
    """Create or update the MCP server row for this Instagram account."""
    from vera_shared.db.models import MCPServer
    server_name = f"instagram-{username}"
    async with get_session() as s:
        row = (await s.execute(
            select(MCPServer).where(MCPServer.name == server_name)
        )).scalar_one_or_none()
        if row is None:
            row = MCPServer(
                name=server_name,
                transport="stdio",
                command=["npx", "-y", "@pinkpixel/instagram-engagement-mcp"],
                enabled=True,
                installed_by="instagram_module",
            )
            s.add(row)
        row.env = {
            "INSTAGRAM_ACCESS_TOKEN": access_token,
            "INSTAGRAM_BUSINESS_ACCOUNT_ID": business_id,
        }
        await s.commit()
    log.info("MCP server %s upserted for @%s", server_name, username)
    # Kick the MCP manager to reload
    try:
        from app.mcp.manager import refresh_from_db
        await refresh_from_db()
    except Exception as exc:
        log.warning("MCP refresh after IG account update failed: %s", exc)
