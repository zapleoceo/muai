"""Office intake: постраничный список своих задач с вопросами и результатом."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from vera_shared.db.engine import get_session
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.office import repository as repo
from vera_shared.office.acl import OfficePrincipal, authorize
from vera_shared.office.intake_common import office_room
from vera_shared.office.intake_input import (
    clean_limit,
    decode_cursor,
    encode_cursor,
    parse_since,
)
from vera_shared.room import questions
from vera_shared.timeutil import utc_naive_now
from vera_shared.voice_help.attest_state import is_attested
from vera_shared.voice_help.policy import attest_recipient_blocked


def _lease_live(t: RoomTaskRow) -> bool:
    return bool(t.status == "in_progress" and t.lease_until and t.lease_until > utc_naive_now())


async def _item(o: Any, t: RoomTaskRow) -> dict[str, Any]:
    async with get_session() as s:
        revision = await repo.revision_of(s, t.room, t.task_id)
        result = await repo.result_text_of(s, t.room, t.task_id) if t.status == "done" else None
    withheld = attest_recipient_blocked(
        t.refs, (o.result_recipient or {}).get("channel"),
        attested=await is_attested(t.room, t.task_id))
    if withheld:
        result = None
    open_q = [{"question_id": q["qid"], "text": q["question"]}
              for q in await questions.questions_of(t.room, t.task_id) if q["status"] == "open"]
    return {"task_id": t.task_id, "client_task_id": o.client_task_id, "status": t.status,
            "questions": open_q, "result": result, "result_withheld": withheld,
            "revision": revision, "lease_live": _lease_live(t),
            "delivered_revision": o.delivered_revision, "updated_at": t.updated_at.isoformat()}


async def list_tasks(principal: OfficePrincipal, updated_since: str | datetime | None = None,
                     cursor: str | None = None, limit: int | str | None = None,
                     ) -> dict[str, Any]:
    """Только свои задачи своего проекта; порядок и курсор — (updated_at, task_id)."""
    authorize(principal, "list")
    size = clean_limit(limit)
    since, after = parse_since(updated_since), decode_cursor(cursor)
    async with get_session() as s:
        page = await repo.own_tasks_page(
            s, principal=principal.name, project=principal.project, room=office_room(),
            since=since, after=after, limit=size + 1)
    more = len(page) > size
    page = page[:size]
    items = [await _item(o, t) for o, t in page]
    nxt = encode_cursor(page[-1][1].updated_at, page[-1][1].task_id) if more else None
    return {"tasks": items, "next_cursor": nxt}
