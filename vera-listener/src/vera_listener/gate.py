"""Решение «этот разговор стоит отправлять». Чистая логика.

Важность события считает триаж на сервере — здесь только грубый отсев, чтобы
не гонять распознавание на кашле, «ага» и фоновом ролике.

Отдельный случай — браузер. Meet и веб-версии мессенджеров живут в chrome.exe,
там же живёт ютуб, и по имени процесса их не различить. Поэтому системный звук
браузера принимается только когда в той же сессии говорил и микрофон: у
двустороннего разговора обе стороны звучат, у ролика — одна.
"""
from __future__ import annotations

from typing import NamedTuple

MIC = "mic"
SYSTEM = "system"
DIALOGUE_MIN_S = 5.0


class Verdict(NamedTuple):
    keep: bool
    reason: str


def system_audio_allowed(app: str | None, *, mic_speech_s: float,
                         allow: tuple[str, ...], browsers: tuple[str, ...],
                         deny: tuple[str, ...]) -> bool:
    if not app:
        return False
    name = app.lower()
    if name in deny:
        return False
    if name in allow:
        return True
    if name in browsers:
        return mic_speech_s >= DIALOGUE_MIN_S
    return False


def judge(speech_s: dict[str, float], *, app: str | None,
          allow: tuple[str, ...], browsers: tuple[str, ...],
          deny: tuple[str, ...], min_speech_s: float,
          monologue_speech_s: float) -> Verdict:
    mic_s = speech_s.get(MIC, 0.0)
    heard_s = speech_s.get(SYSTEM, 0.0)
    allowed = system_audio_allowed(app, mic_speech_s=mic_s, allow=allow,
                                   browsers=browsers, deny=deny)
    system_s = heard_s if allowed else 0.0

    if heard_s > 0.0 and not allowed and mic_s == 0.0:
        return Verdict(False, "media_only")
    if mic_s + system_s < min_speech_s:
        return Verdict(False, "too_short")
    if mic_s >= DIALOGUE_MIN_S and system_s >= DIALOGUE_MIN_S:
        return Verdict(True, "dialogue")
    if mic_s >= monologue_speech_s:
        return Verdict(True, "monologue")
    if system_s >= monologue_speech_s and mic_s > 0.0:
        return Verdict(True, "call_one_sided")
    return Verdict(False, "not_a_conversation")
