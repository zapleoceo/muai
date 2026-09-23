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
    # Обрезку тишины выключаем: здесь проверяется раздача имён, а не
    # распознавание речи. Иначе модульный тест тянул бы за собой silero
    # и проходил бы только по совпадению — нашло ревью.
    session = SpeakerSession(_FakeEmbedder(vectors), registry,
                             trim=lambda audio: audio, **kw)
    return session, registry


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

    def test_confirmed_direct_chat_beats_the_clustering(self, tmp_path):
        """Раньше здесь было обратное правило: «голосов несколько — значит
        обещание один-на-один нарушено, имя не ставим».

        Оно снято 16.09 по данным. Живой разговор ОДИН НА ОДИН дал восемь
        «собеседников»: кластеризация на настоящей речи дробит одного
        человека, поэтому «кластеров много» больше не считается признаком
        того, что людей много. Подтверждение приложения сильнее.

        Запоминание при этом не размягчилось — оно проверяется ниже.
        """
        session, registry = _session(tmp_path, [_vec(0), _vec(1)])
        for at in (1.0, 2.0, 3.0, 4.0):
            session.observe(at, _AUDIO)
        assert set(session.resolve(_direct("Виктор")).values()) == {"Виктор"}
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
        session = SpeakerSession(_BrokenEmbedder(), registry,
                                 trim=lambda audio: audio)
        session.observe(1.0, _AUDIO)
        assert len(session) == 0
        assert session.resolve(_direct("Виктор")) == {}

    def test_embedder_returning_none_is_skipped(self, tmp_path):
        """Слишком короткий кусок — обычное дело, не ошибка."""
        registry = VoiceprintRegistry(tmp_path / "voiceprints.json")
        session = SpeakerSession(_FakeEmbedder([None]), registry,
                                 trim=lambda audio: audio)
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


class TestConfirmedDirectChatWins:
    """Приложение подтвердило личку — значит собеседник один, и точка.

    Ради этого случая всё и затевалось. Вживую 16.09 разговор ОДИН НА ОДИН
    дал восемь «собеседников»: кластеризация на живой речи дробит одного
    человека, и спорить с подтверждением приложения ей нечем.
    """

    def test_several_clusters_collapse_to_the_named_person(self, tmp_path):
        session, _ = _session(tmp_path, [_vec(0), _vec(1), _vec(2)])
        for at in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
            session.observe(at, _AUDIO)

        names = session.resolve(_direct("Виктор"))

        assert set(names.values()) == {"Виктор"}
        assert len(names) == 6

    def test_split_voice_is_not_remembered(self, tmp_path):
        """Назвать — дёшево и обратимо, запомнить — навсегда.

        Если кластеризация насчитала несколько голосов, в центроид мог попасть
        второй человек. Имя ставим, отпечаток не трогаем.
        """
        session, registry = _session(tmp_path, [_vec(0), _vec(1), _vec(2)])
        for at in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
            session.observe(at, _AUDIO)

        session.resolve(_direct("Виктор"))

        assert registry.names == []

    def test_single_voice_is_still_remembered(self, tmp_path):
        """Кластеризация согласна, что голос один — запоминаем, как раньше."""
        session, registry = _session(tmp_path, [_vec(0)])
        session.observe(1.0, _AUDIO)
        session.observe(2.0, _AUDIO)

        session.resolve(_direct("Виктор"))

        assert registry.names == ["Виктор"]

    def test_two_known_voices_break_the_confirmation(self, tmp_path):
        """Личка подтверждена, но узнаны ДВА разных знакомых голоса.

        Это противоречие, а не шум: либо в хадл позвали третьего, либо одно
        узнавание ложное. Свести всех к одному имени нельзя, и выбрать «по
        большинству» тоже — свидетельства спорят. Отступаем к нумерации.
        Нашло ревью.
        """
        session, registry = _session(tmp_path, [_vec(0), _vec(1)])
        registry.remember("Вадим", _vec(0))
        registry.remember("Олег", _vec(1))
        for at in (1.0, 2.0, 3.0, 4.0):
            session.observe(at, _AUDIO)

        names = set(session.resolve(_direct("Виктор")).values())

        assert names == {"Вадим", "Олег"}
        assert "Виктор" not in names

    def test_known_voice_of_the_title_person_collapses(self, tmp_path):
        """Узнан тот же, кто в заголовке, — подтверждение, все реплики ему."""
        session, registry = _session(tmp_path, [_vec(0), _vec(1)])
        registry.remember("Виктор", _vec(0))
        for at in (1.0, 2.0, 3.0, 4.0):
            session.observe(at, _AUDIO)

        assert set(session.resolve(_direct("Виктор")).values()) == {"Виктор"}

    def test_other_known_voice_in_a_dm_is_a_huddle(self, tmp_path):
        """21.09: личка Виктора, но в хадле был ещё Вадим — узнан отпечатком.

        Узнанный голос с именем ≠ заголовку — доказательство второго человека
        на линии. Раньше имя узнанного перетирало всех: 26 реплик «Вадим».
        """
        session, registry = _session(tmp_path, [_vec(0), _vec(1)])
        registry.remember("Вадим", _vec(0))
        for at in (1.0, 2.0, 3.0, 4.0):
            session.observe(at, _AUDIO)

        names = session.resolve(_direct("Виктор"))

        assert names == {1.0: "Вадим", 2.0: "Виктор", 3.0: "Вадим", 4.0: "Виктор"}
        assert registry.names == ["Вадим"]

    def test_huddle_numbers_the_rest_when_the_title_voice_is_known(self, tmp_path):
        """Узнаны и Виктор, и Вадим — чей неузнанный осколок, неизвестно."""
        session, registry = _session(tmp_path, [_vec(0), _vec(1), _vec(2)])
        registry.remember("Вадим", _vec(0))
        registry.remember("Виктор", _vec(1))
        for at in (1.0, 2.0, 3.0):
            session.observe(at, _AUDIO)

        names = session.resolve(_direct("Виктор"))

        assert names == {1.0: "Вадим", 2.0: "Виктор", 3.0: "Собеседник 1"}

    def test_two_strangers_known_leave_the_rest_to_the_title(self, tmp_path):
        """Узнаны двое, и оба не хозяин лички, — неузнанное остаётся ему."""
        session, registry = _session(tmp_path, [_vec(0), _vec(1), _vec(2)])
        registry.remember("Вадим", _vec(0))
        registry.remember("Олег", _vec(1))
        for at in (1.0, 2.0, 3.0):
            session.observe(at, _AUDIO)

        names = session.resolve(_direct("Виктор"))

        assert names == {1.0: "Вадим", 2.0: "Олег", 3.0: "Виктор"}
        assert sorted(registry.names) == ["Вадим", "Олег"]

    def test_chat_name_without_confirmation_still_needs_one_voice(self, tmp_path):
        """Telegram личку не подтверждает: «General @ …» — групповой чат.

        Проверено по накопленным заголовкам звонков 16.09: форма с «@» бывает
        и у групп, поэтому для неподтверждённого имени старое правило в силе —
        имя только когда голос действительно один.
        """
        session, _ = _session(tmp_path, [_vec(0), _vec(1)])
        for at in (1.0, 2.0, 3.0, 4.0):
            session.observe(at, _AUDIO)

        names = session.resolve(_chat("Кайфушники Нячанга"))

        assert "Кайфушники Нячанга" not in set(names.values())
