"""Распознавание на нейропроцессоре: выбор устройства и разбор таймкодов.

Сам вывод модели тестами не проверить — она весит 1.6 ГБ и считает на железе.
Поэтому здесь то, что ломается тихо: порядок попыток по устройствам (на чужом
ноутбуке NPU может не быть вовсе) и превращение ответа пайплайна в реплики со
смещениями. Смещение критично: по нему реплики выстраиваются в хронологию,
и потеря таймкода свалила бы весь разговор в начало.
"""
from __future__ import annotations

import sys

import numpy as np
import pytest

from vera_listener.config import Config
from vera_listener.transcriber import (
    MIN_AUDIO_S,
    Transcriber,
    device_chain,
    segments_of,
)


class _Chunk:
    def __init__(self, start_ts, text, end_ts=0.0):
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.text = text


class _Result:
    """Похоже на WhisperDecodedResults: str() даёт текст, chunks — сегменты."""

    def __init__(self, text="", chunks=None):
        self._text = text
        if chunks is not None:
            self.chunks = chunks

    def __str__(self):
        return self._text


class TestDeviceChain:
    def test_npu_first_cpu_last(self):
        assert device_chain("NPU") == ["NPU", "CPU"]

    def test_gpu_falls_back_to_cpu(self):
        assert device_chain("GPU") == ["GPU", "CPU"]

    def test_cpu_is_not_duplicated(self):
        assert device_chain("CPU") == ["CPU"]

    def test_case_does_not_matter(self):
        assert device_chain("npu") == ["NPU", "CPU"]


class TestSegments:
    def test_timestamps_become_offsets(self):
        got = segments_of(_Result(chunks=[
            _Chunk(0.0, " Давай сверим сроки"),
            _Chunk(3.72, " Даша обещала отчёт"),
        ]))
        assert got == [(0.0, "Давай сверим сроки"), (3.72, "Даша обещала отчёт")]

    def test_empty_chunks_are_dropped(self):
        got = segments_of(_Result(chunks=[
            _Chunk(1.0, "   "), _Chunk(2.0, "есть"), _Chunk(3.0, ""),
        ]))
        assert got == [(2.0, "есть")]

    def test_no_chunks_but_text_keeps_the_words(self):
        """Текст без таймкодов лучше, чем ничего: он уйдёт с нулевым смещением,
        и об этом пишется предупреждение — молча путать порядок нельзя."""
        assert segments_of(_Result(text="  привет  ")) == [(0.0, "привет")]

    def test_nothing_at_all(self):
        assert segments_of(_Result(text="")) == []
        assert segments_of(_Result(text="", chunks=[])) == []

    def test_offsets_are_floats(self):
        got = segments_of(_Result(chunks=[_Chunk(5, "пять")]))
        assert isinstance(got[0][0], float)


class TestTooShort:
    @staticmethod
    def _transcriber(tmp_path) -> Transcriber:
        return Transcriber(Config(root=tmp_path, internal_secret="x"))

    def test_short_audio_never_touches_the_model(self, tmp_path):
        """На обрывке в 64 мс whisper выдаёт выдумку «Спасибо за просмотр».

        `_load` намеренно не подменён: позови его код — тест упал бы на
        отсутствии модели, а значит порог проверяется по-настоящему."""
        t = self._transcriber(tmp_path)
        pcm = np.zeros(int(0.2 * 16_000), dtype=np.int16).tobytes()
        assert t.transcribe(pcm) == []

    def test_empty_pcm(self, tmp_path):
        assert self._transcriber(tmp_path).transcribe(b"") == []

    def test_long_enough_audio_goes_to_the_model(self, tmp_path, monkeypatch):
        t = self._transcriber(tmp_path)
        seen = {}

        class _Pipe:
            def generate(self, audio, **kw):
                seen["samples"] = len(audio)
                seen["kw"] = kw
                return _Result(chunks=[_Chunk(0.5, "слышно")])

        monkeypatch.setattr(t, "_load", lambda: _Pipe())
        pcm = np.zeros(int((MIN_AUDIO_S + 0.5) * 16_000), dtype=np.int16).tobytes()
        assert t.transcribe(pcm) == [(0.5, "слышно")]
        assert seen["samples"] == int((MIN_AUDIO_S + 0.5) * 16_000)
        # Язык уходит whisper-токеном, иначе модель его не поймёт.
        assert seen["kw"]["language"] == "<|ru|>"
        assert seen["kw"]["return_timestamps"] is True


class TestConfig:
    def test_model_and_cache_live_under_the_root(self, tmp_path):
        assert Config(root=tmp_path, internal_secret="x").model_dir == tmp_path / "models"

    def test_npu_is_the_default(self):
        assert Config().stt_device == "NPU"

    def test_turbo_is_the_default_model(self):
        assert "turbo" in Config().model_id


