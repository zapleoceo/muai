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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from vera_listener import codeword
from vera_listener.command_guard import BLIND, ECHO, OWN, SystemTrack

log = logging.getLogger("listener.commands")

MIC = "mic"

#: Сколько ждать поручения, если фраза попала на конец куска. Больше — и в
#: поручение уедет посторонняя следующая фраза разговора.
FOLLOWUP_S = 15.0

#: Повтор того же поручения в этом окне — дубль (перераспознавание, «Вера,
#: мне нужна помощь» сказано дважды подряд), а не новая просьба.
DUP_WINDOW_S = 120.0
DUP_RATIO = 0.85


@dataclass
class _Pending:
    at: float
    #: Реплики, которые должны оказаться голосом владельца: сама фраза и, если
    #: поручение пришло следом, реплика с ним.
    parts: list[tuple[float, float, str]] = field(default_factory=list)
    instruction: str = ""
    followup_until: float | None = None


class CommandWatch:
    def __init__(self, session_id: str, started_wall: datetime,
                 deliver: Callable[[dict[str, Any]], None], *,
                 phrase: str = codeword.DEFAULT_PHRASE,
                 app: str | None = None, window_title: str | None = None):
        self.session_id = session_id
        self.started_wall = started_wall
        self.deliver = deliver
        self.phrase = phrase
        self.app = app
        self.window_title = window_title
        self.system = SystemTrack()
        self._pending: list[_Pending] = []
        self._sent: list[tuple[float, str]] = []
        self._seen: set[tuple[float, str]] = set()

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
        if self._attach_followup(at, end, text):
            self.tick()
            return
        hit = codeword.find(text, self.phrase)
        if hit is None:
            return
        pending = _Pending(at=at, parts=[(at, end, text)],
                           instruction=hit.instruction)
        if not hit.instruction:
            pending.followup_until = end + FOLLOWUP_S
        self._pending.append(pending)
        log.info("кодовая фраза на %.1fс сессии %s%s", at, self.session_id,
                 "" if hit.instruction else " — поручение жду следующей репликой")
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
                log.info("кодовая фраза на %.1fс без поручения — пропускаю",
                         pending.at)
                continue
            # Следующая реплика сама может начинаться с фразы — тогда поручение
            # то, что после неё, а не обращение целиком.
            hit = codeword.find(text, self.phrase)
            pending.parts.append((at, end, text))
            if hit is not None and not hit.instruction:
                pending.followup_until = end + FOLLOWUP_S
                return True
            pending.instruction = hit.instruction if hit else text.strip()
            pending.followup_until = None
            return True
        return False

    def tick(self, *, final: bool = False) -> None:
        still: list[_Pending] = []
        for pending in self._pending:
            if not pending.instruction:
                if pending.followup_until is not None and not final:
                    still.append(pending)
                continue
            verdicts = {self.system.verdict(a, e, t, final=final)
                        for a, e, t in pending.parts}
            if BLIND in verdicts:
                log.warning("кодовая фраза на %.1fс: звук с динамиков не пишется "
                            "(нет кадров системной дорожки) — чей голос, не "
                            "проверить, не исполняю", pending.at)
            elif ECHO in verdicts:
                log.warning("кодовая фраза на %.1fс похожа на голос собеседника "
                            "из динамиков — не исполняю", pending.at)
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

    def _accept(self, pending: _Pending) -> None:
        norm = " ".join(codeword.words(pending.instruction))
        for at, prev in self._sent:
            if (abs(at - pending.at) <= DUP_WINDOW_S
                    and SequenceMatcher(None, norm, prev).ratio() >= DUP_RATIO):
                log.info("поручение на %.1fс — повтор предыдущего, второй раз "
                         "не отправляю", pending.at)
                return
        self._sent.append((pending.at, norm))
        spoken = self.started_wall + timedelta(seconds=pending.at)
        digest = hashlib.sha1(
            f"{self.session_id}|{round(pending.at, 1)}".encode()).hexdigest()[:16]
        log.info("поручение принято (%.1fс, %d симв.) — в очередь отправки",
                 pending.at, len(pending.instruction))
        log.debug("поручение: %s", pending.instruction)
        self.deliver({
            "command_id": f"vc-{digest}",
            "instruction": pending.instruction,
            "spoken_at": spoken.isoformat(),
            "app": self.app,
            "window_title": self.window_title,
        })
