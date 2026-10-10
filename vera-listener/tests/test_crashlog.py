from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

import pytest

from vera_listener.crashlog import CRASHED, RUNNING, CrashLog


@pytest.fixture
def crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CrashLog:
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    return CrashLog(tmp_path / "crash", keep=3)


def test_clean_run_leaves_nothing(crash: CrashLog) -> None:
    crash.install()
    assert (crash.dir / RUNNING).exists()
    crash.close()
    assert list(crash.dir.iterdir()) == []


def test_unclean_exit_reported_on_next_start(crash: CrashLog,
                                             caplog: pytest.LogCaptureFixture) -> None:
    crash.dir.mkdir(parents=True)
    (crash.dir / RUNNING).write_text("x", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        summary = crash.report_previous()
    assert summary == "процесс завершился без штатного выхода"
    assert "previous run crashed" in caplog.text
    assert not (crash.dir / RUNNING).exists()


def test_nonempty_dump_reported(crash: CrashLog) -> None:
    crash.dir.mkdir(parents=True)
    (crash.dir / "faulthandler-20261010T000000Z.log").write_text(
        "Fatal Python error", encoding="utf-8")
    assert "нативный сбой" in (crash.report_previous() or "")


def test_thread_exception_logged_and_marked(crash: CrashLog,
                                            caplog: pytest.LogCaptureFixture) -> None:
    crash.install()
    try:
        def boom() -> None:
            raise MemoryError("bad allocation")

        with caplog.at_level(logging.ERROR):
            worker = threading.Thread(target=boom, name="stt")
            worker.start()
            worker.join()
        assert "потоке stt" in caplog.text
        assert "bad allocation" in caplog.text
        assert "MemoryError в потоке stt" in (crash.dir / CRASHED).read_text(encoding="utf-8")
    finally:
        crash.close()
    assert "MemoryError" in (crash.report_previous() or "")
    assert not (crash.dir / CRASHED).exists()


def test_main_thread_exception_marked(crash: CrashLog,
                                      caplog: pytest.LogCaptureFixture) -> None:
    crash.install()
    try:
        with caplog.at_level(logging.ERROR):
            sys.excepthook(ValueError, ValueError("x"), None)
        assert "необработанное исключение" in caplog.text
        assert (crash.dir / CRASHED).exists()
    finally:
        crash.close()


def test_dumps_bounded(crash: CrashLog) -> None:
    crash.dir.mkdir(parents=True)
    for i in range(6):
        (crash.dir / f"faulthandler-2026100{i}T000000Z.log").write_text("x", encoding="utf-8")
    crash.install()
    try:
        assert len(list(crash.dir.glob("faulthandler-*.log"))) == 3
        assert not (crash.dir / "faulthandler-20261000T000000Z.log").exists()
    finally:
        crash.close()
