"""LLM response → JSON parsing — handles fenced markdown, trailing prose,
and outright garbage."""
from vera_shared.llm.json_parse import safe_parse, strip_fence


def test_strip_fence_handles_plain_json():
    assert strip_fence('{"a": 1}') == '{"a": 1}'


def test_strip_fence_removes_json_fence():
    inp = '```json\n{"a": 1}\n```'
    assert strip_fence(inp) == '{"a": 1}'


def test_strip_fence_removes_plain_fence():
    assert strip_fence('```\n{"a": 1}\n```') == '{"a": 1}'


def test_safe_parse_plain():
    assert safe_parse('{"k": "v"}') == {"k": "v"}


def test_safe_parse_with_fence():
    assert safe_parse('```json\n{"k": "v"}\n```') == {"k": "v"}


def test_safe_parse_with_trailing_prose():
    raw = 'Sure! Here is your JSON:\n{"k": "v"}\nLet me know if you need more.'
    assert safe_parse(raw) == {"k": "v"}


def test_safe_parse_returns_none_for_garbage():
    assert safe_parse("totally not json at all") is None


def test_safe_parse_returns_none_for_empty():
    assert safe_parse("") is None
