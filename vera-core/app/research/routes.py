"""Bulk import of long-form research content (Perplexity Spaces dumps,
ChatGPT exports, raw markdown notes) into Graphiti as episodes."""
import hashlib
import logging
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException

from app.common.bg import spawn
from app.dashboard.auth import require_owner
from app.graph import write as gw

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/research")

_MAX_BODY_CHARS = 50_000
_MAX_DOCS_PER_BATCH = 50


def _short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


@router.post("/import")
async def import_dump(payload: dict = Body(...),
                       _=Depends(require_owner)) -> dict:
    """payload = {"source": "perplexity"|"chatgpt"|"notes",
                  "documents": [{"title": str, "body": str,
                                 "url"?: str, "date"?: ISO8601}]}

    Each document becomes a Graphiti episode. Fire-and-forget; returns
    immediately with the queued count."""
    source = (payload.get("source") or "").strip().lower() or "notes"
    docs = payload.get("documents") or []
    if not isinstance(docs, list) or not docs:
        raise HTTPException(400, "documents required")
    if len(docs) > _MAX_DOCS_PER_BATCH:
        raise HTTPException(400,
            f"max {_MAX_DOCS_PER_BATCH} docs per batch, "
            f"got {len(docs)}. Split into chunks.")

    queued = 0
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        title = (doc.get("title") or "").strip()[:200]
        body = (doc.get("body") or "").strip()[:_MAX_BODY_CHARS]
        if not body:
            continue
        url = (doc.get("url") or "").strip()[:500]
        date_s = (doc.get("date") or "").strip()
        when: datetime
        try:
            when = datetime.fromisoformat(date_s.replace("Z", "+00:00")) if date_s else datetime.utcnow()
        except Exception:
            when = datetime.utcnow()

        h = _short_hash((title or "") + "|" + body[:500])
        name = f"{source}/{h}"
        episode_body = (
            (f"# {title}\n\n" if title else "")
            + body
            + (f"\n\nSource URL: {url}" if url else "")
            + f"\n\nИсточник: {source}"
        )
        spawn(gw._add(name=name, body=episode_body, ref_time=when,
                      description=f"research:{source}"),
              name=f"research-import-{h}")
        queued += 1
    return {"ok": True, "queued": queued, "source": source}
