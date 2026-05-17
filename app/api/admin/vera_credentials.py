"""CRUD for VERA tool credentials (perplexity, trello, gmail, poster, etc.)."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_owner
from app.db.database import AsyncSessionLocal

log = logging.getLogger(__name__)
router = APIRouter()

_TEMPLATES = {
    "perplexity": {"api_key": "pplx-..."},
    "trello": {"api_key": "...", "token": "..."},
    "gmail": {
        "token": "...",
        "refresh_token": "...",
        "client_id": "...",
        "client_secret": "...",
        "token_uri": "https://oauth2.googleapis.com/token",
    },
    "poster": {"token": "..."},
    "instagram": {"access_token": "...", "ig_user_id": "..."},
}


class VeraCredIn(BaseModel):
    type: str
    name: str = ""
    data: dict
    is_active: bool = True


class VeraCredOut(BaseModel):
    id: int
    type: str
    name: str
    is_active: bool


async def _session() -> AsyncSession:
    async with AsyncSessionLocal() as s:
        yield s


@router.get("/admin/vera-credentials")
async def list_vera_creds(_uid: int = Depends(require_owner)) -> list[dict]:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(text("SELECT id, type, name, is_active FROM vera_credentials ORDER BY id"))
        return [{"id": r.id, "type": r.type, "name": r.name, "is_active": r.is_active} for r in rows]


@router.get("/admin/vera-credentials/templates")
async def get_templates(_uid: int = Depends(require_owner)) -> dict:
    return _TEMPLATES


@router.post("/admin/vera-credentials", status_code=201)
async def upsert_vera_cred(body: VeraCredIn, _uid: int = Depends(require_owner)) -> dict:
    """Insert or replace credential for a given type."""
    async with AsyncSessionLocal() as s:
        await s.execute(
            text("""
                INSERT INTO vera_credentials (type, name, data, is_active)
                VALUES (:type, :name, :data::jsonb, :is_active)
                ON CONFLICT (type) DO UPDATE SET name=EXCLUDED.name, data=EXCLUDED.data, is_active=EXCLUDED.is_active
            """),
            {"type": body.type, "name": body.name, "data": __import__("json").dumps(body.data), "is_active": body.is_active},
        )
        await s.commit()
        row = await s.execute(text("SELECT id, type, name, is_active FROM vera_credentials WHERE type=:t"), {"t": body.type})
        r = row.one()
        return {"id": r.id, "type": r.type, "name": r.name, "is_active": r.is_active}


@router.patch("/admin/vera-credentials/{cred_id}/toggle")
async def toggle_vera_cred(cred_id: int, _uid: int = Depends(require_owner)) -> dict:
    async with AsyncSessionLocal() as s:
        await s.execute(
            text("UPDATE vera_credentials SET is_active = NOT is_active WHERE id=:id"),
            {"id": cred_id},
        )
        await s.commit()
        row = await s.execute(text("SELECT id, type, name, is_active FROM vera_credentials WHERE id=:id"), {"id": cred_id})
        r = row.one_or_none()
        if not r:
            raise HTTPException(status_code=404)
        return {"id": r.id, "is_active": r.is_active}


@router.delete("/admin/vera-credentials/{cred_id}", status_code=204)
async def delete_vera_cred(cred_id: int, _uid: int = Depends(require_owner)) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(text("DELETE FROM vera_credentials WHERE id=:id"), {"id": cred_id})
        await s.commit()
