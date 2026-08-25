"""Нарезка непрерывного потока речи на разговоры. Чистая логика, без звука.

Сессию НЕ закрывает смена окна переднего плана: во время созвона alt-tab
происходит постоянно, и по нему разговор рассыпался бы на куски. Закрывает
тишина, смена звучащего приложения и предохранитель по длительности.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Session:
    started_at: float
    app: str | None
    window_title: str | None
    last_speech_at: float
    speech_s: dict[str, float] = field(default_factory=dict)

    def speech_total(self) -> float:
        return sum(self.speech_s.values())

    def duration(self, now: float) -> float:
        return max(0.0, now - self.started_at)


@dataclass
class Closed:
    session: Session
    ended_at: float
    reason: str


class Segmenter:
    """feed() на каждый кадр VAD; отдаёт закрытую сессию, когда та закончилась."""

    def __init__(self, *, silence_timeout_s: float = 60.0,
                 max_session_s: float = 7200.0, frame_s: float = 0.032):
        self.silence_timeout_s = silence_timeout_s
        self.max_session_s = max_session_s
        self.frame_s = frame_s
        self.current: Session | None = None

    def feed(self, now: float, track: str, speech: bool, *,
             app: str | None = None, window_title: str | None = None) -> Closed | None:
        closed = self._maybe_close(now, app)
        if speech:
            if self.current is None:
                self.current = Session(started_at=now, app=app,
                                       window_title=window_title, last_speech_at=now)
            session = self.current
            session.last_speech_at = now
            session.speech_s[track] = session.speech_s.get(track, 0.0) + self.frame_s
            # Приложение и заголовок могли появиться уже после первой реплики
            # (звонок начался раньше, чем поднялось окно) — дописываем.
            if session.app is None and app:
                session.app = app
            if session.window_title is None and window_title:
                session.window_title = window_title
        return closed

    def _maybe_close(self, now: float, app: str | None) -> Closed | None:
        session = self.current
        if session is None:
            return None
        if now - session.last_speech_at >= self.silence_timeout_s:
            return self._close(session, session.last_speech_at, "silence")
        if session.duration(now) >= self.max_session_s:
            return self._close(session, now, "max_duration")
        if app and session.app and app != session.app:
            return self._close(session, session.last_speech_at, "app_changed")
        return None

    def _close(self, session: Session, ended_at: float, reason: str) -> Closed:
        self.current = None
        return Closed(session=session, ended_at=ended_at, reason=reason)

    def flush(self, now: float, reason: str = "shutdown") -> Closed | None:
        """Закрыть открытую сессию принудительно — при остановке слушателя."""
        if self.current is None:
            return None
        return self._close(self.current, min(now, self.current.last_speech_at), reason)
