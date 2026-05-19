import time
import logging

import httpx
from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import Token

log = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 60.0


async def _deepseek_balance(api_key: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if r.status_code != 200:
            return {"error": f"http {r.status_code}"}
        data = r.json()
        bal_info = (data.get("balance_infos") or [{}])[0]
        return {
            "is_available": data.get("is_available"),
            "total_balance": float(bal_info.get("total_balance", 0)),
            "granted_balance": float(bal_info.get("granted_balance", 0)),
            "topped_up_balance": float(bal_info.get("topped_up_balance", 0)),
            "currency": bal_info.get("currency", "USD"),
        }
    except Exception as exc:
        log.warning("deepseek balance fetch failed: %s", exc)
        return {"error": str(exc)}


async def _first_active_token(provider: str) -> str | None:
    async with get_session() as session:
        result = await session.execute(
            select(Token.token).where(Token.provider == provider, Token.is_active == True).limit(1)
        )
        row = result.first()
    return row[0] if row else None


async def get_live_balances() -> dict:
    now = time.time()
    if "deepseek" in _CACHE and now - _CACHE["deepseek"][0] < _TTL:
        return {"deepseek": _CACHE["deepseek"][1]}

    out: dict = {}
    ds_key = await _first_active_token("deepseek")
    if ds_key:
        ds_bal = await _deepseek_balance(ds_key)
        out["deepseek"] = ds_bal
        _CACHE["deepseek"] = (now, ds_bal)
    return out
