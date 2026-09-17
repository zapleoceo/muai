"""Очередь на диске: дозапись, закрытие, восстановление после падения."""
from __future__ import annotations

import os
import time

from vera_listener.outbox import Outbox, read_payload


def _outbox(tmp_path) -> Outbox:
    return Outbox(tmp_path / "queue")


def test_session_roundtrip(tmp_path):
    box = _outbox(tmp_path)
    path = box.start("s1", "2026-08-25T10:00:00", app="zoom.exe",
                     window_title="Коля — Zoom", device_hint="наушники")
    box.append(path, 1.0, "mic", "привет")
    box.append(path, 2.5, "system", "привет, слышу")
    ready = box.finish(path, "2026-08-25T10:05:00")

    assert box.ready() == [ready]
    payload = read_payload(ready)
    assert payload["started_at"] == "2026-08-25T10:00:00"
    assert payload["ended_at"] == "2026-08-25T10:05:00"
    assert payload["app"] == "zoom.exe"
    assert [u["stream"] for u in payload["utterances"]] == ["mic", "system"]


def test_empty_utterances_are_not_written(tmp_path):
    box = _outbox(tmp_path)
    path = box.start("s2", "2026-08-25T10:00:00", app=None,
                     window_title=None, device_hint=None)
    box.append(path, 1.0, "mic", "   ")
    assert read_payload(path) is None


def test_crashed_session_is_recovered_with_derived_end(tmp_path):
    box = _outbox(tmp_path)
    path = box.start("s3", "2026-08-25T10:00:00", app="zoom.exe",
                     window_title=None, device_hint=None)
    box.append(path, 42.0, "mic", "успели записать")
    # Футера нет — процесс упал. Файл старый, значит сессия не живая.
    import os
    old = path.stat().st_mtime - 7200
    os.utime(path, (old, old))

    # Подбирает НОВЫЙ процесс: своей эта сессия для него не является.
    moved = _outbox(tmp_path).recover(max_age_s=3600.0)
    assert len(moved) == 1
    payload = read_payload(moved[0])
    assert payload["ended_at"] == "2026-08-25T10:00:42"


def test_live_session_is_not_recovered(tmp_path):
    box = _outbox(tmp_path)
    path = box.start("s4", "2026-08-25T10:00:00", app=None,
                     window_title=None, device_hint=None)
    box.append(path, 1.0, "mic", "идёт прямо сейчас")
    assert box.recover(max_age_s=3600.0) == []
    assert path.exists()


def test_torn_last_line_does_not_lose_the_rest(tmp_path):
    box = _outbox(tmp_path)
    path = box.start("s5", "2026-08-25T10:00:00", app=None,
                     window_title=None, device_hint=None)
    box.append(path, 1.0, "mic", "первая")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "u", "at": 2.0, "st')

    payload = read_payload(path)
    assert len(payload["utterances"]) == 1


def test_parked_file_leaves_the_queue(tmp_path):
    box = _outbox(tmp_path)
    path = box.start("s6", "2026-08-25T10:00:00", app=None,
                     window_title=None, device_hint=None)
    box.append(path, 1.0, "mic", "текст")
    ready = box.finish(path, "2026-08-25T10:01:00")
    box.park(ready, "тест")
    assert box.ready() == []
    assert (box.failed_dir / "s6.jsonl").exists()


def test_finish_can_replace_utterances_after_echo_cleanup(tmp_path):
    box = Outbox(tmp_path / "queue")
    path = box.start("s7", "2026-08-25T10:00:00", app="zoom.exe",
                     window_title=None, device_hint=None)
    box.append(path, 1.0, "system", "давай в четверг")
    box.append(path, 1.2, "mic", "давай в четверг")
    ready = box.finish(path, "2026-08-25T10:01:00",
                       utterances=[{"at": 1.0, "stream": "system",
                                    "text": "давай в четверг"}])
    payload = read_payload(ready)
    assert len(payload["utterances"]) == 1
    assert payload["utterances"][0]["stream"] == "system"
    assert payload["app"] == "zoom.exe"

def test_stale_open_files_do_not_pile_up(tmp_path):
    """recover зовётся только при старте, и тогда живых файлов в open/ нет.

    Порог в час оставлял мусор навсегда: 2026-08-27 в open/ лежало пять
    брошенных сессий возрастом от восьми минут — ни одна не подбиралась.
    """
    box = Outbox(tmp_path)
    path = box.start("s-1", "2026-08-27T18:16:08+07:00", app="chrome.exe",
                     window_title="ролик", device_hint=None)
    box.append(path, 1.0, "system", "что-то сказали")
    os.utime(path, (time.time() - 300, time.time() - 300))

    moved = Outbox(tmp_path).recover()
    assert [p.name for p in moved] == ["s-1.jsonl"]
    assert list(box.open_dir.glob("*.jsonl")) == []


