"""vera_shared.style — per-listener voice profile + prompt rendering."""
from __future__ import annotations

import pytest

from vera_shared.style import (
    SampleMessage,
    StyleProfile,
    render_style_prompt,
)


def test_default_profile_constructs():
    p = StyleProfile()
    assert p.speaker == "dima"
    assert p.formality == "unknown"
    assert p.sample_messages == []
    assert p.code_switching == {}


def test_profile_to_payload_roundtrip():
    p = StyleProfile(
        listener_entity_id=42,
        listener_label="Маша",
        formality="ty",
        avg_length_chars=86,
        emoji_per_msg=0.6,
        frequent_emoji=["🙏", "🤙"],
        openings=["Слушай,"],
        closings=["Обнимаю"],
        sample_messages=[
            SampleMessage(event_id=1, text="привет", occurred_at="2026-06-01"),
        ],
        confidence=0.83,
        based_on_n_messages=234,
    )
    payload = p.to_payload()
    restored = StyleProfile.from_payload(payload)
    assert restored.listener_label == "Маша"
    assert restored.formality == "ty"
    assert restored.frequent_emoji == ["🙏", "🤙"]
    assert restored.confidence == 0.83
    assert len(restored.sample_messages) == 1
    assert restored.sample_messages[0].event_id == 1


def test_render_prompt_with_no_profile_falls_back():
    out = render_style_prompt(None, "Маша", "напомни про встречу")
    assert "Маша" in out
    assert "напомни про встречу" in out
    assert "ещё не построен" in out   # cold-start hint


def test_render_prompt_with_zero_message_profile_falls_back():
    p = StyleProfile(based_on_n_messages=0)
    out = render_style_prompt(p, "Маша", "хей")
    assert "ещё не построен" in out


def test_render_prompt_with_real_profile_includes_signals():
    p = StyleProfile(
        listener_label="Маша",
        based_on_n_messages=234,
        formality="ty",
        avg_length_chars=86,
        avg_sentences=1.4,
        emoji_per_msg=0.6,
        frequent_emoji=["🙏"],
        openings=["Слушай,"],
        closings=["Обнимаю"],
        vocabulary_signatures=["короче", "заебись"],
        code_switching={"ru": 0.78, "en": 0.18},
        sample_messages=[
            SampleMessage(event_id=1, text="ок, через час", occurred_at="x"),
        ],
    )
    out = render_style_prompt(p, "Маша", "перенесём встречу")
    assert "Маша" in out
    assert "ty" in out
    assert "86" in out
    assert "Слушай," in out
    assert "Обнимаю" in out
    assert "короче" in out
    assert "ru: 78%" in out
    assert "перенесём встречу" in out
    assert "ок, через час" in out


def test_render_prompt_respects_length_hint():
    p = StyleProfile(based_on_n_messages=10, listener_label="X",
                      avg_length_chars=50)
    out = render_style_prompt(p, "X", "x", length_hint="одно предложение")
    assert "одно предложение" in out


def test_render_prompt_includes_context_when_given():
    out = render_style_prompt(None, "X", "напиши", context="опаздываю на 10 минут")
    assert "опаздываю на 10 минут" in out


def test_render_prompt_skips_low_emoji_marker_when_zero():
    p = StyleProfile(based_on_n_messages=10, listener_label="X",
                      emoji_per_msg=0.0)
    out = render_style_prompt(p, "X", "intent")
    assert "не использует" in out  # explicit "no emoji" hint


def test_sample_message_dataclass():
    s = SampleMessage(event_id=1, text="hi", occurred_at="2026-06-01")
    assert s.event_id == 1
    assert s.text == "hi"
