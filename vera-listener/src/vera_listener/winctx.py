"""Кто сейчас звучит и что на экране. Только Windows, только чтение.

Приложение определяется по активной аудио-сессии, а не по окну переднего
плана: во время созвона alt-tab происходит постоянно, а звучит по-прежнему
Zoom. Заголовок окна берётся отдельно — в нём часто стоит имя собеседника.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys

log = logging.getLogger("listener.winctx")

# Свои и системные процессы за разговор не считаем.
IGNORED = {"audiodg.exe", "rtkuwp.exe", "explorer.exe", "python.exe",
           "pythonw.exe", "veralistener.exe", "svchost.exe",
           "shellexperiencehost.exe"}
_SELF = os.getpid()


def foreground_window_title() -> str | None:
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return None
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value or None
    except (AttributeError, OSError) as e:
        log.debug("заголовок окна недоступен: %s", e)
        return None


def active_audio_app() -> str | None:
    """Имя процесса, который прямо сейчас играет звук."""
    # comtypes при импорте инициализирует COM в том режиме, который прочитает
    # из sys.coinit_flags, и падает, если поток уже в другом. Захват звука
    # (soundcard → Media Foundation) ставит потоку MTA раньше, поэтому просим
    # тот же режим. Без этого в собранном exe импорт pycaw валит процесс с
    # «Cannot change thread mode after it is set».
    sys.coinit_flags = 0
    try:
        from pycaw.utils import AudioUtilities
    except (ImportError, OSError) as e:
        log.debug("pycaw недоступен: %s", e)
        return None
    try:
        for session in AudioUtilities.GetAllSessions():
            process = session.Process
            if process is None or session.State != 1 or process.pid == _SELF:
                continue
            name = process.name().lower()
            if name in IGNORED:
                continue
            return name
    except Exception as e:
        log.debug("аудио-сессии недоступны: %s", e)
    return None
