"""redact_fields и обёртки поверх него дают тот же выход, что прежние локальные циклы."""
from __future__ import annotations

from typing import Any

from vera_shared.redact_view import MASK, redact_fields, redact_tree, redact_view

SECRET = "ghp_" + "a" * 36
TEXT = f"key {SECRET} here"
TREE = {"token": "x", "nested": [f"see {SECRET}"], "n": 5}


def _old_event_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("content_text", "triage_error"):
        if isinstance(out.get(key), str):
            out[key] = redact_view(out[key])
    for key in ("content_extra", "metadata"):
        if out.get(key) is not None:
            out[key] = redact_tree(out[key])
    return out


def _old_search(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    if isinstance(out.get("answer"), str):
        out["answer"] = redact_view(out["answer"])
    if isinstance(out.get("agent_trace"), list):
        out["agent_trace"] = redact_tree(out["agent_trace"])
    if isinstance(out.get("results"), list):
        out["results"] = [{**r, "content_preview": redact_view(r["content_preview"])}
                          if isinstance(r, dict) and isinstance(r.get("content_preview"), str)
                          else r for r in out["results"]]
    return out


def _old_mask(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: redact_view(v) if k in keys and isinstance(v, str) else v for k, v in item.items()}


def test_copy_masks_only_named_keys_and_keeps_order() -> None:
    src = {"id": SECRET, "body": TEXT, "n": 1}
    got = redact_fields(src, text_keys=("body",))
    assert list(got) == ["id", "body", "n"]
    assert got["id"] == SECRET and MASK in got["body"] and got["n"] == 1
    assert src["body"] == TEXT


def test_text_keys_skip_non_strings_tree_keys_skip_none() -> None:
    got = redact_fields({"a": 5, "b": None, "c": None}, text_keys=("a", "missing"),
                        tree_keys=("b", "missing"))
    assert got == {"a": 5, "b": None, "c": None}


def test_dashboard_event_row_unchanged() -> None:
    from dashboard.redaction import redact_event_row
    for row in ({"id": 1, "content_text": TEXT, "triage_error": None,
                 "content_extra": TREE, "metadata": [TEXT], "subject": TEXT},
                {"content_text": 7, "content_extra": "plain " + SECRET, "metadata": None},
                {}):
        assert redact_event_row(row) == _old_event_row(row)


def test_gateway_search_unchanged() -> None:
    from gateway.query import _redact_search
    for data in ({"answer": TEXT, "agent_trace": [TREE], "cost_usd": 0.1,
                  "results": [{"event_id": 1, "content_preview": TEXT},
                              {"event_id": 2, "content_preview": None}, "raw", 3]},
                 {"answer": None, "agent_trace": [], "results": None},
                 {}):
        assert _redact_search(data) == _old_search(data)


def test_room_views_unchanged() -> None:
    from vera_mcp.room_redact import message_view, question_view, task_view
    msg = {"message_id": SECRET, "body": TEXT, "status": "request"}
    assert message_view(msg) == {**_old_mask(msg, ("body",)), "truncated": False}
    task = {"task_id": "T", "title": TEXT, "note": None, "fencing_token": SECRET,
            "refs": [{"kind": "url", "ref": SECRET, "excerpt": TEXT}, "x"]}
    want = _old_mask(task, ("title", "note", "next_action", "last_progress_text",
                            "waiting_reason"))
    want["refs"] = [_old_mask(task["refs"][0], ("excerpt",)), "x"]
    assert task_view(task) == want
    q = {"id": 1, "question": TEXT, "answers": [{"by": "o", "text": TEXT}]}
    assert question_view(q) == {**_old_mask(q, ("question",)),
                                "answers": [_old_mask(q["answers"][0], ("text",))]}


def test_brain_search_preview_unchanged() -> None:
    from brain_search.models import SearchResult
    from brain_search.synthesis import _redacted
    r = SearchResult(event_id=1, score=0.5, source="telegram", occurred_at="2026-01-01",
                     content_preview=TEXT, importance=None, source_url=f"https://x/?token={SECRET}")
    got = _redacted(r)
    assert got == r.model_copy(update={"content_preview": redact_view(r.content_preview)})
    assert MASK in got.content_preview
