"""Proposer: send DM with a candidate, capture owner decision + creds via
reply-to-message. State machine: proposed → awaiting_creds → installing →
active | rejected | failed."""
import logging
from datetime import datetime

from sqlalchemy import select

from vera_shared.db.engine import get_session
from vera_shared.db.models import MCPProposal

from app.bot.sender import get_bot
from app.config import get_settings

log = logging.getLogger(__name__)


async def _send(text: str) -> int | None:
    settings = get_settings()
    bot = get_bot()
    try:
        msg = await bot.send_message(
            chat_id=settings.owner_telegram_id,
            text=text, parse_mode="HTML",
        )
        return msg.message_id
    except Exception as exc:
        log.warning("self_extend DM failed: %s", exc)
        return None


from app.common.text import html_escape as _html_escape  # noqa: E402


async def create_proposal(capability: str, candidate: dict,
                          source_event_id: int | None = None) -> int:
    async with get_session() as s:
        row = MCPProposal(
            capability=capability,
            package_name=candidate["name"],
            package_info=candidate,
            env_required=candidate.get("env_required") or [],
            source_event_id=source_event_id,
            status="proposed",
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
    await _send_proposal_card(row)
    return row.id


async def _send_proposal_card(proposal: MCPProposal) -> None:
    info = proposal.package_info or {}
    env_req = proposal.env_required or []
    lines = [
        f"✨ <b>Нашла инструмент для:</b> «{_html_escape(proposal.capability)}»",
        "",
        f"<b>Пакет:</b> <code>{_html_escape(info.get('name') or proposal.package_name)}</code>",
    ]
    if info.get("description"):
        lines.append(f"{_html_escape(info['description'])}")
    if info.get("publisher"):
        lines.append(f"<i>by {_html_escape(info['publisher'])}</i>")
    if env_req:
        lines.append("")
        lines.append(f"<b>Потребуется:</b> {', '.join(env_req)}")
    else:
        lines.append("")
        lines.append("<b>Credentials:</b> не нужны")
    lines.append("")
    lines.append(f"Чтобы поставить — ответь reply'ем на это сообщение:")
    lines.append(f"  <code>#proposal-{proposal.id} install</code>")
    lines.append(f"Отказаться:")
    lines.append(f"  <code>#proposal-{proposal.id} reject</code>")
    await _send("\n".join(lines))


async def handle_followup(proposal_id: int, text: str) -> str:
    """Called from bot handler when owner replies to a proposal/cred prompt."""
    async with get_session() as s:
        row = await s.get(MCPProposal, proposal_id)
        if row is None:
            return f"⚠️ Предложение #{proposal_id} не найдено."

    cmd = text.strip().lower().split()[0] if text.strip() else ""

    if row.status == "proposed":
        if cmd == "reject":
            await _mark_status(proposal_id, "rejected")
            return f"Ок, отказалась от #{proposal_id}."
        if cmd == "install":
            return await _start_collecting_creds(proposal_id)
        return ("Не поняла. Жду <code>install</code> или <code>reject</code> "
                "первым словом в reply.")

    if row.status == "awaiting_creds":
        return await _collect_cred(row, text)

    return f"Предложение #{proposal_id} уже в статусе {row.status}, ничего не делаю."


async def _mark_status(proposal_id: int, status: str, error: str | None = None,
                       mcp_server_id: int | None = None) -> None:
    async with get_session() as s:
        row = await s.get(MCPProposal, proposal_id)
        if not row:
            return
        row.status = status
        row.decided_at = row.decided_at or datetime.utcnow()
        if status in ("active", "rejected", "failed", "uninstalled"):
            row.completed_at = datetime.utcnow()
        if error:
            row.error = error[:500]
        if mcp_server_id:
            row.mcp_server_id = mcp_server_id
        await s.commit()


async def _start_collecting_creds(proposal_id: int) -> str:
    async with get_session() as s:
        row = await s.get(MCPProposal, proposal_id)
        env_req = row.env_required or []
        row.status = "awaiting_creds" if env_req else "installing"
        row.env_collected = {}
        row.decided_at = datetime.utcnow()
        await s.commit()
    if not env_req:
        from app.self_extend.installer import install_now
        return await install_now(proposal_id)
    first = env_req[0]
    return (f"Окей. Пришли значение для <code>{first}</code> "
            f"следующим reply'ем на это сообщение "
            f"<i>(#proposal-{proposal_id})</i>")


async def _collect_cred(row: MCPProposal, value: str) -> str:
    collected = dict(row.env_collected or {})
    needed = [k for k in (row.env_required or []) if k not in collected]
    if not needed:
        return "Все creds уже собраны, запускаю установку…"
    key = needed[0]
    collected[key] = value.strip()
    async with get_session() as s:
        fresh = await s.get(MCPProposal, row.id)
        fresh.env_collected = collected
        await s.commit()
    still_needed = [k for k in (row.env_required or []) if k not in collected]
    if still_needed:
        nxt = still_needed[0]
        return (f"Принято для <code>{key}</code>. Теперь <code>{nxt}</code> "
                f"<i>(#proposal-{row.id})</i>")
    from app.self_extend.installer import install_now
    return await install_now(row.id)


async def list_pending(limit: int = 10) -> list[dict]:
    async with get_session() as s:
        result = await s.execute(
            select(MCPProposal)
            .order_by(MCPProposal.id.desc())
            .limit(limit)
        )
        rows = result.scalars().all()
    return [
        {
            "id": r.id, "capability": r.capability,
            "package_name": r.package_name, "status": r.status,
            "env_required": r.env_required or [],
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "error": r.error,
        }
        for r in rows
    ]
