"""Сборка слушателя: захват → VAD → сессии → распознавание → очередь → отправка.

Три потока с разной ценой кадра. Главный поток делает только VAD и нарезку
(дёшево, нельзя тормозить), распознавание живёт отдельно (дорого, всплеском),
отправка — отдельно ещё раз, иначе минутный таймаут сети встал бы поперёк
захвата звука.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from vera_listener.capture import MIC, SYSTEM, Capture, Frame
from vera_listener.config import Config
from vera_listener.counterpart import counterpart
from vera_listener.dedup import mark_echo
from vera_listener.gate import judge, system_audio_allowed
from vera_listener.hold import BYTES_PER_S, Hold
from vera_listener.outbox import Outbox, read_payload
from vera_listener.recorder import PAUSE_FLUSH_S, TrackRecorder
from vera_listener.segmenter import Closed, Segmenter
from vera_listener.sender import Sender
from vera_listener.speakers import (
    OpenVinoSpeakerEmbedder,
    SpeakerSession,
    VoiceprintRegistry,
)
from vera_listener.status import DEAF, IDLE, TALKING, Status
from vera_listener.transcriber import Transcriber, pcm_to_float, slice_seconds
from vera_listener.vad import SpeechDetector
from vera_listener.winctx import active_audio_app, foreground_window_title

log = logging.getLogger("listener")

CONTEXT_POLL_S = 2.0
#: Сколько системного звука держим, пока не ясно — созвон это или ролик.
#: Десять минут PCM16 16 кГц ≈ 19 МБ; дальше забываем самое старое.
HOLD_MAX_S = 600.0
#: сколько секунд без кадров считаем глухотой, а не переоткрытием устройства
DEAF_AFTER_TICKS = 3


class Listener:
    def __init__(self, config: Config, status: Status | None = None):
        self.config = config
        # Состояние для иконки в трее. Без трея это просто счётчики в памяти —
        # слушателю они не мешают и ничего не стоят.
        self.status = status or Status()
        self.frames: queue.Queue[Frame] = queue.Queue(maxsize=4000)
        self.jobs: queue.Queue[tuple] = queue.Queue()
        self.outbox = Outbox(config.queue_dir)
        self.capture = Capture(self.frames)
        self.transcriber = Transcriber(config)
        self.sender = Sender(config, self.outbox)
        # Опознание говорящих. Хранилище отпечатков — одно на всё время
        # жизни слушателя, сессия опознания — своя на каждый разговор.
        self.voiceprints = VoiceprintRegistry(config.voiceprints_file)
        self._embedder = OpenVinoSpeakerEmbedder(config.speaker_model_dir)
        self._speakers: SpeakerSession | None = None
        self.segmenter = Segmenter(silence_timeout_s=config.silence_timeout_s,
                                   max_session_s=config.max_session_s)
        self.detectors = {MIC: SpeechDetector(), SYSTEM: SpeechDetector()}
        self.recorders = {
            track: TrackRecorder(track, config.chunk_speech_s,
                                 max_wall_s=config.chunk_max_wall_s)
            for track in (MIC, SYSTEM)
        }
        self.session: Path | None = None
        self._session_wall: datetime | None = None
        self._session_zero: float = 0.0
        # Продолжение той же встречи после разреза по длительности: (id, номер
        # следующей части). None — следующая сессия начинает новую встречу.
        self._continues: tuple[str, int] | None = None
        self._meeting: tuple[str, int] | None = None
        # Системный звук сессии, ещё не прошедшей ворота: распознавать его
        # сразу — значит платить за каждый ютуб полностью (см. hold.py).
        self._held = Hold(int(HOLD_MAX_S * BYTES_PER_S))
        self._silent_ticks = 0
        self._stop = threading.Event()

    def run(self) -> None:
        self.outbox.recover()
        self.capture.start()
        threading.Thread(target=self._work, name="stt", daemon=True).start()
        threading.Thread(target=self._send, name="sender", daemon=True).start()
        log.info("слушаю: микрофон + системный звук, очередь %s", self.config.queue_dir)
        try:
            self._pump()
        except KeyboardInterrupt:
            log.info("остановка по Ctrl+C")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        closed = self.segmenter.flush(time.monotonic())
        if closed:
            self._finish(closed)
        self.capture.stop()

    def _pump(self) -> None:
        app: str | None = None
        title: str | None = None
        polled = 0.0
        while not self._stop.is_set():
            try:
                frame = self.frames.get(timeout=1.0)
            except queue.Empty:
                self._tick_idle()
                continue
            if self._silent_ticks:
                self._silent_ticks = 0
                if self.segmenter.current is None:
                    self.status.set_state(IDLE)

            speech = self.detectors[frame.track].is_speech(frame.pcm)
            now = time.monotonic()
            # Спрашиваем «кто сейчас звучит», только когда ответ кому-то нужен.
            # Перебор звуковых сессий Windows стоит 28 мс процессора на вызов
            # (замер 04.09) — раз в две секунды это 1.4% ядра НЕПРЕРЫВНО, то
            # есть десятая часть всего расхода слушателя. В тишине без открытой
            # сессии контекст не читает никто: он нужен сегментатору, а тот
            # спрашивает его с первой же речевой рамки. К ней и опрашиваем —
            # `polled` к тому моменту давно просрочен, так что свежесть та же.
            if ((speech or self.segmenter.current is not None)
                    and now - polled >= CONTEXT_POLL_S):
                app, title, polled = active_audio_app(), foreground_window_title(), now

            closed = self.segmenter.feed(frame.at, frame.track, speech,
                                         app=app, window_title=title)
            if closed:
                self._finish(closed)
            if self.segmenter.current is not None:
                self._ensure_open()
                self._record(frame, speech)

    def _tick_idle(self) -> None:
        """Кадров нет (устройство переоткрывается) — но тишина всё равно течёт."""
        self._silent_ticks += 1
        # Три пустых секунды подряд — это уже не «переоткрываю поток», а
        # глухота: чужая сессия Windows либо выдернули устройство. В трее это
        # красный, чтобы не выглядело работающим.
        if self._silent_ticks >= DEAF_AFTER_TICKS and self.segmenter.current is None:
            self.status.set_state(DEAF)
        closed = self.segmenter.feed(time.monotonic(), MIC, False)
        if closed:
            self._finish(closed)

    def _ensure_open(self) -> None:
        session = self.segmenter.current
        if self.session is not None or session is None:
            return
        self._session_zero = session.started_at
        self._session_wall = self._wall(session.started_at)
        session_id = self._session_wall.strftime("s-%Y%m%dT%H%M%S")
        meeting_id, part = self._continues or (session_id, 1)
        self._continues = None
        self._meeting = (meeting_id, part)
        # Своя сессия опознания на каждый разговор: отпечатки одного
        # созвона не должны смешиваться с соседним.
        self._speakers = SpeakerSession(self._embedder, self.voiceprints)
        self.session = self.outbox.start(
            session_id, self._session_wall.isoformat(),
            app=session.app, window_title=session.window_title,
            device_hint=self.capture.device_hint,
            meeting_id=meeting_id, part=part,
        )
        self.status.set_state(TALKING)
        if part > 1:
            log.info("разговор продолжается, часть %d (%s)", part, meeting_id)
        else:
            log.info("разговор начался (%s / %s)", session.app or "?",
                     session.window_title or "?")

    def _record(self, frame: Frame, speech: bool) -> None:
        recorder = self.recorders[frame.track]
        recorder.add(frame.pcm, speech, frame.at - self._session_zero)
        if recorder.ready():
            self._queue_chunk(frame.track, via_ready=True)
        if self._held and self._system_confirmed():
            self._flush_held()

    def _queue_chunk(self, track: str, *, via_ready: bool = False) -> None:
        """`via_ready` — вызов идёт следом за `ready()==True` (обычный ход).

        Без него зовёт `_finish()`: там флашится ХВОСТ на закрытии сессии
        независимо от `ready()`, и по низкой `silence_s` предохранитель
        распознался бы там ложно — обрывок сессии не проходил через него
        вовсе. Нашло на этом же шаге, ещё до коммита.
        """
        recorder = self.recorders[track]
        # До take(): он сбрасывает счётчики, а после уже не отличить, сработал
        # ли предохранитель по времени или обычная пауза. Событие редкое (пауза
        # короче 2с минутами) — стоит видеть в логе, не молчать о нём.
        #
        # Прямое ЗЕРКАЛО первого условия ready(), а не приближение через
        # «пауза короче 2с»: тот вариант давал ложный НЕГАТИВ, если
        # предохранитель срабатывал ровно в момент паузы ≥2с, но норма речи
        # ещё не набрана (редкая речь, растянутая на 5 минут через несколько
        # пауз чуть за 2с) — silence_s тогда уже большой, «пауза короче 2с»
        # не срабатывала, и предохранитель проходил незамеченным ровно в
        # том случае, ради которого лог и нужен. Нашло ревью.
        normal_flush = (recorder.speech_s >= recorder.chunk_speech_s
                        and recorder.silence_s >= PAUSE_FLUSH_S)
        forced = via_ready and not normal_flush
        taken = recorder.take()
        if not taken or self.session is None:
            return
        if forced:
            log.info("дорожка %s: кусок закрыт по предохранителю (%.0fс без "
                    "паузы ≥2с), не дожидаясь естественной остановки",
                    track, self.config.chunk_max_wall_s)
        offset, pcm = taken
        # Системный звук идёт в распознавание только когда ворота уже пропускают
        # эту сессию. Иначе — в копилку: ролик так не стоит ни такта, а созвон
        # ничего не теряет, потому что копилка уйдёт распознаваться, как только
        # заговорит микрофон (или сессию примут на закрытии).
        if track == SYSTEM and not self._system_confirmed():
            self._held.add(offset, pcm)
            return
        self.jobs.put(("chunk", self.session, track, offset, pcm, self._speakers))

    def _system_confirmed(self) -> bool:
        """Прошла ли сессия ворота настолько, что системный звук уже ценен."""
        session = self.segmenter.current
        if session is None:
            return False
        return system_audio_allowed(
            session.app, mic_speech_s=session.speech_s.get(MIC, 0.0),
            allow=self.config.allow_apps, browsers=self.config.browser_apps,
            deny=self.config.deny_apps)

    def _flush_held(self) -> None:
        """Микрофон заговорил — накопленное больше не под вопросом."""
        if self.session is None:
            return
        for offset, pcm in self._held.take():
            self.jobs.put(("chunk", self.session, SYSTEM, offset, pcm, self._speakers))

    def _finish(self, closed: Closed) -> None:
        if self.session is None:
            return
        speech_s = dict(closed.session.speech_s)
        for track in (MIC, SYSTEM):
            self._queue_chunk(track)
        self.jobs.put(("close", self.session, closed, speech_s,
                       self._wall(closed.ended_at), self._held.take(),
                       self._held.dropped_s, self._speakers))
        self._held.clear()
        # Разрез по предохранителю — не конец разговора: следующая сессия
        # продолжает ту же встречу. Тишина и смена приложения — конец.
        if closed.reason == "max_duration" and self._meeting is not None:
            meeting_id, part = self._meeting
            self._continues = (meeting_id, part + 1)
        else:
            self._continues = None
        self.session = None
        self._session_wall = None
        self._meeting = None
        self._speakers = None
        self.status.set_state(IDLE)

    def _wall(self, monotonic_at: float) -> datetime:
        return datetime.now().astimezone() - timedelta(
            seconds=max(0.0, time.monotonic() - monotonic_at))

    def _transcribe_into(self, path: Path, track: str, offset: float, pcm: bytes,
                         speakers: SpeakerSession | None) -> None:
        """Распознать кусок, дописать реплики и снять отпечатки голосов.

        Отпечатки только с дорожки приложения: на микрофоне владелец, его
        опознавать незачем, а эхо из динамиков и так помечено отдельно.
        """
        # `is not None`, а НЕ `if speakers`: у сессии опознания есть
        # `__len__`, и пустая — та, что только началась, — ложна по
        # истинности. С проверкой на истинность отпечатки не снимались бы
        # НИКОГДА: первый же кусок видит пустую сессию и пропускает её,
        # а непустой она без него не станет. Поймано сквозным тестом.
        audio = (pcm_to_float(pcm)
                 if speakers is not None and track == SYSTEM else None)
        for segment in self.transcriber.transcribe(pcm):
            # Тем же условием, что и в `outbox.append`: пустую реплику очередь
            # молча не пишет, и снятый с неё отпечаток остался бы висячим —
            # реплики под него нет, а в кластеризации он участвует и способен
            # занять слот говорящего тишиной. Нашло ревью.
            if not segment.text.strip():
                continue
            self.outbox.append(path, offset + segment.at, track, segment.text)
            if audio is not None and speakers is not None:
                speakers.observe(
                    offset + segment.at,
                    slice_seconds(audio, segment.at, segment.end))

    def _name_speakers(self, utterances: list[dict[str, Any]], closed: Closed,
                       speakers: SpeakerSession | None) -> int:
        """Проставить имена говорящих в репликах. → сколько реплик названо.

        Имена меняются НА МЕСТЕ, потому что разметка говорящих — свойство
        реплики, а не отдельный список: иначе их пришлось бы сводить по
        смещению ещё раз, уже на сервере.
        """
        if speakers is None:
            return 0
        who = counterpart(closed.session.app, closed.session.window_title)
        try:
            names = speakers.resolve(who)
        except Exception as e:                          # noqa: BLE001
            # Разметка говорящих — надстройка над разговором. Текст уже
            # распознан и ценнее её: сбой не имеет права утащить сессию.
            log.warning("имена говорящих не проставились (%s)", type(e).__name__)
            return 0
        # Реплика без отпечатка (слишком короткий срез, сбой модели) осталась
        # бы безымянной среди названных — и один человек выглядел бы как двое:
        # часть его реплик «Вадим», часть «собеседник». Когда голос в разговоре
        # ОДИН, догадываться не о чем: других кандидатов нет. Когда их
        # несколько — оставляем без имени, приписать наугад хуже. Нашло ревью.
        distinct = set(names.values())
        fallback = next(iter(distinct)) if len(distinct) == 1 else None

        named = missing = 0
        for utterance in utterances:
            if utterance.get("stream") != SYSTEM:
                continue
            name = names.get(round(float(utterance.get("at", 0.0)), 2)) or fallback
            if name:
                utterance["speaker"] = name
                named += 1
            else:
                missing += 1
        if missing:
            log.info("реплик без опознанного голоса: %d из %d",
                     missing, named + missing)
        return named

    def _work(self) -> None:
        while not self._stop.is_set() or not self.jobs.empty():
            try:
                job = self.jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run_job(job)
            except Exception as e:
                log.exception("обработка сессии сорвалась: %s", e)

    def _run_job(self, job: tuple) -> None:
        kind = job[0]
        if kind == "chunk":
            _, path, track, offset, pcm, speakers = job
            self._transcribe_into(path, track, offset, pcm, speakers)
            return
        _, path, closed, speech_s, ended_wall, held, held_lost_s, speakers = job
        verdict = judge(speech_s, app=closed.session.app,
                        allow=self.config.allow_apps,
                        browsers=self.config.browser_apps,
                        deny=self.config.deny_apps,
                        min_speech_s=self.config.min_speech_s,
                        monologue_speech_s=self.config.monologue_speech_s)
        if not verdict.keep:
            held_s = sum(len(pcm) for _at, pcm in held) / BYTES_PER_S
            log.info("разговор отброшен (%s, %s), не распознавали %.0fс системного",
                     verdict.reason, closed.reason, held_s)
            self.status.note_dropped()
            self.outbox.drop(path)
            return
        # Сессию берём — теперь придержанный системный звук стоит распознать.
        if held_lost_s:
            log.warning("придержанного звука не хватило памяти: забыто %.0fс",
                        held_lost_s)
        for offset, pcm in held:
            self._transcribe_into(path, SYSTEM, offset, pcm, speakers)
        payload = read_payload(path)
        if payload is None:
            self.outbox.drop(path)
            return
        # По времени, а не по порядку дописывания: придержанное уехало в файл
        # позже реплик микрофона, а осмысление ждёт хронологию.
        in_order = sorted(payload["utterances"], key=lambda u: u.get("at", 0.0))
        # Помечаем эхо, но отправляем ВСЁ: в микрофонный кусок попадает и голос
        # из динамиков, и слова владельца. Что выбросить — решает сервер, и
        # только для осмысления; дословная стенограмма хранит всё.
        utterances: list[dict[str, Any]] = mark_echo(in_order)
        echoes = sum(1 for u in utterances if u.get("echo"))
        named = self._name_speakers(utterances, closed, speakers)
        self.outbox.finish(path, ended_wall.isoformat(), utterances=utterances)
        log.info("разговор сохранён: %s, реплик %d (из них эхо %d, с именем %d) (%s)",
                 closed.reason, len(utterances), echoes, named, verdict.reason)

    def _send(self) -> None:
        while not self._stop.is_set():
            try:
                sent, left = self.sender.flush()
                self.status.note_sent(sent, left)
            except Exception as e:
                log.exception("отправщик споткнулся: %s", e)
                self.status.note_error(f"{type(e).__name__}: {e}")
            self._stop.wait(max(self.config.send_interval_s, self.sender.backoff_s))
