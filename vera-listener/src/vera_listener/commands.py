"""Голосовые поручения Вере прямо во время разговора.

Одна `CommandWatch` на сессию. Каждую реплику микрофона проверяем сразу после
распознавания, звонка не ждём: владелец говорит «Вера, мне нужна помощь,
<поручение>», и поручение уходит на сервер, как только ясно, что это его голос
(`command_guard`).

Дорожка `system` командой не бывает никогда: там собеседники, и любой из них
на созвоне сказал бы фразу за владельца. Реплики оттуда идут только в проверку
эха.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from vera_listener import codeword
from vera_listener.command_guard import BLIND, ECHO, OWN, SystemTrack
from vera_listener.command_intake import (
    CONTINUE_GAP_S,
    Pending,
    doubts_for,
    payload,
)

log = logging.getLogger("listener.commands")

MIC = "mic"

#: Сколько ждать поручения, если фраза попала на конец куска. Больше — и в
#: поручение уедет посторонняя следующая фраза разговора.
FOLLOWUP_S = 15.0

#: Повтор того же поручения в этом окне — дубль (перераспознавание, «Вера,
#: мне нужна помощь» сказано дважды подряд), а не новая просьба.
DUP_WINDOW_S = 120.0
DUP_RATIO = 0.85


class CommandWatch:
    def __init__(self, session_id: str, started_wall: datetime,
                 deliver: Callable[[dict[str, Any]], None], *,
                 phrase: str = codeword.DEFAULT_PHRASE,
                 app: str | None = None, window_title: str | None = None,
                 clock: Callable[[], float] | None = None):
        self.session_id = session_id
        self.started_wall = started_wall
        self.deliver = deliver
        self.phrase = phrase
        self.app = app
        self.window_title = window_title
        self.system = SystemTrack()
        #: Часы сессии в тех же секундах, что и `at` реплик. Без внешних —
        #: часы захвата: в тестах кадры идут быстрее настоящего времени.
        self.clock = clock or (lambda: self.system.heard_until)
        self._pending: list[Pending] = []
        self._sent: list[tuple[float, str]] = []
        self._seen: set[tuple[float, str]] = set()
        self._last_mic: tuple[float, str] | None = None

    def hear(self, track: str, at: float, duration: float, speech: bool) -> None:
        # Микрофон двигает только часы: речь в нём эхом не бывает источником.
        self.system.hear(at, duration, speech, system=track != MIC)

    def on_segment(self, track: str, at: float, end: float, text: str) -> None:
        if track != MIC or not text.strip():
            return
        key = (round(at, 1), text.strip())
        if key in self._seen:
            return
        self._seen.add(key)
        previous, self._last_mic = self._last_mic, (end, text)
        if self._continue(at, end, text) or self._attach_followup(at, end, text):
            self.tick()
            return
        hit = codeword.find(text, self.phrase)
        if hit is None:
            return
        prefix = text[:hit.start]
        quoted = hit.quoted or (not codeword.words(prefix) and previous is not None
                                and at - previous[0] <= CONTINUE_GAP_S
                                and codeword.quote_intro(previous[1].rstrip(" :")))
        if quoted:
            # Не отбрасываем молча: «я говорю, Вера…» бывает и своей просьбой.
            # Сервер откроет задачу с пометкой «источник не подтверждён».
            log.info("кодовая фраза на %.1fс похожа на пересказ — уйдёт как "
                     "неподтверждённое", at)
        own = text[hit.start:]
        pending = Pending(at=at, parts=[(at, end, text)], fragment=[(at, end, own)],
                          instruction=hit.instruction, score=hit.score,
                          doubts=doubts_for(prefix, hit.instruction, quoted=quoted))
        if not hit.instruction:
            pending.followup_until = end + FOLLOWUP_S
        pending.detected_at = self.clock()
        self._pending.append(pending)
        log.info("кодовая фраза на %.1fс сессии %s%s", at, self.session_id,
                 "" if hit.instruction else " — поручение жду следующей репликой")
        self.tick()

    def _continue(self, at: float, end: float, text: str) -> bool:
        """Продолжение поручения дописывается; любая другая реплика его закрывает."""
        for pending in self._pending:
            if not pending.instruction or pending.closed is not None:
                continue
            if pending.continues(at, text):
                pending.extend(at, end, text)
                return True
            pending.closed = True
        return False

    def chunk_done(self, track: str, end: float) -> None:
        """Кусок микрофона распознан целиком: он режется на паузе, значит за
        последней репликой куска — пауза, и поручение в нём закрыто."""
        if track != MIC:
            return
        for pending in self._pending:
            if pending.instruction and pending.closed is None:
                pending.closed = True
        self.tick()

    def _attach_followup(self, at: float, end: float, text: str) -> bool:
        for pending in self._pending:
            if pending.instruction or pending.followup_until is None:
                continue
            if at > pending.followup_until:
                # Срок судим по времени следующей РЕПЛИКИ, а не по часам захвата:
                # следующий кусок микрофона распознаётся через минуту и позже,
                # и по часам поручение истекало бы, ещё не дойдя до распознавания.
                pending.followup_until = None
                pending.reprompt = True
                log.info("кодовая фраза на %.1fс без поручения — переспрошу",
                         pending.at)
                continue
            # Следующая реплика сама может начинаться с фразы — тогда поручение
            # то, что после неё, а не обращение целиком.
            hit = codeword.find(text, self.phrase)
            pending.parts.append((at, end, text))
            pending.fragment.append((at, end, text[hit.start:] if hit else text))
            if hit is not None and not hit.instruction:
                pending.followup_until = end + FOLLOWUP_S
                return True
            pending.instruction = hit.instruction if hit else text.strip()
            pending.followup_until = None
            return True
        return False

    def tick(self, *, final: bool = False) -> None:
        still: list[Pending] = []
        for pending in self._pending:
            if not pending.instruction and not pending.reprompt:
                if pending.followup_until is None:
                    # На закрытии фраза так и осталась без поручения.
                    pending.reprompt = True
                elif not final:
                    still.append(pending)
                    continue
                else:
                    pending.reprompt = True
            if pending.instruction and pending.closed is None and not final:
                still.append(pending)
                continue
            verdicts = {self.system.verdict(a, e, t, final=final)
                        for a, e, t in pending.parts}
            if ECHO in verdicts:
                log.warning("кодовая фраза на %.1fс похожа на голос собеседника "
                            "из динамиков — не исполняю", pending.at)
            elif verdicts <= {OWN, BLIND} and BLIND in verdicts:
                # Слепота — не улика против владельца: чаще всего фраза открыла
                # сессию раньше первого кадра loopback. Отбросить — потерять
                # просьбу молча; с сомнением `blind` сервер откроет задачу с
                # пометкой «источник не подтверждён» и ничего не исполнит.
                log.info("кодовая фраза на %.1fс: звук с динамиков не проверить "
                         "(нет кадров системной дорожки) — уйдёт как неподтверждённое",
                         pending.at)
                if "blind" not in pending.doubts:
                    pending.doubts.append("blind")
                self._accept(pending)
            elif verdicts == {OWN}:
                self._accept(pending)
            else:
                still.append(pending)
        self._pending = still

    def wants_system_text(self) -> bool:
        """Есть ли поручение, которому для решения нужен распознанный системный звук."""
        return any(p.instruction for p in self._pending)

    def close(self) -> None:
        self.tick(final=True)

    def _accept(self, pending: Pending) -> None:
        norm = " ".join(codeword.words(pending.instruction))
        for at, prev in self._sent if norm else ():
            if (abs(at - pending.at) <= DUP_WINDOW_S
                    and SequenceMatcher(None, norm, prev).ratio() >= DUP_RATIO):
                log.info("поручение на %.1fс — повтор предыдущего, второй раз "
                         "не отправляю", pending.at)
                return
        self._sent.append((pending.at, norm))
        digest = hashlib.sha1(
            f"{self.session_id}|{round(pending.at, 1)}".encode()).hexdigest()[:16]
        silent = "blind" not in pending.doubts and all(
            self.system.speakers_silent(a, e) for a, e, _ in pending.parts)
        command = payload(pending, command_id=f"vc-{digest}", session_id=self.session_id,
                          started=self.started_wall, app=self.app,
                          window_title=self.window_title, speakers_silent=silent)
        log.info("поручение принято (%.1fс, %s, %d симв., уверенность %.2f) — "
                 "в очередь отправки", pending.at, command["kind"],
                 len(command["instruction"]), command["confidence"])
        log.debug("поручение: %s", command["instruction"])
        now, phrase_end = self.clock(), pending.fragment[-1][1]
        log.info("задержка %s: конец фразы→распознано %d мс, →в очередь %d мс",
                 command["command_id"], (pending.detected_at - phrase_end) * 1000,
                 (now - phrase_end) * 1000)
        self.deliver(command)
