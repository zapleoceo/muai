"""Выдача claim с answered_questions проходит маскирование MCP (токен в вопросе и ответе)."""
from __future__ import annotations

from vera_mcp.room_redact import task_view

TOKEN = "ghp_" + "CANARY" * 4  # маска redact_view; маска rlk_ появится в C1


def sample() -> dict:
    return {"task_id": "T", "title": "t", "answered_questions": [
        {"qid": 1, "question": f"вопрос {TOKEN}",
         "answers": [{"text": f"ответ {TOKEN}", "by": "owner", "at": "2026-10-23T10:00:00"}]}]}


def test_answered_questions_text_is_masked_and_shape_kept():
    out = task_view(sample())
    [aq] = out["answered_questions"]
    assert TOKEN not in str(out)
    assert aq["qid"] == 1 and aq["answers"][0]["by"] == "owner"
    assert aq["answers"][0]["at"] == "2026-10-23T10:00:00"


def test_task_without_answers_is_unchanged():
    assert "answered_questions" not in task_view({"task_id": "T", "title": "t"})
    assert task_view({"task_id": "T", "answered_questions": None})["answered_questions"] == []
