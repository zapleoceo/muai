"""Раздача имён репликам: связка кластеризации, хранилища и заголовка окна.

Модель здесь подставная — проверяется логика именования, а не качество
опознания. Качество измерено отдельно и записано в `embedder.py`.
"""
from __future__ import annotations

import numpy as np

from vera_listener.counterpart import Counterpart
from vera_listener.speakers.embedder import EMBEDDING_DIM, normalize
from vera_listener.speakers.registry import VoiceprintRegistry
from vera_listener.speakers.session import SpeakerSession


def _vec(index: int) -> np.ndarray:
    base = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    base[index] = 1.0
    return normalize(base)


class _FakeEmbedder:
    """Отдаёт заранее заданный вектор на каждый вызов — по очереди.

    Ради этого `SpeakerEmbedder` и объявлен протоколом: тест не тянет модель
    на 25 МБ и не считает инференс.
    """

    def __init__(self, vectors):
        self._vectors = list(vectors)
        self.calls = 0

    def embed(self, audio):
        vector = self._vectors[self.calls % len(self._vectors)]
        self.calls += 1
        return vector


class _BrokenEmbedder:
    def embed(self, audio):
        raise RuntimeError("модель отвалилась")


def _session(tmp_path, vectors, **kw) -> tuple[SpeakerSession, VoiceprintRegistry]:
    registry = VoiceprintRegistry(tmp_path / "voiceprints.json")
    return SpeakerSession(_FakeEmbedder(vectors), registry, **kw), registry


_AUDIO = np.zeros(16_000, dtype=np.float32)


def _direct(name: str) -> Counterpart:
    """Подтверждённая личка — как её отдаёт Slack."""
    return Counterpart(name=name, is_direct=True)


def _chat(name: str) -> Counterpart:
    """Имя чата без подтверждения — как его отдаёт Telegram."""
    return Counterpart(name=name, is_direct=False)


class TestNaming:
    def test_single_voice_takes_the_name_from_the_window(self, tmp_path):
        session, _ = _session(tmp_path, [_vec(0)])
        session.observe(1.0, _AUDIO)
        session.observe(5.0, _AUDIO)
        assert session.resolve(_direct("Виктор")) == {1.0: "Виктор", 5.0: "Виктор"}

    def test_several_voices_get_numbers_when_no_names_known(self, tmp_path):
        session, _ = _session(tmp_path, [_vec(0), _vec(1)])
        for at in (1.0, 2.0, 3.0, 4.0):
            session.observe(at, _AUDIO)
        assert set(session.resolve(None).values()) == {"Собеседник 1", "Собеседник 2"}

    def test_numbers_of_the_unnamed_run_in_a_row(self, tmp_path):
        """Один голос узнан, два нет. Безымянные — «1» и «2», а не «2» и «3»:
        пропуск в нумерации читается как потерянная реплика."""
        session, registry = _session(tmp_path, [_vec(0), _vec(1), _vec(2)])
        registry.remember("Виктор", _vec(0))
        for at in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
            session.observe(at, _AUDIO)
        assert set(session.resolve(None).values()) == {
            "Виктор", "Собеседник 1", "Собеседник 2"}

    def test_window_name_is_refused_when_voices_are_many(self, tmp_path):
        """Заголовок обещает один-на-один. Голосов несколько — обещание
        нарушено, и приписать имя одному из них наугад нельзя."""
        session, registry = _session(tmp_path, [_vec(0), _vec(1)])
        for at in (1.0, 2.0, 3.0, 4.0):
            session.observe(at, _AUDIO)
        assert "Виктор" not in set(session.resolve(_direct("Виктор")).values())
        assert registry.names == []

    def test_known_voice_wins_over_the_window_title(self, tmp_path):
        """Отпечаток надёжнее заголовка: заголовок мог остаться от прошлого
        окна, голос — нет."""
        session, registry = _session(tmp_path, [_vec(0)])
        registry.remember("Вадим", _vec(0))
        session.observe(1.0, _AUDIO)
        assert session.resolve(_direct("Виктор")) == {1.0: "Вадим"}

    def test_known_voice_is_recognised_in_a_group(self, tmp_path):
        """Ради этого всё и затевалось: имя, узнанное в разговоре один на
        один, находит того же человека в общем созвоне."""
        session, registry = _session(tmp_path, [_vec(0), _vec(1)])
        registry.remember("Вадим", _vec(1))
        for at in (1.0, 2.0, 3.0, 4.0):
            session.observe(at, _AUDIO)
        assert "Вадим" in set(session.resolve(None).values())

    def test_one_name_is_not_given_to_two_clusters(self, tmp_path):
        session, registry = _session(tmp_path, [_vec(0), _vec(1)])
        registry.remember("Вадим", _vec(0))
        registry.remember("Вадим", _vec(1))
        for at in (1.0, 2.0, 3.0, 4.0):
            session.observe(at, _AUDIO)
        names = list(session.resolve(None).values())
        assert len(set(names)) == 2


