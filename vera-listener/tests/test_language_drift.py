"""Русская речь не должна превращаться в английский перевод (24.09.2026).

Пайплайн подменён: он отвечает так, как отвечал живой whisper на NPU весь
вечер 24.09 — без токена языка говорит `en` и переводит, с токеном `ru` пишет
по-русски. Английский звук под `ru` остаётся английским (замер в language.py).
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from vera_listener.config import Config
from vera_listener.transcriber import MIN_AUDIO_S, Transcriber


class _Chunk:
    def __init__(self, start_ts, text):
        self.start_ts, self.end_ts, self.text = start_ts, 0.0, text


class _Result:
    def __init__(self, text, language):
        self._text, self.language = text, language
        self.chunks = [_Chunk(0.0, text)]

    def __str__(self):
        return self._text


class _Pipe:
    """`auto` отвечает `said`; с токеном — `forced[язык]`."""

    def __init__(self, said: tuple[str, str], forced: dict[str, str]):
        self.said, self.forced, self.calls = said, forced, []

    def generate(self, audio, **kw):
        lang = kw.get("language", "").strip("<|>")
        self.calls.append(lang or "auto")
        if not lang:
            return _Result(self.said[1], self.said[0])
        return _Result(self.forced[lang], lang)


def _run(tmp_path, monkeypatch, pipe, caplog=None):
    t = Transcriber(replace(Config(root=tmp_path, internal_secret="x"), language="auto"))
    monkeypatch.setattr(t, "_load", lambda: pipe)
    pcm = np.zeros(int((MIN_AUDIO_S + 1) * 16_000), dtype=np.int16).tobytes()
    return [s.text for s in t.transcribe(pcm, track="mic")]


def test_russian_heard_as_english_comes_back_russian(tmp_path, monkeypatch):
    pipe = _Pipe(("en", "Listen, I was on a meeting with a new manager"),
                 {"ru": "Слушай, я сегодня была на встрече с новым менеджером"})
    assert _run(tmp_path, monkeypatch, pipe) == [
        "Слушай, я сегодня была на встрече с новым менеджером"]
    assert pipe.calls == ["auto", "ru"]


def test_english_call_stays_english(tmp_path, monkeypatch):
    text = "Yesterday I had a meeting with the new manager"
    pipe = _Pipe(("en", text), {"ru": text})
    assert _run(tmp_path, monkeypatch, pipe) == [text]


def test_icelandic_hallucination_is_not_a_language(tmp_path, monkeypatch):
    pipe = _Pipe(("is", "það er það er það er"), {"ru": "Да, это оно"})
    assert _run(tmp_path, monkeypatch, pipe) == ["Да, это оно"]


def test_russian_and_ukrainian_cost_one_pass(tmp_path, monkeypatch):
    for lang, text in (("ru", "Давай сверим сроки"), ("uk", "Юра зробив концепт")):
        pipe = _Pipe((lang, text), {})
        assert _run(tmp_path, monkeypatch, pipe) == [text]
        assert pipe.calls == ["auto"], "здоровый кусок не платит вторым проходом"


def test_every_check_is_logged_with_track_and_reason(tmp_path, monkeypatch, caplog):
    pipe = _Pipe(("en", "Listen, I was on a meeting"), {"ru": "Слушай, я была на встрече"})
    with caplog.at_level("INFO", logger="listener.stt"):
        _run(tmp_path, monkeypatch, pipe)
    said = [r.getMessage() for r in caplog.records if "проверка языка" in r.getMessage()]
    assert len(said) == 1
    assert "mic" in said[0] and "en" in said[0] and "ru" in said[0] and "кириллица" in said[0]
