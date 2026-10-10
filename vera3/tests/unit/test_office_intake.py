"""Office intake: идемпотентное создание, ответы с ревизией, ACL «только своё», курсор выдачи."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select
from vera_shared.db.models_office import OfficeAnswerKeyRow, RoomTaskOriginRow
from vera_shared.db.models_room import RoomTaskRow
from vera_shared.office import acl, intake, repository
from vera_shared.office.acl import OfficeForbidden, OfficePrincipal
from vera_shared.office.intake_input import InvalidInput
from vera_shared.room import questions, tasks

pytestmark = pytest.mark.asyncio

ROOM = "office-test"
BOT = OfficePrincipal(name="vbot", project="Veranda")
BOT2 = OfficePrincipal(name="vbot2", project="Veranda")
OTHER_PROJECT = OfficePrincipal(name="vbot", project="Elsewhere")
ORIGIN = {"channel": "telegram", "chat": "-100", "thread": "7", "message": "55", "from_id": 9}
RECIPIENT = {"channel": "telegram", "chat": "-100", "reply_to": "55"}


@pytest.fixture(autouse=True)
def _room(monkeypatch, sqlite_db):
    monkeypatch.setenv("OFFICE_ROOM", ROOM)
    return sqlite_db


async def _create(principal=BOT, cid="c-1", body="Заказать воду", **kw):
    return await intake.create_task(principal, cid, kw.get("title", "Вода"), body,
                                    kw.get("origin", ORIGIN), kw.get("recipient", RECIPIENT),
                                    kw.get("causation_id"), kw.get("context"),
                                    kw.get("attachments"))


async def _count(model) -> int:
    from vera_shared.db.engine import get_session
    async with get_session() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar() or 0


async def _ask(task_id: str, text: str = "Сколько бутылок?") -> int:
    held = await tasks.claim(room=ROOM, task_id=task_id, agent="claude", lease_seconds=300)
    q = await questions.ask(room=ROOM, task_id=task_id, agent="claude",
                            fencing_token=held["fencing_token"], question=text)
    return q["qid"]


async def _revision(task_id: str) -> int:
    from vera_shared.db.engine import get_session
    async with get_session() as s:
        return await repository.revision_of(s, ROOM, task_id)


async def test_create_opens_room_task_in_principal_project():
    made = await _create()
    from vera_shared.db.engine import get_session
    async with get_session() as s:
        task = await s.get(RoomTaskRow, (ROOM, made["task_id"]))
        origin = await repository.origin_of(s, made["task_id"])
    assert made["created"] and made["task_id"].startswith("vb-")
    assert (task.project, task.auto_pickup, task.created_by) == ("Veranda", False, "vbot")
    assert origin.origin == ORIGIN and origin.result_recipient == RECIPIENT
    assert origin.correlation_id == f"{ROOM}/{made['task_id']}"
    assert made["revision"] > 0


async def test_same_payload_returns_existing_task_without_duplicates():
    first, again = await _create(), await _create()
    assert again["task_id"] == first["task_id"] and not again["created"]
    assert await _count(RoomTaskOriginRow) == 1 and await _count(RoomTaskRow) == 1


async def test_conflicting_payload_is_409():
    await _create()
    for changed in ({"body": "Совсем другой текст"}, {"origin": {**ORIGIN, "message": "56"}},
                    {"context": {"reply_to": {"id": "1"}}},
                    {"attachments": [{"type": "photo", "file_id": "f"}]}):
        with pytest.raises(intake.IntakeConflict) as err:
            await _create(**changed)
        assert err.value.code == "payload_mismatch"


async def test_concurrent_insert_race_resolves_to_existing_task(monkeypatch):
    first = await _create()
    real = repository.origin_by_client
    calls = {"n": 0}

    async def blind_once(s, principal, client_task_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(s, principal, client_task_id)

    monkeypatch.setattr(repository, "origin_by_client", blind_once)
    again = await _create()  # IntegrityError на UNIQUE(principal, client_task_id)
    assert again["task_id"] == first["task_id"] and not again["created"]
    assert await _count(RoomTaskOriginRow) == 1 and await _count(RoomTaskRow) == 1
    calls["n"] = 0
    with pytest.raises(intake.IntakeConflict):
        await _create(body="другое")  # гонка + другое содержимое = конфликт, не дубль
    assert await _count(RoomTaskOriginRow) == 1


async def test_origin_is_write_once():
    made = await _create()
    assert not [n for n in dir(repository) if "update_origin" in n or "set_origin" in n]
    from vera_shared.db.engine import get_session
    with pytest.raises(Exception):  # noqa: B017, PT011
        async with get_session() as s:
            await repository.insert_origin(
                s, room=ROOM, task_id="vb-x", creator_principal="vbot", client_task_id="c-1",
                payload_hash="h", origin={"channel": "evil"}, result_recipient={"channel": "x"})
    await intake.mark_delivered(BOT, made["task_id"], made["revision"])
    async with get_session() as s:
        row = await repository.origin_of(s, made["task_id"])
    assert row.origin == ORIGIN and row.result_recipient == RECIPIENT


async def test_answer_revision_mismatch_then_success_then_replay():
    made = await _create()
    qid = await _ask(made["task_id"])
    rev = await _revision(made["task_id"])
    with pytest.raises(intake.IntakeConflict) as err:
        await intake.answer(BOT, made["task_id"], qid, "Две", rev - 1, "k-1")
    assert err.value.code == "revision_mismatch" and await _count(OfficeAnswerKeyRow) == 0
    done = await intake.answer(BOT, made["task_id"], qid, "Две", rev, "k-1")
    assert done["status"] == "answered" and not done["replayed"] and done["revision"] > rev
    again = await intake.answer(BOT, made["task_id"], qid, "Две", rev, "k-1")
    assert again["replayed"] and again["revision"] == done["revision"]
    assert await _count(OfficeAnswerKeyRow) == 1
    with pytest.raises(intake.IntakeConflict) as err:
        await intake.answer(BOT, made["task_id"], qid, "Три", rev, "k-1")
    assert err.value.code == "payload_mismatch"
    # вопрос уже не открыт: новый ключ — не ответ
    with pytest.raises(intake.IntakeNotFound):
        await intake.answer(BOT, made["task_id"], qid, "Две", done["revision"], "k-2")


async def test_answer_only_open_question_of_own_task():
    mine, theirs = await _create(cid="a"), await _create(BOT2, cid="b")
    qid = await _ask(theirs["task_id"])
    rev = await _revision(theirs["task_id"])
    with pytest.raises(intake.IntakeNotFound):  # чужая задача неотличима от несуществующей
        await intake.answer(BOT, theirs["task_id"], qid, "x", rev, "k")
    with pytest.raises(intake.IntakeNotFound):
        await intake.answer(BOT, mine["task_id"], qid, "x", await _revision(mine["task_id"]), "k")
    with pytest.raises(intake.IntakeNotFound):
        await intake.answer(BOT, "vb-missing", qid, "x", 1, "k")


async def test_acl_other_project_and_default_deny():
    made = await _create()
    qid = await _ask(made["task_id"])
    rev = await _revision(made["task_id"])
    with pytest.raises(intake.IntakeNotFound):
        await intake.answer(OTHER_PROJECT, made["task_id"], qid, "x", rev, "k")
    with pytest.raises(intake.IntakeNotFound):
        await intake.mark_delivered(OTHER_PROJECT, made["task_id"], 1)
    with pytest.raises(OfficeForbidden):
        acl.authorize(OfficePrincipal(name="u", project="Veranda", type="user"), "list")
    with pytest.raises(OfficeForbidden):
        acl.authorize(BOT, "claim")  # claim/post в ACL нет
    with pytest.raises(OfficeForbidden):
        acl.authorize(OfficePrincipal(name="u", project="Veranda", own_tasks=False), "create")


async def test_list_is_own_only_with_stable_cursor():
    ids = [(await _create(cid=f"c{i}"))["task_id"] for i in range(5)]
    # SQLite хранит now() без микросекунд и сравнивает строки; задаём равные метки явно (ничья)
    from datetime import datetime

    from sqlalchemy import update
    from vera_shared.db.engine import get_session
    async with get_session() as s:
        await s.execute(update(RoomTaskRow).values(updated_at=datetime(2026, 1, 1, 0, 0, 0)))
    await _create(BOT2, cid="foreign")
    await _create(OTHER_PROJECT, cid="other-proj")
    seen, cursor = [], None
    for _ in range(5):
        page = await intake.list_tasks(BOT, None, cursor, "2")  # limit строкой
        seen += [t["task_id"] for t in page["tasks"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen)) == 5
    first = (await intake.list_tasks(BOT, None, None, 100))["tasks"][0]
    assert {"status", "questions", "result", "revision"} <= set(first)


async def test_list_shows_open_question_and_filters_by_updated_since():
    made = await _create()
    qid = await _ask(made["task_id"])
    item = (await intake.list_tasks(BOT))["tasks"][0]
    assert item["questions"] == [{"question_id": qid, "text": "Сколько бутылок?"}]
    assert item["revision"] == await _revision(made["task_id"])
    assert (await intake.list_tasks(BOT, "2999-01-01T00:00:00"))["tasks"] == []
    with pytest.raises(ValueError, match="limit"):
        await intake.list_tasks(BOT, None, None, 101)
    with pytest.raises(ValueError, match="cursor"):
        await intake.list_tasks(BOT, None, "###")


async def test_mark_delivered_is_idempotent_and_monotonic():
    made = await _create()
    await _ask(made["task_id"])
    top = await _revision(made["task_id"])
    assert (await intake.mark_delivered(BOT, made["task_id"], top))["delivered_revision"] == top
    assert (await intake.mark_delivered(BOT, made["task_id"], top))["delivered_revision"] == top
    assert (await intake.mark_delivered(BOT, made["task_id"], 1))["delivered_revision"] == top
    with pytest.raises(intake.IntakeConflict):
        await intake.mark_delivered(BOT, made["task_id"], top + 5)


def test_registry_parsing_is_strict():
    token = "t" * 32
    assert acl.parse_registry(f"vbot:Veranda:{token}")[token] == BOT
    for bad in ("vbot:Veranda:short", "vbot:Veranda", f"BAD NAME:Veranda:{token}",
                f"a:P:{token},b:P:{token}"):
        with pytest.raises(ValueError):
            acl.parse_registry(bad)
    with pytest.raises(ValueError, match="reuse"):
        acl.check_disjoint(acl.parse_registry(f"vbot:Veranda:{token}"), {token})
    assert acl.resolve(None, {}) is None


async def test_retry_with_same_key_after_revision_mismatch_is_allowed():
    made = await _create()
    qid = await _ask(made["task_id"])
    rev = await _revision(made["task_id"])
    with pytest.raises(intake.IntakeConflict):
        await intake.answer(BOT, made["task_id"], qid, "Две", rev - 1, "k-r")
    done = await intake.answer(BOT, made["task_id"], qid, "Две", rev, "k-r")
    assert not done["replayed"]


async def test_context_attachments_stored_write_once_and_redacted():
    ctx = {"reply_to": {"id": "5", "user": "anna", "text": "token sk-ant-api03-" + "a" * 40}}
    att = [{"type": "photo", "file_id": "F1", "size": 10, "url": "https://x/1?sig=a",
            "url_expires_at": "2026-10-12T00:00:00Z"}]
    made = await _create(context=ctx, attachments=att,
                         recipient={**RECIPIENT, "thread": "7"})
    from vera_shared.db.engine import get_session
    async with get_session() as s:
        row = await repository.origin_of(s, made["task_id"])
    assert "sk-ant" not in row.context["reply_to"]["text"] and row.attachments == att
    assert row.result_recipient["thread"] == "7"
    # другая подписанная ссылка — тот же payload; другой file_id — уже нет
    same = await _create(context=ctx, recipient={**RECIPIENT, "thread": "7"},
                         attachments=[{**att[0], "url": "https://x/1?sig=b",
                                       "url_expires_at": "2026-10-13T00:00:00Z"}])
    assert same["task_id"] == made["task_id"] and not same["created"]
    with pytest.raises(intake.IntakeConflict):
        await _create(context=ctx, recipient={**RECIPIENT, "thread": "7"},
                      attachments=[{**att[0], "file_id": "F2"}])


@pytest.mark.parametrize("kw", [
    {"origin": {**ORIGIN, "thread": None}}, {"recipient": {**RECIPIENT, "reply_to": None}},
    {"context": {"reply_to": {"id": None}}}, {"attachments": [{"type": "p", "file_id": None}]},
    {"attachments": [{"type": "p"}]}, {"attachments": [{"type": "p", "file_id": "f"}] * 11},
    {"attachments": [{"type": "p", "file_id": "f", "size": -1}]}, {"title": ""},
    {"causation_id": "bad id"}, {"origin": {"evil": 1}},
])
async def test_invalid_input_is_rejected(kw):
    with pytest.raises(InvalidInput):
        await _create(**kw)
    assert await _count(RoomTaskOriginRow) == 0


async def test_repeat_create_does_not_repost_and_flag_enables_auto_pickup(monkeypatch):
    from vera_shared.db.models_room import RoomMessageRow
    made = await _create()
    before = await _count(RoomMessageRow)
    await _create()
    assert await _count(RoomMessageRow) == before
    monkeypatch.setenv("OFFICE_AUTO_PICKUP", "1")
    other = await _create(cid="c-2")
    from vera_shared.db.engine import get_session
    async with get_session() as s:
        assert (await s.get(RoomTaskRow, (ROOM, other["task_id"]))).auto_pickup is True
    assert made["task_id"] != other["task_id"]


async def test_concurrent_create_in_progress_is_busy(monkeypatch):
    real = repository.origin_by_client
    state = {"n": 0}

    async def hide(s, principal, client_task_id):
        state["n"] += 1
        return None if state["n"] <= 2 else await real(s, principal, client_task_id)

    monkeypatch.setattr(repository, "origin_by_client", hide)
    await _create()  # первый вызов создаёт (n=1)
    state["n"] = 0
    with pytest.raises(intake.IntakeBusy):
        # insert_origin упадёт на UNIQUE, а повторное чтение всё ещё «не видит» строку
        monkeypatch.setattr(repository, "origin_by_client", lambda *a: _none())
        await _create()


async def _none():
    return None


async def test_list_reports_lease_live():
    made = await _create()
    assert (await intake.list_tasks(BOT))["tasks"][0]["lease_live"] is False
    await tasks.claim(room=ROOM, task_id=made["task_id"], agent="claude", lease_seconds=300)
    item = (await intake.list_tasks(BOT))["tasks"][0]
    assert item["status"] == "in_progress" and item["lease_live"] is True


async def test_answer_and_delivered_bump_updated_at_for_updated_since():
    from datetime import datetime

    from sqlalchemy import update
    from vera_shared.db.engine import get_session
    since = "2026-06-01T00:00:00"
    made = await _create()
    qid = await _ask(made["task_id"])

    async def age() -> None:
        async with get_session() as s:
            await s.execute(update(RoomTaskRow).values(updated_at=datetime(2026, 1, 1)))

    await age()
    rev = await _revision(made["task_id"])
    assert (await intake.list_tasks(BOT, since))["tasks"] == []
    done = await intake.answer(BOT, made["task_id"], qid, "Две", rev, "k-u")
    assert [t["task_id"] for t in (await intake.list_tasks(BOT, since))["tasks"]
            ] == [made["task_id"]]
    await age()
    await intake.mark_delivered(BOT, made["task_id"], done["revision"])
    assert len((await intake.list_tasks(BOT, since))["tasks"]) == 1
    await age()
    await intake.mark_delivered(BOT, made["task_id"], done["revision"])  # повтор: без изменений
    assert (await intake.list_tasks(BOT, since))["tasks"] == []
