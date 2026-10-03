"""План слияния дублей: составление по БД, применение, откат, отказ без отчёта."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from vera_shared.db.engine import get_session
from vera_shared.db.models import EventRow
from vera_shared.db.models_graph import EntityAliasRow, EntityRow, MembershipRow
from vera_shared.graph import dupe_apply
from vera_shared.graph.dupe_apply import (
    PlanError,
    apply_plan,
    plan_document,
    undo_report,
)
from vera_shared.graph.dupe_detect import build_plan
from vera_shared.graph.dupe_snapshot import load_snapshot
from vera_shared.graph.merge import merge_entities
from vera_shared.timeutil import utc_naive_now

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "merge_graph_duplicates.py"


async def _seed() -> dict[str, int]:
    ids: dict[str, int] = {}
    rows = (
        ("tg", "person", "Ivan Petrenko", {"tg_id": 11}, ["telegram:user:11"]),
        ("work", "person", "Иван Петренко", {}, ["gmail:ip@itstep.org"]),
        ("svc", "person", "Slack", {}, ["gmail:no-reply@slack.com"]),
        ("slack_org", "organization", "Slack", {}, ["gmail:notification@slack.com"]),
        ("generic", "organization", "noreply", {}, ["gmail:noreply@tuneprotect.com"]),
        ("ph", "person", "tg_user_77", {"tg_id": 77}, ["telegram:user:77"]),
        ("chan", "channel", "Some Channel", {"tg_id": 77}, ["telegram:chat:77"]),
        ("room", "supergroup", "Room", {}, []),
    )
    async with get_session() as s:
        for key, type_, name, attrs, aliases in rows:
            ent = EntityRow(type=type_, name=name, attributes=attrs)
            s.add(ent)
            await s.flush()
            ids[key] = ent.id
            for a in aliases:
                src, ident = a.split(":", 1)
                s.add(EntityAliasRow(entity_id=ent.id, source=src, identifier=ident))
        now = utc_naive_now()
        s.add(EventRow(source="telegram", source_event_id="e1", content_text="x",
                       occurred_at=now, received_at=now, triage_status="done",
                       metadata_={"chat_id": 77, "sender_id": 77}))
        s.add(MembershipRow(parent_entity_id=ids["room"], child_entity_id=ids["tg"],
                            source="telegram"))
        s.add(MembershipRow(parent_entity_id=ids["chan"], child_entity_id=ids["ph"],
                            source="telegram"))
    return ids


async def _state() -> list[tuple]:
    async with get_session() as s:
        ents = (await s.execute(select(EntityRow).order_by(EntityRow.id))).scalars().all()
        als = (await s.execute(select(EntityAliasRow).order_by(EntityAliasRow.id))).scalars().all()
        mems = (await s.execute(select(MembershipRow).order_by(MembershipRow.id))).scalars().all()
    return [
        [(e.id, e.type, e.name, json.dumps(e.attributes, sort_keys=True)) for e in ents],
        [(a.id, a.entity_id, a.source, a.identifier) for a in als],
        [(m.id, m.parent_entity_id, m.child_entity_id) for m in mems],
    ]


async def _plan() -> dict:
    return plan_document(build_plan(await load_snapshot()), "test")


@pytest.mark.usefixtures("sqlite_db")
class TestPlanApplyUndo:

    @pytest.mark.asyncio
    async def test_plan_finds_every_case_from_the_database(self):
        await _seed()
        plan = await _plan()
        assert {(a["case"], a["action"]) for a in plan["actions"]} >= {
            (1, "merge"), (2, "merge"), (2, "rename"), (4, "merge")}
        json.dumps(plan)  # план должен быть сериализуем

    @pytest.mark.asyncio
    async def test_apply_then_undo_restores_everything(self, tmp_path):
        await _seed()
        before = await _state()
        plan = await _plan()
        report = tmp_path / "rollback.json"
        result = await apply_plan(plan, report)
        assert result["entries"] and await _state() != before
        on_disk = json.loads(report.read_text(encoding="utf-8"))
        assert len(on_disk["entries"]) == len(result["entries"])
        assert await undo_report(report) == len(result["entries"])
        assert await _state() == before
        assert await undo_report(report) == 0  # повторный откат — пустая операция

    @pytest.mark.asyncio
    async def test_apply_result_matches_plan(self, tmp_path):
        ids = await _seed()
        await apply_plan(await _plan(), tmp_path / "r.json")
        async with get_session() as s:
            alive = {e.id: e for e in (await s.execute(select(EntityRow))).scalars()}
        assert ids["ph"] not in alive and ids["chan"] in alive
        assert {ids["tg"], ids["work"]} & set(alive) and not {ids["tg"], ids["work"]} <= set(alive)
        assert alive[ids["generic"]].name == "Tuneprotect"
        assert alive[ids["svc"]].type == "organization" or ids["svc"] not in alive

    @pytest.mark.asyncio
    async def test_apply_refuses_without_report_or_over_existing_report(self, tmp_path):
        await _seed()
        plan = await _plan()
        with pytest.raises(PlanError):
            await apply_plan(plan, None)
        existing = tmp_path / "r.json"
        existing.write_text("{}", encoding="utf-8")
        with pytest.raises(PlanError):
            await apply_plan(plan, existing)
        assert (await _state())[0]  # ничего не тронуто

    @pytest.mark.asyncio
    async def test_stale_action_is_skipped_not_applied(self, tmp_path):
        ids = await _seed()
        plan = await _plan()
        async with get_session() as s:
            (await s.get(EntityRow, ids["tg"])).name = "Renamed Since Plan"
        result = await apply_plan(plan, tmp_path / "r.json", cases={1})
        assert result["entries"] == [] and result["skipped"]

    @pytest.mark.asyncio
    async def test_case_filter_limits_what_is_applied(self, tmp_path):
        await _seed()
        result = await apply_plan(await _plan(), tmp_path / "r.json", cases={4})
        assert {e["case"] for e in result["entries"]} == {4}


@pytest.mark.usefixtures("sqlite_db")
class TestCrashSafety:

    @pytest.mark.asyncio
    async def test_intent_whose_transaction_never_committed_is_not_undone(self, tmp_path):
        ids = await _seed()
        before = await _state()
        entry = None
        with pytest.raises(RuntimeError):
            async with get_session() as s:
                rep = await merge_entities(ids["tg"], [ids["work"]], "x", session=s)
                entry = {"action_id": 1, "case": 1, "undone": False, "status": "intent",
                         "kind": "merge", "report": rep.to_dict()}
                raise RuntimeError("смерть до коммита")
        assert await _state() == before
        path = tmp_path / "r.json"
        path.write_text(json.dumps({"version": 1, "entries": [entry], "skipped": []}),
                        encoding="utf-8")
        assert await undo_report(path) == 0
        assert await _state() == before
        assert "не закоммичена" in json.loads(path.read_text(encoding="utf-8"))["entries"][0]["note"]

    @pytest.mark.asyncio
    async def test_intent_that_committed_is_undone(self, tmp_path):
        ids = await _seed()
        before = await _state()
        rep = await merge_entities(ids["tg"], [ids["work"]], "x")
        path = tmp_path / "r.json"
        path.write_text(json.dumps({"version": 1, "skipped": [], "entries": [
            {"action_id": 1, "case": 1, "undone": False, "status": "intent",
             "kind": "merge", "report": rep.to_dict()}]}), encoding="utf-8")
        assert await undo_report(path) == 1
        assert await _state() == before

    @pytest.mark.asyncio
    async def test_report_has_intent_on_disk_before_commit(self, tmp_path, monkeypatch):
        await _seed()
        plan = await _plan()
        seen = []
        real = dupe_apply._write_json

        def spy(path, data):
            seen.append([e["status"] for e in data["entries"]])
            real(path, data)

        monkeypatch.setattr(dupe_apply, "_write_json", spy)
        await apply_plan(plan, tmp_path / "r.json", cases={4})
        assert ["intent"] in seen and seen[-1] == ["done"]

    @pytest.mark.asyncio
    async def test_crash_mid_plan_leaves_a_rollback_that_restores_state(self, tmp_path, monkeypatch):
        await _seed()
        before = await _state()
        plan = await _plan()
        real, calls = dupe_apply._exec, []

        async def flaky(action, s):
            calls.append(action["id"])
            if len(calls) == 3:
                raise RuntimeError("упали на третьем")
            return await real(action, s)

        monkeypatch.setattr(dupe_apply, "_exec", flaky)
        report = tmp_path / "r.json"
        with pytest.raises(RuntimeError):
            await apply_plan(plan, report)
        assert await _state() != before
        assert await undo_report(report) == 2
        assert await _state() == before

    @pytest.mark.asyncio
    async def test_alias_change_after_plan_makes_action_stale(self, tmp_path):
        from vera_shared.db.models_graph import EntityAliasRow
        ids = await _seed()
        plan = await _plan()
        async with get_session() as s:
            s.add(EntityAliasRow(entity_id=ids["tg"], source="telegram", identifier="user:999"))
        result = await apply_plan(plan, tmp_path / "r.json", cases={1})
        assert result["entries"] == [] and result["skipped"]


class TestCli:

    def _load(self):
        spec = importlib.util.spec_from_file_location("merge_graph_duplicates", _SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_apply_without_report_is_refused(self, monkeypatch, capsys):
        module = self._load()
        monkeypatch.setattr("sys.argv", ["x", "--apply", "plan.json"])
        assert module.main() == 2
        assert "--report" in capsys.readouterr().err

    def test_plan_from_snapshot_file_needs_no_database(self, tmp_path, monkeypatch):
        module = self._load()
        snap = tmp_path / "snap.json"
        snap.write_text(json.dumps({"entities": [
            {"id": 1, "type": "person", "name": "Slack", "attributes": {}}],
            "aliases": [{"entity_id": 1, "source": "gmail", "identifier": "no-reply@slack.com"}],
            "degree": {}}), encoding="utf-8")
        out = tmp_path / "plan.json"
        monkeypatch.setattr("sys.argv", ["x", "--plan", str(out), "--snapshot", str(snap)])
        assert module.main() == 0
        assert json.loads(out.read_text(encoding="utf-8"))["counts"] == {"5": {"retype": 1}}
