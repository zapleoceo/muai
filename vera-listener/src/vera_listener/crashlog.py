"""Падения слушателя должны оставлять след.

Процесс под планировщиком умирал молча: нативный сбой нейропроцессора
(«bad allocation») убивает интерпретатор мимо logging, а исключение в
фоновом потоке без хука печатается в stderr, которого у exe без консоли нет.
"""
from __future__ import annotations

import atexit
import faulthandler
import logging
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO

KEEP_DUMPS = 10
RUNNING = "running.marker"
CRASHED = "crashed.marker"

log = logging.getLogger("listener.crash")


class CrashLog:
    def __init__(self, crash_dir: Path, keep: int = KEEP_DUMPS) -> None:
        self.dir = crash_dir
        self.keep = keep
        self.dump_path: Path | None = None
        self._dump: IO[str] | None = None

    def install(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.report_previous()
        self._prune()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        self.dump_path = self.dir / f"faulthandler-{stamp}.log"
        self._dump = self.dump_path.open("w", encoding="utf-8")
        faulthandler.enable(file=self._dump, all_threads=True)
        (self.dir / RUNNING).write_text(stamp, encoding="utf-8")
        sys.excepthook = self._excepthook
        threading.excepthook = self._thread_excepthook
        atexit.register(self.close)

    def report_previous(self) -> str | None:
        reasons: list[str] = []
        crashed = self.dir / CRASHED
        if crashed.exists():
            reasons.append(crashed.read_text(encoding="utf-8").strip())
            crashed.unlink()
        dumps = [p for p in self._dumps() if p.stat().st_size > 0]
        if dumps:
            reasons.append(f"нативный сбой, дамп {dumps[-1]}")
        running = self.dir / RUNNING
        if running.exists():
            if not reasons:
                reasons.append("процесс завершился без штатного выхода")
            running.unlink()
        if not reasons:
            return None
        summary = "; ".join(reasons)
        log.error("previous run crashed: %s", summary)
        return summary

    def record(self, exc_type: type[BaseException], where: str) -> None:
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        line = f"{stamp} {exc_type.__name__} в {where}"
        try:
            (self.dir / CRASHED).write_text(line, encoding="utf-8")
        except OSError:
            log.exception("не удалось записать маркер падения")

    def close(self) -> None:
        if self._dump is None:
            return
        faulthandler.disable()
        self._dump.close()
        self._dump = None
        if self.dump_path is not None and self.dump_path.stat().st_size == 0:
            self.dump_path.unlink()
        (self.dir / RUNNING).unlink(missing_ok=True)

    def _excepthook(self, exc_type: type[BaseException], exc: BaseException,
                    tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.error("необработанное исключение", exc_info=(exc_type, exc, tb))
        self.record(exc_type, "главном потоке")

    def _thread_excepthook(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.error("необработанное исключение в потоке %s", name,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))  # type: ignore[arg-type]
        self.record(args.exc_type, f"потоке {name}")

    def _dumps(self) -> list[Path]:
        return sorted(self.dir.glob("faulthandler-*.log"))

    def _prune(self) -> None:
        dumps = self._dumps()
        for old in dumps[:max(0, len(dumps) - (self.keep - 1))]:
            old.unlink(missing_ok=True)
