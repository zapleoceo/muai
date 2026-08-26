"""Иконка в системном трее: видно, что слушатель жив, и чем он сейчас занят.

Без неё слушатель — невидимый процесс: понять, работает он или тихо умер,
можно было только по логу. Цвет иконки отвечает на этот вопрос сразу:

| цвет | что значит |
|---|---|
| серый | тишина, слушает |
| зелёный | идёт разговор, дорожки пишутся |
| жёлтый | сеть недоступна, разговоры копятся в очереди |
| красный | устройства не слышны — чужая сессия Windows или выдернули наушники |

Трей необязателен. Нет pystray или Pillow, нет самого трея (сервер, RDP без
оболочки) — слушатель работает как раньше, просто без иконки: писать в мозг
важнее, чем показывать картинку.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from vera_listener.status import DEAF, IDLE, OFFLINE, TALKING, Status

log = logging.getLogger("listener.tray")

REFRESH_S = 2.0

_COLORS = {
    IDLE: (110, 118, 129),      # серый — жив, тишина
    TALKING: (109, 214, 135),   # зелёный — идёт разговор
    OFFLINE: (255, 200, 100),   # жёлтый — очередь копится
    DEAF: (255, 120, 120),      # красный — не слышит
}


def _icon_image(state: str):
    """Иконка рисуется кодом, а не файлом: один PNG в пакете — это ещё один
    путь, который ломается при упаковке, а нужен всего кружок."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = _COLORS.get(state, _COLORS[IDLE])
    draw.ellipse((6, 6, size - 6, size - 6), fill=(*color, 255))
    # Две «волны» — узнаваемо про звук и отличает от любого другого кружка.
    draw.arc((18, 18, size - 18, size - 18), 300, 60, fill=(15, 17, 21, 255), width=5)
    draw.arc((26, 26, size - 26, size - 26), 300, 60, fill=(15, 17, 21, 255), width=4)
    return img


def write_ico(path) -> None:
    """Иконка приложения из того же кода, что рисует трей.

    Один источник правды: иначе картинка в трее и картинка в Проводнике
    разъезжаются при первой же правке. Серый вариант — «жив, тишина».
    """
    _icon_image(IDLE).save(str(path), format="ICO",
                          sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])


def _human(seconds: float | None) -> str:
    if seconds is None:
        return "ещё ничего"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "только что"
    if minutes < 90:
        return f"{minutes} мин назад"
    return f"{minutes // 60} ч назад"


def tooltip(snap: dict) -> str:
    """Всё главное — в подсказке: трей показывает её на наведении."""
    head = {
        IDLE: "Вера слушает",
        TALKING: "Вера пишет разговор",
        OFFLINE: "Вера слушает (нет сети)",
        DEAF: "Вера не слышит устройства",
    }.get(snap["state"], "Вера слушает")
    lines = [head,
             f"отправлено: {snap['sent']}, в очереди: {snap['queued']}",
             f"последняя отправка: {_human(snap['since_sent_s'])}"]
    if snap["dropped"]:
        lines.append(f"отсеяно как «не разговор»: {snap['dropped']}")
    if snap["last_error"]:
        lines.append(f"последняя ошибка: {snap['last_error'][:80]}")
    return "\n".join(lines)


def _open(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606 — открыть свой лог в блокноте
        else:
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
    except OSError as e:
        log.warning("не открыл %s: %s", path, e)


def run(status: Status, *, log_file: Path, queue_dir: Path,
        on_quit) -> bool:
    """Показать иконку и держать её до выхода. → удалось ли вообще.

    Блокирует поток: pystray под Windows хочет свой цикл сообщений, поэтому в
    `__main__` трей живёт в ГЛАВНОМ потоке, а слушатель — в фоновом.
    """
    try:
        import pystray
    except ImportError:
        log.info("pystray не установлен — работаю без иконки в трее")
        return False

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        log.info("Pillow не установлен — работаю без иконки в трее")
        return False

    icon = pystray.Icon("vera-listener", _icon_image(IDLE), "Вера слушает")

    def _status_text(_item=None) -> str:
        return tooltip(status.snapshot()).split("\n")[1]

    icon.menu = pystray.Menu(
        pystray.MenuItem(_status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Открыть лог", lambda: _open(log_file)),
        pystray.MenuItem("Открыть очередь", lambda: _open(queue_dir)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", lambda: (on_quit(), icon.stop())),
    )

    stop = threading.Event()

    def _refresh() -> None:
        while not stop.wait(REFRESH_S):
            snap = status.snapshot()
            try:
                icon.icon = _icon_image(snap["state"])
                icon.title = tooltip(snap)
            except Exception as e:  # noqa: BLE001 — трей не должен ронять слушателя
                log.debug("иконка не обновилась: %s", e)

    threading.Thread(target=_refresh, name="tray-refresh", daemon=True).start()
    try:
        # Строка в логе — единственный способ узнать снаружи, что иконка
        # действительно поднялась: живой процесс сам по себе этого не значит,
        # без трея слушатель работает точно так же.
        log.info("иконка в трее показана")
        icon.run()
    except Exception as e:  # noqa: BLE001 — трея может не быть вовсе
        log.info("трей недоступен (%s) — работаю без иконки", e)
        return False
    finally:
        stop.set()
    return True
