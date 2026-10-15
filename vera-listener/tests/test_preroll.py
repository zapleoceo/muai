"""Pre-roll системной дорожки: фраза, открывшая сессию, проверяется по-честному.

Кадры system до открытия сессии слежение раньше не видело, и фраза на 0.0 с
уходила с сомнением «blind». Закрепляем: хвост, проигранный в новое
слежение, делает её голосом владельца — но только там, где он действительно
покрывает окно; эхо в хвосте отбрасывается, пустота остаётся слепотой.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime

from vera_listener.app import Listener
from vera_listener.capture import MIC
from vera_listener.command_guard import VAD_SLACK_S
from vera_listener.commands import CommandWatch
from vera_listener.config import Config
from vera_listener.preroll import MAX_FRAMES, PREROLL_S, PreRoll

T0 = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
PHRASE = "Вера, мне нужна помощь, срочно напиши мне что-то в телеграм"
FRAME = 0.032
#: Монотонные часы захвата в момент открытия сессии.
ZERO = 1000.0


def _preroll(since: float, *, until: float = ZERO, speech=()) -> PreRoll:
    """Кадры system в абсолютном времени от `since` до ZERO."""
    roll = PreRoll()
    t = since
    while t < until - 1e-9:
        roll.observe(t, FRAME, any(a <= t < b for a, b in speech))
        t += FRAME
    return roll


def _session(roll: PreRoll | None, *, system_from: float = 0.0) -> tuple[CommandWatch, list]:
    sent: list[dict] = []
    watch = CommandWatch("s-1", T0, sent.append, app="zoom.exe", window_title="Созвон")
    if roll is not None:
        roll.replay(lambda at, d, s: watch.hear("system", at, d, s), ZERO)
    t = 0.0
    while t < 20.0:
        watch.hear("mic", t, FRAME, False)
        if t >= system_from:
            watch.hear("system", t, FRAME, False)
        t += FRAME
    return watch, sent


def _say_at_start(watch: CommandWatch) -> None:
    watch.on_segment("mic", 0.0, 4.0, PHRASE)
    watch.chunk_done("mic", 6.0)


class TestPreRollGuard:
    def test_phrase_at_session_start_with_live_system_is_own(self):
        watch, sent = _session(_preroll(ZERO - PREROLL_S))
        _say_at_start(watch)
        [command] = sent
        assert command["guard"] == "own"
        assert "blind" not in command["doubts"]
        assert command["confidence"] == 1.0

    def test_same_phrase_without_preroll_was_blind(self):
        watch, sent = _session(None, system_from=0.01)
        _say_at_start(watch)
        watch.close()
        [command] = sent
        assert command["guard"] == "blind" and "blind" in command["doubts"]

    def test_echo_before_first_observed_system_frame_is_not_own(self):
        """Динамики отыграли эхо, когда loopback ещё не писался: хвоста нет,
        system появился после начала фразы — доказать нечем."""
        watch, sent = _session(PreRoll(), system_from=1.0)
        _say_at_start(watch)
        watch.close()
        assert sent and all(c["guard"] != "own" for c in sent)
        assert all("blind" in c["doubts"] for c in sent)

    def test_gap_between_preroll_and_session_stays_blind(self):
        """Устройство отваливалось: между хвостом и сессией дыра в 3 с внутри
        окна guard — слепота, не OWN."""
        watch, sent = _session(_preroll(ZERO - PREROLL_S, until=ZERO - 3.0))
        watch.on_segment("mic", 0.0, 4.0, PHRASE)
        watch.close()
        assert sent and all(c["guard"] != "own" for c in sent)

    def test_echo_in_preroll_is_dropped(self):
        roll = _preroll(ZERO - PREROLL_S, speech=[(ZERO - 3.0, ZERO + 1.0)])
        watch, sent = _session(roll)
        _say_at_start(watch)
        assert sent == []
        watch.close()
        assert sent == []

    def test_no_system_frames_at_all_is_blind(self):
        sent: list[dict] = []
        watch = CommandWatch("s-1", T0, sent.append)
        PreRoll().replay(lambda at, d, s: watch.hear("system", at, d, s), ZERO)
        t = 0.0
        while t < 20.0:
            watch.hear("mic", t, FRAME, False)
            t += FRAME
        _say_at_start(watch)
        watch.close()
        [command] = sent
        assert command["guard"] == "blind" and "blind" in command["doubts"]


class TestPreRollMemory:
    def test_keeps_only_last_preroll_seconds(self):
        roll = PreRoll()
        for i in range(100_000):
            roll.observe(i * FRAME, FRAME, i % 2 == 0)
        assert len(roll) <= math.ceil(PREROLL_S / FRAME) + 2
        seen: list[float] = []
        roll.replay(lambda at, d, s: seen.append(at), 0.0)
        assert seen[-1] - seen[0] <= PREROLL_S

    def test_hard_cap_on_frame_count(self):
        roll = PreRoll()
        for i in range(10_000):
            roll.observe(5.0, 0.0, False)
        assert len(roll) == MAX_FRAMES

    def test_covers_guard_lookback(self):
        assert PREROLL_S > VAD_SLACK_S

    def test_replay_shifts_to_session_time(self):
        roll = PreRoll()
        roll.observe(ZERO - 2.0, FRAME, True)
        seen: list[tuple[float, float, bool]] = []
        roll.replay(lambda at, d, s: seen.append((at, d, s)), ZERO)
        assert seen == [(-2.0, FRAME, True)]


def test_listener_replays_preroll_into_new_session(tmp_path):
    """Сборка: кадры system до открытия сессии попадают в её слежение."""
    listener = Listener(replace(Config(root=tmp_path, internal_secret="x"),
                                voice_commands=True))
    for i in range(100):
        listener._preroll.observe(ZERO - 4.0 + i * 0.04, 0.04, False)
    listener.segmenter.feed(ZERO, MIC, True, app="zoom.exe", window_title="окно")
    listener._ensure_open()
    watch = listener._watches[listener.session]
    assert abs(watch.system._frames.first - (-4.0)) < 1e-6
