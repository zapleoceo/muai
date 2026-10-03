"""Подменяет Windows-only библиотеки заглушками до импорта тестов.

`capture.py` импортирует `soundcard` на уровне модуля, а на Linux (CI) он не
импортируется без pulseaudio. Тестам нужна только поверхность, достаточная для
импорта: сами тесты подставляют своё устройство через `monkeypatch`. Живой звук
здесь не проверяется — такие проверки идут вручную на ноутбуке.
"""
from __future__ import annotations

import importlib
import sys
import types


def _soundcard_importable() -> bool:
    try:
        importlib.import_module("soundcard")
    except (ImportError, OSError):
        for name in [m for m in sys.modules if m == "soundcard" or m.startswith("soundcard.")]:
            del sys.modules[name]
        return False
    return True


if not _soundcard_importable():
    _fake = types.ModuleType("soundcard")
    _fake.mediafoundation = types.ModuleType("soundcard.mediafoundation")
    sys.modules["soundcard"] = _fake
    sys.modules["soundcard.mediafoundation"] = _fake.mediafoundation
