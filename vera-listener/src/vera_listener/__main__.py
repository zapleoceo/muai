"""Точка входа: запуск слушателя, самопроверка захвата, шаблон конфига.

Самопроверка нужна не для красоты: аудио-устройства Windows принадлежат
активной сессии, поэтому проверять захват можно только из своей сессии —
из фонового окружения микрофон и системный вывод не слышны в принципе.
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import logging.handlers
import queue
import sys
import threading
import time

from vera_listener.app import Listener
from vera_listener.capture import MIC, SYSTEM, Capture, Frame
from vera_listener.config import ENV_FILE, load_config
from vera_listener.status import Status
from vera_listener.transcriber import Transcriber
from vera_listener.vad import FRAME_S, SpeechDetector
from vera_listener.winctx import active_audio_app, foreground_window_title

TEMPLATE = """VERA_GATEWAY_URL=https://dima.veranda.my
INTERNAL_SECRET=<взять из /var/www/vera3/infra/.env на hetzner-root>
# VERA_STT_DEVICE=NPU          # NPU | GPU | CPU, откатится сам
# VERA_MODEL_ID=OpenVINO/whisper-large-v3-turbo-int8-ov
# VERA_ALLOW_APPS=zoom.exe,telegram.exe,discord.exe
# VERA_DENY_APPS=spotify.exe
"""


def setup() -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if ENV_FILE.exists():
        print(f"{ENV_FILE} уже есть — не трогаю")
        return
    ENV_FILE.write_text(TEMPLATE, encoding="utf-8")
    print(f"Создал {ENV_FILE} — впиши INTERNAL_SECRET и запусти без --setup")


def warmup() -> int:
    """Скачать модель и скомпилировать её под устройство заранее.

    Замер: загрузка 790 МБ плюс первая компиляция под нейропроцессор — 153
    секунды. Дальше берётся из кэша за 1.9с. Без прогрева эту цену платил бы
    первый разговор: звук копится в памяти и не теряется, но выжимка пришла
    бы через минуты, а со стороны выглядело бы как повисание.
    """
    config = load_config()
    print(f"устройство: {config.stt_device}, модель: {config.model_id}")
    transcriber = Transcriber(config)
    started = time.monotonic()
    device = transcriber.warm_up()
    print(f"готово за {time.monotonic() - started:.0f}с, считаем на {device}")
    return 0


def probe(seconds: float) -> int:
    """Слышит ли слушатель обе дорожки прямо сейчас."""
    frames: queue.Queue[Frame] = queue.Queue()
    capture = Capture(frames)
    capture.start()
    detectors = {MIC: SpeechDetector(), SYSTEM: SpeechDetector()}
    counts = {MIC: 0, SYSTEM: 0}
    speech = {MIC: 0.0, SYSTEM: 0.0}

    print(f"Слушаю {seconds:.0f}с. Скажи что-нибудь и включи звук в созвоне или ролике.")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            frame = frames.get(timeout=1.0)
        except queue.Empty:
            continue
        counts[frame.track] += 1
        if detectors[frame.track].is_speech(frame.pcm):
            speech[frame.track] += FRAME_S
    capture.stop()

    print(f"устройство вывода: {capture.device_hint or 'не определилось'}")
    print(f"звучит приложение: {active_audio_app() or 'ничего'}")
    print(f"окно переднего плана: {foreground_window_title() or '—'}")
    ok = True
    for track in (MIC, SYSTEM):
        got = "кадров нет" if not counts[track] else f"кадров {counts[track]}"
        print(f"{track}: {got}, речи {speech[track]:.1f}с")
        ok = ok and counts[track] > 0
    print("захват работает" if ok else "одна из дорожек молчит — см. docs/listener.md")
    return 0 if ok else 1


def _setup_logging(verbose: bool) -> None:
    """Логи по-русски, а консоль Windows по умолчанию cp1252.

    Без явной кодировки первая же строка лога убивает процесс под планировщиком."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")

    config = load_config()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    with contextlib.suppress(OSError):
        config.root.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            config.log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="vera-listener")
    parser.add_argument("--setup", action="store_true", help="создать шаблон конфига")
    parser.add_argument("--probe", type=float, nargs="?", const=10.0, default=None,
                        metavar="СЕК", help="самопроверка захвата")
    parser.add_argument("--warmup", action="store_true",
                        help="скачать и скомпилировать модель заранее")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-tray", action="store_true",
                        help="не показывать иконку в трее")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    if args.setup:
        setup()
        return 0
    if args.warmup:
        return warmup()
    if args.probe is not None:
        return probe(args.probe)

    config = load_config()
    if not config.internal_secret:
        print("INTERNAL_SECRET пуст. Запусти --setup и заполни конфиг.", file=sys.stderr)
        return 1
    config.root.mkdir(parents=True, exist_ok=True)

    status = Status()
    listener = Listener(config, status)
    if args.no_tray:
        listener.run()
        return 0

    # Трей под Windows хочет свой цикл сообщений в ГЛАВНОМ потоке, поэтому
    # слушатель уходит в фоновый. Если трея нет (нет pystray, нет оболочки) —
    # ждём слушателя здесь же: иконка не важнее записи разговоров.
    from vera_listener import tray

    worker = threading.Thread(target=listener.run, name="listener", daemon=True)
    worker.start()
    shown = tray.run(status, log_file=config.log_file, queue_dir=config.queue_dir,
                     on_quit=listener.stop)
    if not shown:
        worker.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
