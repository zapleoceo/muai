"""Копилка системного звука: потолок памяти и честный счёт забытого."""
from __future__ import annotations

from vera_listener.hold import BYTES_PER_S, Hold


def _pcm(seconds: float) -> bytes:
    return b"\x00" * int(seconds * BYTES_PER_S)


def test_empty_hold_is_falsy():
    hold = Hold(max_bytes=BYTES_PER_S)
    assert not hold
    assert hold.take() == []
    assert hold.seconds == 0.0


def test_keeps_chunks_in_order():
    hold = Hold(max_bytes=10 * BYTES_PER_S)
    hold.add(0.0, _pcm(1))
    hold.add(5.0, _pcm(2))
    assert hold
    assert hold.seconds == 3.0
    assert [offset for offset, _pcm_ in hold.take()] == [0.0, 5.0]


def test_take_empties_the_hold():
    hold = Hold(max_bytes=10 * BYTES_PER_S)
    hold.add(0.0, _pcm(1))
    hold.take()
    assert not hold
    assert hold.seconds == 0.0


def test_empty_pcm_is_not_stored():
    hold = Hold(max_bytes=BYTES_PER_S)
    hold.add(1.0, b"")
    assert not hold


def test_cap_forgets_the_oldest():
    hold = Hold(max_bytes=3 * BYTES_PER_S)
    for i in range(5):
        hold.add(float(i), _pcm(1))
    kept = hold.take()
    assert [offset for offset, _pcm_ in kept] == [2.0, 3.0, 4.0]


def test_forgotten_seconds_are_counted():
    """Молчаливая потеря хуже честной: сколько забыли — видно в логе события."""
    hold = Hold(max_bytes=2 * BYTES_PER_S)
    for i in range(5):
        hold.add(float(i), _pcm(1))
    assert hold.dropped_s == 3.0


def test_take_does_not_reset_the_loss_counter():
    """Счётчик про сессию, а не про выемку: закрытие обязано о потере узнать."""
    hold = Hold(max_bytes=BYTES_PER_S)
    hold.add(0.0, _pcm(1))
    hold.add(1.0, _pcm(1))
    hold.take()
    assert hold.dropped_s == 1.0


def test_clear_resets_everything():
    hold = Hold(max_bytes=BYTES_PER_S)
    hold.add(0.0, _pcm(1))
    hold.add(1.0, _pcm(1))
    hold.clear()
    assert not hold and hold.dropped_s == 0.0


def test_single_chunk_larger_than_the_cap_survives():
    """Рвать один кусок посередине нельзя — лучше один раз превысить потолок."""
    hold = Hold(max_bytes=BYTES_PER_S)
    hold.add(0.0, _pcm(5))
    assert hold.seconds == 5.0
    assert hold.dropped_s == 0.0