def test_empty_stale_file_is_deleted_not_queued(tmp_path):
    """Пустышка без реплик — мусор: слать нечего, держать незачем."""
    box = Outbox(tmp_path)
    path = box.start("s-2", "2026-08-27T18:23:34+07:00", app=None,
                     window_title=None, device_hint=None)
    os.utime(path, (time.time() - 300, time.time() - 300))

    fresh = Outbox(tmp_path)          # подбирает новый процесс
    assert fresh.recover() == []
    assert not path.exists()
    assert list(fresh.ready_dir.glob("*.jsonl")) == []


class TestRestartInTheMiddleOfATalk:
    """Перезапуск посреди разговора не должен оставлять запись в open/.

    17.09 вживую: слушатель перезапустили на 37-й минуте созвона, файл писался
    до последней секунды и при старте оказался моложе минуты — порог от гонки с
    умирающим процессом его пропустил. Второго захода не было, и 1281 реплика
    лежала в open/ неотправленной. Подбирать пришлось руками.
    """

    def test_fresh_orphan_is_skipped_at_first_but_picked_up_later(self, tmp_path):
        box = Outbox(tmp_path)
        path = box.start("s-1", "2026-09-17T18:56:40+07:00", app="chrome.exe",
                         window_title="Meet", device_hint=None)
        box.append(path, 1.0, "system", "разговор шёл прямо до перезапуска")

        # Новый процесс поднялся сразу после падения: файл свежий, трогать рано.
        fresh = Outbox(tmp_path)
        assert fresh.recover() == []
        assert path.exists()

        # Минутой позже — уже некому его писать, и он обязан уехать.
        os.utime(path, (time.time() - 120, time.time() - 120))
        assert [p.name for p in fresh.recover()] == ["s-1.jsonl"]
        assert list(fresh.open_dir.glob("*.jsonl")) == []

    def test_own_open_session_is_never_taken(self, tmp_path):
        """Защита от обратной беды: в долгом разговоре бывают паузы длиннее
        минуты, и подбор из цикла отправки утащил бы файл из-под записи."""
        box = Outbox(tmp_path)
        mine = box.start("s-live", "2026-09-17T19:33:57+07:00", app="chrome.exe",
                         window_title="Meet", device_hint=None)
        box.append(mine, 1.0, "system", "говорю прямо сейчас")
        os.utime(mine, (time.time() - 600, time.time() - 600))

        assert box.recover() == []
        assert mine.exists(), "живую сессию забирать нельзя ни при каком возрасте"

    def test_session_is_protected_until_it_is_actually_closed(self, tmp_path):
        """Владение считает САМА очередь, а не состояние вызывающего.

        Первая версия защиты спрашивала у слушателя «какая сессия сейчас
        твоя» — и этого не хватило: в `_finish` сессия обнуляется сразу, а
        задача на закрытие висит в очереди потока распознавания минутами, пока
        разбираются накопленные куски. В это окно защиты не было.

        Вживую 17.09: на 118-минутном созвоне подбор забрал живой файл за 43
        секунды до закрытия. Запись ушла сырой — 0 пометок эха и 0 имён из 3787
        реплик, а `_finish` уже не нашёл файла.
        """
        box = Outbox(tmp_path)
        path = box.start("s-long", "2026-09-17T19:36:46+07:00", app="chrome.exe",
                         window_title="Meet – Демо", device_hint=None)
        box.append(path, 1.0, "system", "длинный созвон")
        # Разговор кончился, вызывающий уже забыл про сессию — но файл ещё не
        # закрыт: задача стоит в очереди. Возраст любой.
        os.utime(path, (time.time() - 3600, time.time() - 3600))

        assert box.recover() == [], "файл ещё наш, пока не закрыт"
        assert path.exists()

        # Закрыли по-настоящему — только теперь он перестаёт быть нашим.
        box.finish(path, "2026-09-17T21:36:00+07:00")
        assert not path.exists()

    def test_parked_session_stops_being_ours(self, tmp_path):
        """Отложенная в failed сессия тоже перестаёт быть нашей — иначе набор
        рос бы вечно на долгоживущем процессе."""
        box = Outbox(tmp_path)
        path = box.start("s-bad", "2026-09-17T10:00:00+07:00", app=None,
                         window_title=None, device_hint=None)
        box.append(path, 1.0, "mic", "кривая сессия")
        box.park(path, "4xx")
        assert path not in box._mine

    def test_other_orphans_still_move_while_mine_stays(self, tmp_path):
        """Чужая брошенная сессия уезжает, своя незакрытая остаётся."""
        dead = Outbox(tmp_path)
        orphan = dead.start("s-old", "2026-09-17T18:56:40+07:00", app=None,
                            window_title=None, device_hint=None)
        dead.append(orphan, 1.0, "mic", "брошенная сессия")

        box = Outbox(tmp_path)
        mine = box.start("s-live", "2026-09-17T19:33:57+07:00", app=None,
                         window_title=None, device_hint=None)
        box.append(mine, 1.0, "mic", "моя сессия")
        for path in (mine, orphan):
            os.utime(path, (time.time() - 600, time.time() - 600))

        moved = box.recover()

        assert [p.name for p in moved] == ["s-old.jsonl"]
        assert mine.exists()
