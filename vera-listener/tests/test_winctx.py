"""winctx не имеет права уронить процесс из-за pycaw.

Импорт pycaw тянет comtypes, а тот инициализирует COM и падает с OSError,
если поток уже в другом режиме — так умирал собранный exe. Тесты идут и на
Linux в CI: сам pycaw там не устанавливается, что и есть один из случаев.
"""
from __future__ import annotations

import builtins
import sys

from vera_listener import winctx


def _break_import(monkeypatch, error: Exception) -> None:
    for name in list(sys.modules):
        if name.startswith("pycaw"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    real_import = builtins.__import__

    def fake(name, *args, **kwargs):
        if name.startswith("pycaw"):
            raise error
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_com_error_on_import_is_not_fatal(monkeypatch):
    _break_import(monkeypatch, OSError("Cannot change thread mode after it is set"))
    assert winctx.active_audio_app() is None


def test_missing_pycaw_is_not_fatal(monkeypatch):
    _break_import(monkeypatch, ImportError("No module named 'pycaw'"))
    assert winctx.active_audio_app() is None


def test_asks_for_the_same_com_mode_as_capture(monkeypatch):
    """MTA — тот режим, в котором захват звука уже оставил поток."""
    _break_import(monkeypatch, ImportError("нет пакета"))
    monkeypatch.setattr(sys, "coinit_flags", 2, raising=False)
    winctx.active_audio_app()
    assert sys.coinit_flags == 0