class TestEnrollment:
    def test_one_on_one_call_remembers_the_voice(self, tmp_path):
        session, registry = _session(tmp_path, [_vec(0)])
        session.observe(1.0, _AUDIO)
        session.resolve(_direct("Виктор"))
        assert registry.names == ["Виктор"]

    def test_enrollment_survives_reload(self, tmp_path):
        session, _ = _session(tmp_path, [_vec(0)])
        session.observe(1.0, _AUDIO)
        session.resolve(_direct("Виктор"))
        fresh = VoiceprintRegistry(tmp_path / "voiceprints.json")
        assert fresh.match(_vec(0)) == "Виктор"

    def test_nothing_is_remembered_without_a_name(self, tmp_path):
        session, registry = _session(tmp_path, [_vec(0)])
        session.observe(1.0, _AUDIO)
        session.resolve(None)
        assert registry.names == []


class TestRobustness:
    def test_no_observations_gives_empty_mapping(self, tmp_path):
        session, _ = _session(tmp_path, [_vec(0)])
        assert session.resolve(_direct("Виктор")) == {}

    def test_broken_model_does_not_break_the_conversation(self, tmp_path):
        """Текст уже распознан и ценнее разметки говорящих: сбой опознания
        обязан остаться внутри."""
        registry = VoiceprintRegistry(tmp_path / "voiceprints.json")
        session = SpeakerSession(_BrokenEmbedder(), registry)
        session.observe(1.0, _AUDIO)
        assert len(session) == 0
        assert session.resolve(_direct("Виктор")) == {}

    def test_embedder_returning_none_is_skipped(self, tmp_path):
        """Слишком короткий кусок — обычное дело, не ошибка."""
        registry = VoiceprintRegistry(tmp_path / "voiceprints.json")
        session = SpeakerSession(_FakeEmbedder([None]), registry)
        session.observe(1.0, _AUDIO)
        assert len(session) == 0

    def test_keys_are_rounded_like_the_outbox_stores_them(self, tmp_path):
        """Ключ — смещение реплики; очередь округляет до сотых, и разъезд
        здесь оставил бы реплики без имён."""
        session, _ = _session(tmp_path, [_vec(0)])
        session.observe(1.23456, _AUDIO)
        assert list(session.resolve(_direct("Виктор"))) == [1.23]


class TestEnrollmentConfidence:
    """Запоминать голос навсегда можно только с подтверждением приложения."""

    def test_unconfirmed_chat_labels_but_does_not_remember(self, tmp_path):
        """Telegram: имя чата может оказаться группой. Разметить разговор им
        можно — это видно и обратимо; запомнить голос нельзя."""
        session, registry = _session(tmp_path, [_vec(0)])
        session.observe(1.0, _AUDIO)
        assert session.resolve(_chat("Кайфушники Нячанга")) == {
            1.0: "Кайфушники Нячанга"}
        assert registry.names == []

    def test_confirmed_direct_message_does_remember(self, tmp_path):
        session, registry = _session(tmp_path, [_vec(0)])
        session.observe(1.0, _AUDIO)
        session.resolve(_direct("Виктор"))
        assert registry.names == ["Виктор"]
