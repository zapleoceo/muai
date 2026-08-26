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
from vera_listener.dedup import drop_echo
from vera_listener.gate import judge
from vera_listener.outbox import Outbox, read_payload
from vera_listener.recorder import TrackRecorder
from vera_listener.segmenter import Closed, Segmenter
from vera_listener.sender import Sender
from vera_listener.status import DEAF, IDLE, TALKING, Status
from vera_listener.transcriber import Transcriber
from vera_listener.vad import SpeechDetector
from vera_listener.winctx import active_audio_app, foreground_window_title

log = logging.getLogger("listener")

CONTEXT_POLL_S = 2.0
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
        self.segmenter = Segmenter(silence_timeout_s=config.silence_timeout_s,
                                   max_session_s=config.max_session_s)
        self.detectors = {MIC: SpeechDetector(), SYSTEM: SpeechDetector()}
        self.recorders = {track: TrackRecorder(track, config.chunk_speech_s)
                          for track in (MIC, SYSTEM)}
        self.session: Path | None = None
        self._session_wall: datetime | None = None
        self._session_zero: float = 0.0
        # Продолжение той же встречи после разреза по длительности: (id, номер
        # следующей части). None — следующая сессия начинает новую встречу.
        self._continues: tuple[str, int] | None = None
        self._meeting: tuple[str, int] | None = None
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

            now = time.monotonic()
            if now - polled >= CONTEXT_POLL_S:
                app, title, polled = active_audio_app(), foreground_window_title(), now

            speech = self.detectors[frame.track].is_speech(frame.pcm)
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
            self._queue_chunk(frame.track)

    def _queue_chunk(self, track: str) -> None:
        taken = self.recorders[track].take()
        if taken and self.session is not None:
            offset, pcm = taken
            self.jobs.put(("chunk", self.session, track, offset, pcm))

    def _finish(self, closed: Closed) -> None:
        if self.session is None:
            return
        speech_s = dict(closed.session.speech_s)
        for track in (MIC, SYSTEM):
            self._queue_chunk(track)
        self.jobs.put(("close", self.session, closed, speech_s,
                       self._wall(closed.ended_at)))
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
        self.status.set_state(IDLE)

    def _wall(self, monotonic_at: float) -> datetime:
        return datetime.now().astimezone() - timedelta(
            seconds=max(0.0, time.monotonic() - monotonic_at))

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
            _, path, track, offset, pcm = job
            for at, text in self.transcriber.transcribe(pcm):
                self.outbox.append(path, offset + at, track, text)
            return
        _, path, closed, speech_s, ended_wall = job
        verdict = judge(speech_s, app=closed.session.app,
                        allow=self.config.allow_apps,
                        browsers=self.config.browser_apps,
                        deny=self.config.deny_apps,
                        min_speech_s=self.config.min_speech_s,
                        monologue_speech_s=self.config.monologue_speech_s)
        if not verdict.keep:
            log.info("разговор отброшен (%s, %s)", verdict.reason, closed.reason)
            self.status.note_dropped()
            self.outbox.drop(path)
            return
        payload = read_payload(path)
        if payload is None:
            self.outbox.drop(path)
            return
        utterances: list[dict[str, Any]] = drop_echo(payload["utterances"])
        self.outbox.finish(path, ended_wall.isoformat(), utterances=utterances)
        log.info("разговор сохранён: %s, реплик %d (%s)",
                 closed.reason, len(utterances), verdict.reason)

    def _send(self) -> None:
        while not self._stop.is_set():
            try:
                sent, left = self.sender.flush()
                self.status.note_sent(sent, left)
            except Exception as e:
                log.exception("отправщик споткнулся: %s", e)
                self.status.note_error(f"{type(e).__name__}: {e}")
            self._stop.wait(max(self.config.send_interval_s, self.sender.backoff_s))
