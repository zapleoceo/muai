"""Эхо из динамиков: реплика собеседника не должна попасть дважды."""
from __future__ import annotations

from vera_listener.dedup import drop_echo, similar


def _u(at: float, stream: str, text: str) -> dict:
    return {"at": at, "stream": stream, "text": text}


def test_mic_echo_of_system_line_is_removed():
    utterances = [
        _u(10.0, "system", "давай перенесём встречу на четверг"),
        _u(10.4, "mic", "Давай перенесём встречу на четверг!"),
        _u(12.0, "mic", "хорошо, четверг подходит"),
    ]
    kept = drop_echo(utterances)
    assert [u["stream"] for u in kept] == ["system", "mic"]
    assert kept[1]["text"] == "хорошо, четверг подходит"


def test_same_words_far_apart_are_not_echo():
    utterances = [
        _u(0.0, "system", "до понедельника"),
        _u(300.0, "mic", "до понедельника"),
    ]
    assert len(drop_echo(utterances)) == 2


def test_system_lines_are_never_dropped():
    utterances = [_u(1.0, "system", "алло"), _u(1.1, "system", "алло")]
    assert len(drop_echo(utterances)) == 2


def test_no_system_track_means_nothing_to_dedupe():
    utterances = [_u(1.0, "mic", "привет"), _u(1.2, "mic", "привет")]
    assert drop_echo(utterances) == utterances


def test_similarity_ignores_case_and_punctuation():
    assert similar("Привет, Коля!", "привет коля") > 0.95
    assert similar("привет", "") == 0.0
