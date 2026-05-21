import json

from app.triage.engine import _safe_parse


def _normalise_like_engine(raw_actions: list[dict]) -> list[dict]:
    out: list[dict] = []
    for a in raw_actions[:5]:
        if not isinstance(a, dict):
            continue
        label = str(a.get("label", "")).strip()[:30]
        if not label:
            continue
        item = {
            "label": label,
            "description": str(a.get("description", "")).strip()[:300],
            "default": bool(a.get("default", False)),
        }
        tool = a.get("tool")
        if isinstance(tool, str) and tool.strip():
            item["tool"] = tool.strip()
            args = a.get("args")
            item["args"] = args if isinstance(args, dict) else {}
        out.append(item)
    return out


def test_action_with_tool_preserved():
    raw = json.dumps({
        "actions": [
            {"label": "Reply OK", "description": "send ack",
             "default": True, "tool": "tg_send", "args": {"peer": "x", "text": "ok"}},
            {"label": "Ignore", "description": "skip"},
        ]
    })
    data = _safe_parse(raw)
    actions = _normalise_like_engine(data["actions"])
    assert actions[0]["tool"] == "tg_send"
    assert actions[0]["args"] == {"peer": "x", "text": "ok"}
    assert "tool" not in actions[1]


def test_action_invalid_tool_dropped():
    raw = json.dumps({
        "actions": [
            {"label": "X", "description": "", "tool": "", "args": {"a": 1}},
            {"label": "Y", "description": "", "tool": 123, "args": {}},
        ]
    })
    actions = _normalise_like_engine(_safe_parse(raw)["actions"])
    assert all("tool" not in a for a in actions)


def test_action_args_must_be_dict():
    raw = json.dumps({
        "actions": [{"label": "Z", "description": "", "tool": "t", "args": "nope"}]
    })
    actions = _normalise_like_engine(_safe_parse(raw)["actions"])
    assert actions[0]["tool"] == "t"
    assert actions[0]["args"] == {}