class TestLoadFallback:
    @staticmethod
    def _fake_genai(monkeypatch, tried, working="CPU"):
        class _FakeGenai:
            @staticmethod
            def WhisperPipeline(path, device, **kw):  # noqa: N802
                tried.append(device)
                if device != working:
                    raise RuntimeError(f"устройство {device} недоступно")
                return f"пайплайн на {device}"

        monkeypatch.setitem(sys.modules, "openvino_genai", _FakeGenai)

    def test_falls_back_to_cpu_when_npu_fails(self, tmp_path, monkeypatch):
        """Нет нейропроцессора — работаем дороже, но работаем."""
        t = Transcriber(Config(root=tmp_path, internal_secret="x"))
        monkeypatch.setattr(t, "_model_dir", lambda: tmp_path)
        tried: list[str] = []
        self._fake_genai(monkeypatch, tried)

        assert t._load() == "пайплайн на CPU"
        assert tried == ["NPU", "CPU"]
        assert t.device == "CPU"

    def test_npu_is_used_when_it_works(self, tmp_path, monkeypatch):
        t = Transcriber(Config(root=tmp_path, internal_secret="x"))
        monkeypatch.setattr(t, "_model_dir", lambda: tmp_path)
        tried: list[str] = []
        self._fake_genai(monkeypatch, tried, working="NPU")

        assert t._load() == "пайплайн на NPU"
        assert tried == ["NPU"]
        assert t.device == "NPU"

    def test_pipeline_is_built_once(self, tmp_path, monkeypatch):
        """Модель живёт до конца процесса: компиляция стоит секунды, а под NPU
        первая — до двух минут."""
        t = Transcriber(Config(root=tmp_path, internal_secret="x"))
        monkeypatch.setattr(t, "_model_dir", lambda: tmp_path)
        tried: list[str] = []
        self._fake_genai(monkeypatch, tried, working="NPU")

        t._load()
        t._load()
        assert tried == ["NPU"]

    def test_raises_with_all_reasons_when_nothing_works(self, tmp_path, monkeypatch):
        t = Transcriber(Config(root=tmp_path, internal_secret="x"))
        monkeypatch.setattr(t, "_model_dir", lambda: tmp_path)
        tried: list[str] = []
        self._fake_genai(monkeypatch, tried, working="нет такого")

        with pytest.raises(RuntimeError, match="ни одно устройство"):
            t._load()
        assert tried == ["NPU", "CPU"]

    def test_device_is_unknown_before_loading(self, tmp_path):
        assert Transcriber(Config(root=tmp_path, internal_secret="x")).device is None

    def test_warm_up_returns_the_device(self, tmp_path, monkeypatch):
        """Публичный вход вместо приватного _load(), который переименуют."""
        t = Transcriber(Config(root=tmp_path, internal_secret="x"))
        monkeypatch.setattr(t, "_model_dir", lambda: tmp_path)
        self._fake_genai(monkeypatch, [], working="NPU")
        assert t.warm_up() == "NPU"


class TestRuntimeFailure:
    """Устройство отвалилось УЖЕ В РАБОТЕ — молчать до перезапуска нельзя."""

    @staticmethod
    def _transcriber(tmp_path) -> Transcriber:
        return Transcriber(Config(root=tmp_path, internal_secret="x"))

    def _pcm(self):
        return np.zeros(int(2 * 16_000), dtype=np.int16).tobytes()

    def test_failed_device_is_dropped_and_banned(self, tmp_path, monkeypatch):
        t = self._transcriber(tmp_path)
        t._pipe, t._device = object(), "NPU"

        class _Dead:
            def generate(self, *a, **kw):
                raise RuntimeError("устройство отвалилось")

        monkeypatch.setattr(t, "_load", lambda: _Dead())
        with pytest.raises(RuntimeError, match="отвалилось"):
            t.transcribe(self._pcm())
        # Пайплайн сброшен, устройство в чёрном списке — следующий кусок
        # поедет на CPU, а не потеряется молча до перезапуска процесса.
        assert t._pipe is None and t.device is None
        assert "NPU" in t._banned

    def test_banned_device_is_skipped_on_reload(self, tmp_path, monkeypatch):
        t = self._transcriber(tmp_path)
        t._banned.add("NPU")
        monkeypatch.setattr(t, "_model_dir", lambda: tmp_path)
        tried: list[str] = []
        TestLoadFallback._fake_genai(monkeypatch, tried, working="CPU")

        t._load()
        assert tried == ["CPU"]

    def test_all_devices_banned_says_so(self, tmp_path, monkeypatch):
        t = self._transcriber(tmp_path)
        t._banned.update({"NPU", "CPU"})
        monkeypatch.setattr(t, "_model_dir", lambda: tmp_path)
        TestLoadFallback._fake_genai(monkeypatch, [], working="NPU")

        with pytest.raises(RuntimeError, match="ни одно устройство"):
            t._load()


class TestModelCompleteness:
    """Проверять по одному файлу нельзя: обрыв загрузки ломал бы каждый старт."""

    @staticmethod
    def _full(target):
        from vera_listener.transcriber import REQUIRED_FILES
        target.mkdir(parents=True, exist_ok=True)
        for name in REQUIRED_FILES:
            (target / name).write_text("x", encoding="utf-8")

    def test_complete_model(self, tmp_path):
        from vera_listener.transcriber import model_is_complete
        self._full(tmp_path / "m")
        assert model_is_complete(tmp_path / "m") is True

    def test_missing_directory(self, tmp_path):
        from vera_listener.transcriber import model_is_complete
        assert model_is_complete(tmp_path / "нет") is False

    def test_encoder_alone_is_not_enough(self, tmp_path):
        """Ровно тот случай: энкодер докачался, декодер нет."""
        from vera_listener.transcriber import model_is_complete
        target = tmp_path / "m"
        target.mkdir()
        (target / "openvino_encoder_model.xml").write_text("x", encoding="utf-8")
        assert model_is_complete(target) is False

    def test_leftover_incomplete_file_means_not_complete(self, tmp_path):
        """huggingface_hub оставляет .incomplete — прямой признак обрыва."""
        from vera_listener.transcriber import model_is_complete
        target = tmp_path / "m"
        self._full(target)
        (target / "openvino_decoder_model.bin.incomplete").write_text("x", encoding="utf-8")
        assert model_is_complete(target) is False
