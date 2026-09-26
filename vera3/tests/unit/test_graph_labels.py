"""Russian labels for graph relationship predicates."""
from __future__ import annotations

from dashboard.graph_labels import (
    PREDICATE_LABELS,
    predicate_hint,
    predicate_label,
    predicate_labels_json,
    predicate_options_html,
)
from dashboard.graph_routes import _PREDICATES
from vera_shared.graph.rel_extract import PREDICATES


def test_every_known_predicate_has_label() -> None:
    for code in [*PREDICATES, *_PREDICATES]:
        assert code in PREDICATE_LABELS, code


def test_known_labels() -> None:
    assert predicate_label("coworker_of") == "работает с"
    assert predicate_label("member_of") == "состоит в"
    assert predicate_hint("friend_of") == "друзья"


def test_unknown_code_falls_back_readably() -> None:
    assert predicate_label("mentor_of") == "mentor"
    assert predicate_label("likes_music") == "likes music"
    assert predicate_label("") == "без типа"
    assert predicate_hint("mentor_of") == "mentor_of"


def test_options_show_labels_keep_codes_sorted() -> None:
    out = predicate_options_html(["works_at", "boss_of", "x<y"])
    assert '<option value="works_at" title="сотрудник организации">работает в</option>' in out
    assert out.index("начальник") < out.index("работает в")
    assert "x&lt;y" in out and "x<y" not in out


def test_labels_json_is_script_safe() -> None:
    out = predicate_labels_json(["coworker_of", "a</script>"])
    assert "работает с" in out
    assert "</script>" not in out
