"""Хранилище отпечатков голосов между разговорами.

Главное свойство, которое держат эти тесты: ошибиться именем нельзя. Порог
узнавания строгий, испорченный файл не роняет слушателя, а знакомый голос
уточняет отпечаток, а не перезаписывает его свежей плохой записью.
"""
from __future__ import annotations

import json

import numpy as np

from vera_listener.speakers.embedder import EMBEDDING_DIM, normalize
from vera_listener.speakers.registry import MAX_WEIGHT, VoiceprintRegistry


def _vec(index: int, tilt: float = 0.0) -> np.ndarray:
    base = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    base[index] = 1.0
    if tilt:
        base[(index + 1) % EMBEDDING_DIM] = tilt
    return normalize(base)


def _registry(tmp_path, **kw) -> VoiceprintRegistry:
    return VoiceprintRegistry(tmp_path / "voiceprints.json", **kw)


class TestMatching:
    def test_unknown_voice_returns_none(self, tmp_path):
        assert _registry(tmp_path).match(_vec(0)) is None

    def test_remembered_voice_is_recognised(self, tmp_path):
        reg = _registry(tmp_path)
        reg.remember("Вадим", _vec(0))
        assert reg.match(_vec(0)) == "Вадим"

    def test_similar_enough_voice_is_recognised(self, tmp_path):
        reg = _registry(tmp_path)
        reg.remember("Вадим", _vec(0))
        assert reg.match(_vec(0, tilt=0.3)) == "Вадим"

    def test_different_voice_is_not_recognised(self, tmp_path):
        """Приписать реплику живому человеку по имени хуже, чем оставить
        безымянной: ошибка видна как ложь, а не как неполнота."""
        reg = _registry(tmp_path)
        reg.remember("Вадим", _vec(0))
        assert reg.match(_vec(5)) is None

    def test_closest_of_several_wins(self, tmp_path):
        reg = _registry(tmp_path)
        reg.remember("Вадим", _vec(0))
        reg.remember("Виктор", _vec(1))
        assert reg.match(_vec(1, tilt=0.1)) == "Виктор"

    def test_threshold_is_configurable(self, tmp_path):
        strict = _registry(tmp_path, threshold=0.999)
        strict.remember("Вадим", _vec(0))
        assert strict.match(_vec(0, tilt=0.3)) is None


class TestRemembering:
    def test_repeat_updates_instead_of_duplicating(self, tmp_path):
        reg = _registry(tmp_path)
        reg.remember("Вадим", _vec(0))
        reg.remember("Вадим", _vec(0, tilt=0.2))
        assert reg.names == ["Вадим"]

    def test_weight_grows_with_each_encounter(self, tmp_path):
        reg = _registry(tmp_path)
        for _ in range(3):
            reg.remember("Вадим", _vec(0))
        reg.save()
        stored = json.loads((tmp_path / "voiceprints.json").read_text(encoding="utf-8"))
        assert stored[0]["weight"] == 3

    def test_weight_stops_at_the_cap(self, tmp_path):
        """Дальше вклад новой записи затухает: голос устойчив, запись — нет."""
        reg = _registry(tmp_path)
        for _ in range(MAX_WEIGHT + 10):
            reg.remember("Вадим", _vec(0))
        reg.save()
        stored = json.loads((tmp_path / "voiceprints.json").read_text(encoding="utf-8"))
        assert stored[0]["weight"] == MAX_WEIGHT

    def test_accumulated_print_resists_one_bad_recording(self, tmp_path):
        """Один плохой заход не должен утащить накопленный отпечаток."""
        reg = _registry(tmp_path)
        for _ in range(MAX_WEIGHT):
            reg.remember("Вадим", _vec(0))
        reg.remember("Вадим", _vec(7))
        assert reg.match(_vec(0)) == "Вадим"

    def test_empty_name_is_ignored(self, tmp_path):
        reg = _registry(tmp_path)
        reg.remember("", _vec(0))
        assert reg.names == []

    def test_zero_vector_is_ignored(self, tmp_path):
        """Нулевой вектор приходит от неудачного отпечатка — запомнить его
        значило бы отравить хранилище именем без голоса."""
        reg = _registry(tmp_path)
        reg.remember("Вадим", np.zeros(EMBEDDING_DIM, dtype=np.float32))
        assert reg.names == []


class TestPersistence:
    def test_survives_reload(self, tmp_path):
        reg = _registry(tmp_path)
        reg.remember("Вадим", _vec(0))
        reg.save()
        assert _registry(tmp_path).match(_vec(0)) == "Вадим"

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert _registry(tmp_path).names == []

    def test_corrupt_file_does_not_crash_the_listener(self, tmp_path):
        """Отпечатки — удобство, а не данные разговора: испорченный файл не
        повод перестать писать разговоры."""
        (tmp_path / "voiceprints.json").write_text("{не json", encoding="utf-8")
        assert _registry(tmp_path).names == []

    def test_entries_of_wrong_dimension_are_dropped(self, tmp_path):
        """Смена модели меняет размерность; сравнивать старое с новым нельзя."""
        (tmp_path / "voiceprints.json").write_text(
            json.dumps([{"name": "Старый", "centroid": [0.1, 0.2], "weight": 1}]),
            encoding="utf-8")
        assert _registry(tmp_path).names == []

    def test_entries_without_name_are_dropped(self, tmp_path):
        (tmp_path / "voiceprints.json").write_text(
            json.dumps([{"centroid": [0.0] * EMBEDDING_DIM, "weight": 1}]),
            encoding="utf-8")
        assert _registry(tmp_path).names == []

    def test_save_is_atomic_no_partial_file_left(self, tmp_path):
        reg = _registry(tmp_path)
        reg.remember("Вадим", _vec(0))
        reg.save()
        assert not (tmp_path / "voiceprints.tmp").exists()
