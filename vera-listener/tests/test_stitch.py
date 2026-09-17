"""Склейка обрывков: whisper режет предложение по запятым, мы сшиваем обратно.

Регрессия, ради которой всё затевалось, — `test_comma_split_sentence_...`:
до склейки одна фраза приходила тремя «репликами», каждая короче четырёх слов,
и ни одна вырезка звука не дотягивала до трёх секунд, нужных отпечатку голоса.

Второе, что здесь стережётся, — риск обратного знака: склеить двух разных
людей в один отпечаток хуже, чем оставить обрывки. Поэтому пауза и знак конца
проверяются тестами отдельно, а цепочка ограничена по длине.
"""
from __future__ import annotations

from vera_listener.stitch import (
    MERGE_MAX_S,
    Segment,
    looks_unfinished,
    merge_continuations,
)
from vera_listener.transcriber import segments_of


class _Chunk:
    def __init__(self, start_ts, text, end_ts=0.0):
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.text = text


class _Result:
    def __init__(self, text="", chunks=None):
        self._text = text
        if chunks is not None:
            self.chunks = chunks

    def __str__(self):
        return self._text


class TestRegression:
    """Живой пример из расшифровки созвона 17.09."""

    def test_comma_split_sentence_becomes_one_utterance(self):
        got = segments_of(_Result(chunks=[
            _Chunk(0.0, " То есть я на такой тип вопроса,", end_ts=1.4),
            _Chunk(1.4, " я просто думаю,", end_ts=2.3),
            _Chunk(2.3, " что надо не выводить диаграмму.", end_ts=4.1),
        ]), duration_s=5.0)
        assert [s.text for s in got] == [
            "То есть я на такой тип вопроса, я просто думаю, "
            "что надо не выводить диаграмму."]

    def test_merged_span_is_long_enough_for_a_voiceprint(self):
        """Три обрывка по 1.4с отпечатка не дают, склеенная реплика — даёт."""
        from vera_listener.speakers.features import MIN_VOICEPRINT_S

        got = segments_of(_Result(chunks=[
            _Chunk(0.0, " То есть я на такой тип вопроса,", end_ts=1.4),
            _Chunk(1.4, " я просто думаю,", end_ts=2.3),
            _Chunk(2.3, " что надо не выводить диаграмму.", end_ts=4.1),
        ]), duration_s=5.0)
        assert len(got) == 1
        assert got[0].duration >= MIN_VOICEPRINT_S


class TestWhenWeDoNotMerge:
    def test_finished_sentence_stays_on_its_own(self):
        got = merge_continuations([
            Segment(at=0.0, end=2.0, text="Давай сверим сроки."),
            Segment(at=2.0, end=4.0, text="Даша обещала отчёт."),
        ])
        assert [s.text for s in got] == ["Давай сверим сроки.", "Даша обещала отчёт."]

    def test_pause_longer_than_the_gap_stops_the_merge(self):
        """Пауза — признак смены говорящего; порог нарочно почти нулевой."""
        got = merge_continuations([
            Segment(at=0.0, end=2.0, text="я думаю,"),
            Segment(at=3.5, end=5.0, text="нет, давай иначе"),
        ])
        assert len(got) == 2

    def test_question_and_exclamation_close_a_sentence(self):
        got = merge_continuations([
            Segment(at=0.0, end=1.0, text="Ты слышишь?"),
            Segment(at=1.0, end=2.0, text="да, слышу"),
        ])
        assert len(got) == 2

    def test_closing_quote_after_the_dot_still_closes(self):
        got = merge_continuations([
            Segment(at=0.0, end=1.0, text='он сказал «потом».'),
            Segment(at=1.0, end=2.0, text="а мы сделали сразу"),
        ])
        assert len(got) == 2

    def test_chain_stops_at_the_length_cap(self):
        """Длинная цепочка — накопленный шанс прихватить чужой голос."""
        pieces = [Segment(at=float(i), end=float(i + 1), text="и дальше,")
                  for i in range(int(MERGE_MAX_S) + 5)]
        got = merge_continuations(pieces)
        assert len(got) > 1
        assert all(s.duration <= MERGE_MAX_S for s in got)


class TestMerging:
    def test_boundaries_cover_both_pieces(self):
        got = merge_continuations([
            Segment(at=1.0, end=2.0, text="первая часть,"),
            Segment(at=2.1, end=4.0, text="вторая часть."),
        ])
        assert (got[0].at, got[0].end) == (1.0, 4.0)

    def test_pieces_are_joined_with_a_single_space(self):
        got = merge_continuations([
            Segment(at=0.0, end=1.0, text="раз, "),
            Segment(at=1.0, end=2.0, text=" два"),
        ])
        assert got[0].text == "раз, два"

    def test_empty_input(self):
        assert merge_continuations([]) == []


class TestLooksUnfinished:
    def test_comma_is_unfinished(self):
        assert looks_unfinished("я думаю,") is True

    def test_no_punctuation_at_all_is_unfinished(self):
        assert looks_unfinished("ну да ладно") is True

    def test_dot_is_finished(self):
        assert looks_unfinished("Готово.") is False

    def test_ellipsis_is_finished(self):
        assert looks_unfinished("ну как сказать…") is False
