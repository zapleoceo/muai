from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.database import get_session
from app.db.repository import MessageRepo

router = APIRouter()
settings = get_settings()
bearer = HTTPBearer(auto_error=False)


def _check_auth(credentials: HTTPAuthorizationCredentials | None = Security(bearer)) -> None:
    if not settings.api_secret_key:
        return
    if not credentials or credentials.credentials != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/history")
async def get_history(
    chat_id: int = Query(..., description="Telegram chat_id"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    direction: str | None = Query(None, description="in | out"),
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    _: None = Depends(_check_auth),
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = MessageRepo(session)
    rows = await repo.get_messages(
        chat_id=chat_id,
        limit=limit,
        offset=offset,
        direction=direction,
        from_date=from_date,
        to_date=to_date,
    )
    return {
        "chat_id": chat_id,
        "count": len(rows),
        "messages": [
            {
                "id": r.id,
                "telegram_msg_id": r.telegram_msg_id,
                "user_id": r.user_id,
                "direction": r.direction,
                "text": r.text,
                "media_type": r.media_type,
                "caption": r.caption,
                "date_utc": r.date_utc.isoformat() if r.date_utc else None,
                "reply_to_msg_id": r.reply_to_msg_id,
                "is_auto_reply": r.is_auto_reply,
                "dialog_key": r.dialog_key,
            }
            for r in rows
        ],
    }
