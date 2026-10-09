"""Что из просьбы уходит на сервер и насколько слушатель уверен, что это она.

На сервер уходит только сама просьба: реплика с кодовой фразой (с места
обращения — слова до него в той же реплике отрезаются), реплика с поручением,
если оно пришло следом, и не больше `MAX_AFTER` реплик-продолжений. Ни одной
реплики до фразы: окружающий разговор серверу для задачи не нужен, а в
комнату агентов он попал бы целиком.

Продолжение — следующая реплика микрофона, начатая не позже `CONTINUE_GAP_S`
после конца предыдущей, когда та не закончилась точкой. Точка у whisper —
конец интонационной фразы: после неё начинается уже другое.

Уверенность (`confidence`, 0..1) — взвешенная сумма трёх признаков:

    confidence = 0.3·s + 0.5·g + 0.2·c

- s — сходство кодовой фразы, растянутое с [0.8, 1] на [0, 1]. Ниже 0.8
  фраза не ловится вовсе (`codeword.TAIL_RATIO`), поэтому 0.8 — ноль шкалы;
- g — guard: 1, если динамики во всём окне молчали (голос мог быть только
  владельца), 0.4, если звучали и владельцем фразу признала лишь сверка
  текста с распознанным системным звуком. Второй путь слабее: распознавание
  системного звука неточно, и сверка может не узнать в эхе ту же фразу;
- c — фраза закрыта: 1 — после неё пауза (кусок микрофона закончился или
  дальше пошла другая фраза), 0.5 — сессия оборвалась сразу за ней и паузы
  никто не видел. Обрыв на союзе или запятой задачей не становится вовсе —
  это «переспроси».

Порог `CONFIRM_BELOW` = 0.75 выбран так, чтобы звучавшие динамики сами по
себе всегда требовали подтверждения владельца: максимум при g = 0.4 —
0.3 + 0.2 + 0.2 = 0.7. При молчавших динамиках и закрытой фразе задача
заводится сразу, если сходство фразы не ниже ~0.83 (s ≥ 0.17): «мне нужно
помошь» (0.87) проходит, а полуразобранная фраза у самого порога — уже нет.
Это стартовые числа без живых замеров; сервер решает по тому же порогу
(`vera_shared.voice_help.CONFIRM_BELOW`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from vera_listener import codeword

CONTINUE_GAP_S = 1.0
MAX_AFTER = 2

W_SIMILARITY, W_GUARD, W_CLOSED = 0.3, 0.5, 0.2
GUARD_SILENT, GUARD_TEXT = 1.0, 0.4
CLOSED_PAUSE, CLOSED_UNKNOWN = 1.0, 0.5
CONFIRM_BELOW = 0.75

#: Слова, на которых поручение не кончается: «найди письмо и…», «перешли его в…».
_DANGLING = frozenset({
    "и", "а", "но", "или", "что", "чтобы", "потому", "если", "когда", "то",
    "в", "во", "на", "с", "со", "к", "по", "для", "про", "у", "о", "об", "от",
    "из", "за", "как", "где", "который", "которая", "которое", "ну", "это",
})
_DANGLING_MARKS = (",", "—", "-", "–", ":", ";", "...", "…")
_SENTENCE_END = (".", "!", "?")
#: Столько слов перед обращением в той же реплике — фраза посреди мысли.
MID_SENTENCE_WORDS = 3
MIN_INSTRUCTION_WORDS = 2


def is_truncated(text: str) -> bool:
    """Пусто, шум без слов или обрыв на союзе, предлоге, запятой."""
    tail = text.rstrip()
    found = codeword.words(tail)
    if not found:
        return True
    return tail.endswith(_DANGLING_MARKS) or found[-1] in _DANGLING


def ends_sentence(text: str) -> bool:
    tail = text.rstrip()
    return tail.endswith(_SENTENCE_END) and not tail.endswith("...")


def confidence(similarity: float, *, speakers_silent: bool, closed: bool | None) -> float:
    s = min(1.0, max(0.0, (similarity - codeword.TAIL_RATIO) / (1 - codeword.TAIL_RATIO)))
    g = GUARD_SILENT if speakers_silent else GUARD_TEXT
    c = CLOSED_PAUSE if closed else CLOSED_UNKNOWN
    return round(W_SIMILARITY * s + W_GUARD * g + W_CLOSED * c, 3)


@dataclass
class Pending:
    at: float
    #: Реплики, которые должны оказаться голосом владельца (проверка guard).
    parts: list[tuple[float, float, str]] = field(default_factory=list)
    #: То, что уходит на сервер: фраза с места обращения и продолжения.
    fragment: list[tuple[float, float, str]] = field(default_factory=list)
    instruction: str = ""
    followup_until: float | None = None
    score: float = 1.0
    doubts: list[str] = field(default_factory=list)
    after: int = 0
    #: True — после поручения видна пауза; None — пока неизвестно.
    closed: bool | None = None
    #: Фраза без поручения так и осталась — переспросить.
    reprompt: bool = False

    def continues(self, at: float, text: str) -> bool:
        _, last_end, last_text = self.fragment[-1]
        return (self.after < MAX_AFTER and at - last_end <= CONTINUE_GAP_S
                and not ends_sentence(last_text) and codeword.find(text) is None)

    def extend(self, at: float, end: float, text: str) -> None:
        self.parts.append((at, end, text))
        self.fragment.append((at, end, text))
        self.instruction = f"{self.instruction} {text.strip()}".strip()
        self.after += 1


def doubts_for(prefix: str, instruction: str) -> list[str]:
    found = []
    if len(codeword.words(prefix)) >= MID_SENTENCE_WORDS:
        found.append("mid_sentence")
    if 0 < len(codeword.words(instruction)) < MIN_INSTRUCTION_WORDS:
        found.append("short")
    return found


def payload(pending: Pending, *, command_id: str, session_id: str, started: datetime,
            app: str | None, window_title: str | None, speakers_silent: bool,
            ) -> dict[str, Any]:
    reprompt = pending.reprompt or is_truncated(pending.instruction)
    instruction = "" if reprompt else pending.instruction.strip(" \t\n,.;:!?—-–")
    return {
        "command_id": command_id,
        "kind": "reprompt" if reprompt else "command",
        "instruction": instruction,
        "spoken_at": (started + timedelta(seconds=pending.at)).isoformat(),
        "app": app,
        "window_title": window_title,
        "session_id": session_id,
        "start": round(pending.at, 2),
        "end": round(pending.fragment[-1][1], 2),
        "fragment": [{"start": round(a, 2), "end": round(e, 2), "text": t}
                     for a, e, t in pending.fragment],
        "confidence": confidence(pending.score, speakers_silent=speakers_silent,
                                 closed=pending.closed),
        "guard": "own",
        "doubts": [] if reprompt else pending.doubts,
    }
