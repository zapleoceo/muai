"""Сквозная проверка: от куска звука до имени в реплике.

Отдельные части проверены своими тестами. Здесь — их стык, где ломается
тихо: ключом связи служит смещение реплики, и оно проходит через ТРИ
округления (снятие отпечатка, запись в очередь, раздача имён). Разъедься они
хоть на сотую — реплики останутся без имён, и ни одна строка в логе об этом
не скажет.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from vera_listener.app import Listener
from vera_listener.capture import MIC, SYSTEM
from vera_listener.config import Config
from vera_listener.outbox import read_payload
from vera_listener.segmenter import Closed, Session
from vera_listener.speakers.embedder import EMBEDDING_DIM, normalize
from vera_listener.speakers.session import SpeakerSession
from vera_listener.transcriber import Segment


def _vec(index: int) -> np.ndarray:
    base = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    base[index] = 1.0
    return normalize(base)


class _Transcriber:
    """Отдаёт заранее заданные реплики вместо whisper."""

    def __init__(self, segments: list[Segment]):
        self._segments = segments

    def transcribe(self, pcm: bytes) -> list[Segment]:
        return list(self._segments)


class _Embedder:
    """Голос выбирается по ТЕКСТУ реплики — так тест управляет, кто говорит."""

    def __init__(self, by_marker: dict[str, np.ndarray]):
        self._by_marker = by_marker
        self.seen: list[int] = []

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        self.seen.append(len(audio))
        # Маркер прячем в первый отсчёт вырезки: настоящая модель смотрит на
        # звук, а тесту нужен предсказуемый ответ. Конвейер переводит int16 в
        # float делением на 32768 — возвращаем обратно, иначе маркер обнулится
        # (на этом тест и поймал сам себя).
        marker = str(round(float(audio[0]) * 32768)) if len(audio) else ""
        return self._by_marker.get(marker)


def _listener(tmp_path, segments, embedder, **over) -> Listener:
    config = replace(Config(root=tmp_path, internal_secret="x"), **over)
    listener = Listener(config)
    listener.transcriber = _Transcriber(segments)
    listener._embedder = embedder
    return listener


def _open(listener: Listener, *, app: str, title: str) -> None:
    listener.segmenter.feed(0.0, MIC, True, app=app, window_title=title)
    listener._ensure_open()


def _pcm(marker: int, seconds: float = 6.0) -> bytes:
    """Кусок, у которого первый отсчёт несёт номер голоса."""
    audio = np.zeros(int(seconds * 16_000), dtype=np.int16)
    audio[:] = marker
    return audio.tobytes()


def _closed(listener: Listener, app: str, title: str) -> Closed:
    return Closed(
        session=Session(started_at=0.0, app=app, window_title=title,
                        last_speech_at=60.0, speech_s={MIC: 30.0, SYSTEM: 30.0}),
        ended_at=60.0, reason="silence")


class TestNamesReachTheUtterances:
    def test_one_on_one_call_names_every_remote_line(self, tmp_path):
        """Slack называет собеседника в заголовке; голос один — значит имя
        принадлежит именно ему."""
        segments = [Segment(at=0.0, end=3.0, text="привет"),
                    Segment(at=3.5, end=6.0, text="давай начнём")]
        embedder = _Embedder({"1": _vec(0)})
        listener = _listener(tmp_path, segments, embedder)
        title = "Viktor Gavrylenko - Sintegrum Team - Slack"
        _open(listener, app="slack.exe", title=title)

        listener._transcribe_into(listener.session, SYSTEM, 10.0, _pcm(1),
                                  listener._speakers)
        payload_before = read_payload(listener.session)
        utterances = list(payload_before["utterances"])
        named = listener._name_speakers(
            utterances, _closed(listener, "slack.exe", title), listener._speakers)

        assert named == 2
        assert {u["speaker"] for u in utterances} == {"Viktor Gavrylenko"}

    def test_offsets_survive_all_three_roundings(self, tmp_path):
        """Ключ проходит округление трижды. Числа взяты неудобные нарочно."""
        segments = [Segment(at=0.005, end=3.0, text="раз"),
                    Segment(at=2.675, end=5.0, text="два"),
                    Segment(at=4.3349, end=6.0, text="три")]
        embedder = _Embedder({"1": _vec(0)})
        listener = _listener(tmp_path, segments, embedder)
        title = "Viktor Gavrylenko - Sintegrum Team - Slack"
        _open(listener, app="slack.exe", title=title)

        listener._transcribe_into(listener.session, SYSTEM, 7.125, _pcm(1),
                                  listener._speakers)
        utterances = list(read_payload(listener.session)["utterances"])
        named = listener._name_speakers(
            utterances, _closed(listener, "slack.exe", title), listener._speakers)
        assert named == 3, "смещения разъехались между записью и раздачей имён"

    def test_group_call_keeps_voices_apart(self, tmp_path):
        """Meet имён не даёт, но голоса обязаны разделиться по номерам."""
        segments = [Segment(at=0.0, end=6.0, text="реплика")]
        embedder = _Embedder({"1": _vec(0), "2": _vec(1)})
        listener = _listener(tmp_path, segments, embedder)
        title = "Meet - Google Chrome"
        _open(listener, app="chrome.exe", title=title)

        for marker, offset in ((1, 0.0), (2, 10.0), (1, 20.0), (2, 30.0)):
            listener._transcribe_into(listener.session, SYSTEM, offset,
                                      _pcm(marker), listener._speakers)
        utterances = list(read_payload(listener.session)["utterances"])
        listener._name_speakers(
            utterances, _closed(listener, "chrome.exe", title), listener._speakers)

        names = [u.get("speaker") for u in utterances]
        assert set(names) == {"Собеседник 1", "Собеседник 2"}
        assert names[0] == names[2] and names[1] == names[3], "голоса перепутаны"


class TestWhatIsNotNamed:
    def test_microphone_lines_get_no_speaker(self, tmp_path):
        """Владельца опознавать незачем — дорожка сама его называет."""
        segments = [Segment(at=0.0, end=6.0, text="я говорю")]
        embedder = _Embedder({"1": _vec(0)})
        listener = _listener(tmp_path, segments, embedder)
        title = "Viktor Gavrylenko - Sintegrum Team - Slack"
        _open(listener, app="slack.exe", title=title)

        listener._transcribe_into(listener.session, MIC, 0.0, _pcm(1),
                                  listener._speakers)
        utterances = list(read_payload(listener.session)["utterances"])
        listener._name_speakers(
            utterances, _closed(listener, "slack.exe", title), listener._speakers)
        assert all("speaker" not in u for u in utterances)

    def test_microphone_audio_is_never_embedded(self, tmp_path):
        """Отпечатки снимаются только с дорожки приложения: чужой голос на
        микрофоне — это эхо, оно помечено отдельно."""
        segments = [Segment(at=0.0, end=6.0, text="я говорю")]
        embedder = _Embedder({"1": _vec(0)})
        listener = _listener(tmp_path, segments, embedder)
        _open(listener, app="slack.exe", title="Viktor Gavrylenko - Sintegrum Team - Slack")

        listener._transcribe_into(listener.session, MIC, 0.0, _pcm(1),
                                  listener._speakers)
        assert embedder.seen == [], "микрофон не должен доходить до модели голоса"

    def test_missing_speaker_session_does_not_break_anything(self, tmp_path):
        """Сессия опознания может быть None — например, разговор восстановлен
        из очереди после падения. Реплики обязаны сохраниться без имён."""
        segments = [Segment(at=0.0, end=6.0, text="реплика")]
        listener = _listener(tmp_path, segments, _Embedder({}))
        _open(listener, app="slack.exe", title="Viktor Gavrylenko - Sintegrum Team - Slack")

        listener._transcribe_into(listener.session, SYSTEM, 0.0, _pcm(1), None)
        utterances = list(read_payload(listener.session)["utterances"])
        assert len(utterances) == 1
        assert listener._name_speakers(
            utterances, _closed(listener, "slack.exe", "x"), None) == 0

    def test_broken_resolve_keeps_the_conversation(self, tmp_path):
        """Текст уже распознан и ценнее разметки: сбой имён не имеет права
        утащить сессию."""
        segments = [Segment(at=0.0, end=6.0, text="реплика")]
        listener = _listener(tmp_path, segments, _Embedder({"1": _vec(0)}))
        _open(listener, app="slack.exe", title="Viktor Gavrylenko - Sintegrum Team - Slack")
        listener._transcribe_into(listener.session, SYSTEM, 0.0, _pcm(1),
                                  listener._speakers)

        class _Broken(SpeakerSession):
            def resolve(self, counterpart=None):
                raise RuntimeError("кластеризация сорвалась")

        broken = _Broken(listener._embedder, listener.voiceprints)
        utterances = list(read_payload(listener.session)["utterances"])
        assert listener._name_speakers(
            utterances, _closed(listener, "slack.exe", "x"), broken) == 0
        assert utterances[0]["text"] == "реплика"


class TestVoiceprintsAcrossCalls:
    def test_name_learned_one_on_one_is_found_in_a_group(self, tmp_path):
        """Ради этого всё и затевалось: Slack называет собеседника в личке,
        а потом тот же голос узнаётся в общем созвоне Meet, где имён нет."""
        segments = [Segment(at=0.0, end=6.0, text="реплика")]

        private = _listener(tmp_path, segments, _Embedder({"1": _vec(3)}))
        title = "Вадим Кудрявцев - Sintegrum Team - Slack"
        _open(private, app="slack.exe", title=title)
        private._transcribe_into(private.session, SYSTEM, 0.0, _pcm(1),
                                 private._speakers)
        private._name_speakers(list(read_payload(private.session)["utterances"]),
                               _closed(private, "slack.exe", title),
                               private._speakers)
        assert private.voiceprints.names == ["Вадим Кудрявцев"]

        group = _listener(tmp_path, segments, _Embedder({"1": _vec(3), "2": _vec(5)}))
        meet = "Meet - Google Chrome"
        _open(group, app="chrome.exe", title=meet)
        for marker, offset in ((1, 0.0), (2, 10.0)):
            group._transcribe_into(group.session, SYSTEM, offset, _pcm(marker),
                                   group._speakers)
        utterances = list(read_payload(group.session)["utterances"])
        group._name_speakers(utterances, _closed(group, "chrome.exe", meet),
                             group._speakers)

        assert "Вадим Кудрявцев" in {u.get("speaker") for u in utterances}

    def test_telegram_chat_name_is_not_remembered(self, tmp_path):
        """Имя чата в Telegram может оказаться группой — разметить им можно,
        запомнить голос нельзя."""
        segments = [Segment(at=0.0, end=6.0, text="реплика")]
        listener = _listener(tmp_path, segments, _Embedder({"1": _vec(0)}))
        title = "Кайфушники Нячанга"
        _open(listener, app="telegram.exe", title=title)
        listener._transcribe_into(listener.session, SYSTEM, 0.0, _pcm(1),
                                  listener._speakers)
        utterances = list(read_payload(listener.session)["utterances"])
        listener._name_speakers(utterances,
                                _closed(listener, "telegram.exe", title),
                                listener._speakers)

        assert utterances[0]["speaker"] == "Кайфушники Нячанга"
        assert listener.voiceprints.names == []


class TestEmptySessionIsNotFalsy:
    """Регресс, из-за которого функция молча не работала бы вовсе.

    `SpeakerSession` имеет `__len__`, поэтому ТОЛЬКО ЧТО созданная сессия —
    пустая — ложна по истинности. Проверка вида `if speakers` пропускала бы
    первый кусок, а непустой сессия без него уже не станет: отпечатки не
    снимались бы НИКОГДА, и ни одна строка в логе об этом не сказала бы.
    """

    def test_first_chunk_of_a_fresh_session_is_embedded(self, tmp_path):
        segments = [Segment(at=0.0, end=6.0, text="самая первая реплика")]
        embedder = _Embedder({"1": _vec(0)})
        listener = _listener(tmp_path, segments, embedder)
        _open(listener, app="slack.exe",
              title="Viktor Gavrylenko - Sintegrum Team - Slack")

        assert len(listener._speakers) == 0, "сессия пуста и потому ложна"
        assert not listener._speakers, "именно та ловушка, ради которой тест"

        listener._transcribe_into(listener.session, SYSTEM, 0.0, _pcm(1),
                                  listener._speakers)
        assert len(listener._speakers) == 1, "первый же кусок обязан дать отпечаток"


class TestEmptyAndPartialLines:
    """Что делать с репликами, для которых отпечатка нет."""

    def test_empty_text_never_becomes_a_speaker(self, tmp_path):
        """Пустую реплику очередь не пишет, значит и отпечаток с неё висел бы
        без пары — а в кластеризации он участвует и может занять слот
        говорящего тишиной."""
        segments = [Segment(at=0.0, end=3.0, text="   "),
                    Segment(at=3.0, end=6.0, text="настоящая речь")]
        embedder = _Embedder({"1": _vec(0)})
        listener = _listener(tmp_path, segments, embedder)
        _open(listener, app="slack.exe",
              title="Viktor Gavrylenko - Sintegrum Team - Slack")

        listener._transcribe_into(listener.session, SYSTEM, 0.0, _pcm(1),
                                  listener._speakers)
        assert len(listener._speakers) == 1, "пустая реплика дала лишний отпечаток"
        assert len(read_payload(listener.session)["utterances"]) == 1

    def test_single_voice_fills_in_lines_without_a_print(self, tmp_path):
        """Голос один — значит безымянная реплика может быть только его.
        Иначе один человек выглядел бы в выжимке как двое."""
        segments = [Segment(at=0.0, end=6.0, text="длинная реплика"),
                    Segment(at=6.0, end=6.05, text="ага")]
        # На второй срез модель отвечает None — он слишком короткий.
        embedder = _Embedder({"1": _vec(0)})
        listener = _listener(tmp_path, segments, embedder)
        title = "Viktor Gavrylenko - Sintegrum Team - Slack"
        _open(listener, app="slack.exe", title=title)

        listener._transcribe_into(listener.session, SYSTEM, 0.0, _pcm(1),
                                  listener._speakers)
        utterances = list(read_payload(listener.session)["utterances"])
        listener._name_speakers(utterances, _closed(listener, "slack.exe", title),
                                listener._speakers)
        assert [u.get("speaker") for u in utterances] == [
            "Viktor Gavrylenko", "Viktor Gavrylenko"]

    def test_several_voices_leave_unknown_lines_unnamed(self, tmp_path):
        """Голосов несколько — приписать безымянную реплику наугад хуже, чем
        оставить её без имени."""
        segments = [Segment(at=0.0, end=6.0, text="реплика")]
        embedder = _Embedder({"1": _vec(0), "2": _vec(1), "9": None})
        listener = _listener(tmp_path, segments, embedder)
        title = "Meet - Google Chrome"
        _open(listener, app="chrome.exe", title=title)

        for marker, offset in ((1, 0.0), (2, 10.0), (9, 20.0)):
            listener._transcribe_into(listener.session, SYSTEM, offset,
                                      _pcm(marker), listener._speakers)
        utterances = list(read_payload(listener.session)["utterances"])
        listener._name_speakers(utterances, _closed(listener, "chrome.exe", title),
                                listener._speakers)
        assert utterances[2].get("speaker") is None, "безымянной приписали наугад"
