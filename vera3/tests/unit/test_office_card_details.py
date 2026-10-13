"""Pure derivations behind Office card details."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from dashboard.office_card_details import (
    TEXT_LIMIT,
    artifact_of,
    build_details,
    delta_of,
    dependencies_of,
    executor_of,
    ref_links,
    safe_text,
)
from dashboard.office_card_view import details_html
from dashboard.office_status import describe_status

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def row(**kw) -> SimpleNamespace:
    base = {"room": "main", "task_id": "T", "lease_holder": None, "lease_until": None,
                "responsible": None, "owner": None, "next_action": None, "refs": [],
                "depends_on": [],
                "status": "open", "last_progress_at": None, "waiting_reason": None,
                "waiting_until": None}
    return SimpleNamespace(**{**base, **kw})


def ev(kind: str, minutes_ago: int, text: str = "x", data=None) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, at=NOW - timedelta(minutes=minutes_ago), text=text,
                           data=data, id=minutes_ago)


def test_executor_priority_and_labels() -> None:
    live = row(lease_holder="dot", lease_until=NOW + timedelta(minutes=5),
               responsible="r", owner="o")
    assert executor_of(live, NOW).label == "держатель аренды"
    expired = row(lease_holder="dot", lease_until=NOW - timedelta(minutes=1),
                  responsible="r", owner="o")
    assert (executor_of(expired, NOW).name, executor_of(expired, NOW).label) == (
        "r", "ответственный")
    assert executor_of(row(owner="o"), NOW).label == "владелец"
    assert executor_of(row(), NOW) is None


def test_safe_text_redacts_and_truncates() -> None:
    assert "sk-ant-api03-" not in safe_text("key sk-ant-api03-" + "a" * 40)
    assert len(safe_text("я" * 1000)) == TEXT_LIMIT + 1


def test_refs_links_only_for_http_urls() -> None:
    refs = [{"kind": "url", "ref": "https://e.org/x", "excerpt": "z"},
            {"kind": "url", "ref": "javascript:alert(1)"},
            {"kind": "jira", "ref": "SIN-1"}, "bad", {"kind": "", "ref": "q"}]
    got = ref_links(refs)
    assert [(r.kind, r.url) for r in got] == [
        ("url", "https://e.org/x"), ("url", None), ("jira", None)]
    assert ref_links(None) == ()


def test_artifact_prefers_event_refs_else_task_refs() -> None:
    task_refs = [{"kind": "jira", "ref": "SIN-9"}]
    own = ev("progress", 5, "done it", {"refs": [{"kind": "event", "ref": "42"}]})
    assert artifact_of(own, row(refs=task_refs)).refs[0].ref == "42"
    plain = artifact_of(ev("done", 5), row(refs=task_refs))
    assert plain.refs[0].ref == "SIN-9"
    assert artifact_of(None, row()) is None


def test_dependencies_use_status_info() -> None:
    dep = row(task_id="D", status="done")
    infos = {("main", "D"): describe_status(dep, NOW)}
    states = dependencies_of(row(depends_on=["D", "ghost"]), infos)
    assert [(d.task_id, d.status) for d in states] == [("D", "Готово"), ("ghost", "не найдена")]


def test_delta_window_and_unchanged() -> None:
    d = delta_of([ev("blocked", 10), ev("paused", 90), ev("cancelled", 3)],
                 [ev("progress", 20), ev("progress", 120)], NOW)
    assert [label for _, label in d.transitions] == ["блокер", "отменена"]
    assert d.new_artifacts == 1
    assert not d.unchanged
    assert delta_of([], [], NOW).unchanged


def test_html_unchanged_and_escaping() -> None:
    r = row(next_action="<b>go</b>", responsible="r")
    details = build_details(r, None, NOW, {}, [], [])
    html = details_html(details, describe_status(r, NOW))
    assert "без изменений" in html
    assert "&lt;b&gt;go" in html
    assert "<b>go" not in html
    assert "Подтверждённого результата в Room нет" in html
