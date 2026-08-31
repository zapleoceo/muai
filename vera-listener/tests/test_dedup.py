"""Эхо из динамиков: реплика собеседника не должна попасть дважды."""
from __future__ import annotations

from vera_listener.dedup import (
    CONTAINED_MIN_CHARS,
    drop_echo,
    looks_like_echo,
    mark_echo,
    normalize,
    similar,
)


def _u(at: float, stream: str, text: str) -> dict:
    return {"at": at, "stream": stream, "text": text}


def test_mic_echo_of_system_line_is_removed():
    utterances = [
        _u(10.0, "system", "давай перенесём встречу на четверг"),
        _u(10.4, "mic", "Давай перенесём встречу на четверг!"),
        _u(12.0, "mic", "хорошо, четверг подходит"),
    ]
    kept = drop_echo(utterances)
    assert [u["stream"] for u in kept] == ["system", "mic"]
    assert kept[1]["text"] == "хорошо, четверг подходит"


def test_same_words_far_apart_are_not_echo():
    utterances = [
        _u(0.0, "system", "до понедельника"),
        _u(300.0, "mic", "до понедельника"),
    ]
    assert len(drop_echo(utterances)) == 2


def test_system_lines_are_never_dropped():
    utterances = [_u(1.0, "system", "алло"), _u(1.1, "system", "алло")]
    assert len(drop_echo(utterances)) == 2


def test_no_system_track_means_nothing_to_dedupe():
    utterances = [_u(1.0, "mic", "привет"), _u(1.2, "mic", "привет")]
    assert drop_echo(utterances) == utterances


def test_similarity_ignores_case_and_punctuation():
    assert similar("Привет, Коля!", "привет коля") > 0.95
    assert similar("привет", "") == 0.0


class TestCaughtWithContinuation:
    """Микрофон ловит фразу вместе с продолжением — главный промах прежнего фильтра.

    Замер на записи от 31.08: из 132 реплик микрофона прежний фильтр не
    выбросил НИ ОДНОЙ, потому что сравнивал строки целиком. Когда микрофон
    захватил фразу собеседника плюс ещё полтора предложения, доля совпадения
    падает до ~0.6 и до порога 0.75 не дотягивает.
    """

    def test_mic_line_containing_the_system_line_is_echo(self):
        utterances = [
            _u(306.7, "system", "И дальше мои студенты должны иметь возможность"),
            _u(307.5, "mic", "И дальше мои студенты должны иметь возможность "
                             "где-то изучать этот курс на какой-то площадке"),
        ]
        kept = drop_echo(utterances)
        assert [u["stream"] for u in kept] == ["system"]

    def test_offset_beyond_the_old_window_is_still_echo(self):
        """3.7 с между дорожками — реальная пара из записи, старое окно 2.5 с."""
        utterances = [
            _u(767.7, "system", "ни деньги, ни время своего персонала,"),
            _u(764.0, "mic", "на этом не тратила ни деньги ни время своего персонала"),
        ]
        assert len(drop_echo(utterances)) == 1


class TestKnownResidual:
    """Что фильтр НЕ ловит — записано намеренно, чтобы предел был виден.

    Пересказ своими словами текстом не отличить от собственной реплики: обе
    стороны говорят одно и то же разными словами, вхождения нет, сходство
    ниже порога. На записи от 31.08 таких осталось около десятка. Лечится не
    подкруткой порога (он тогда начнёт съедать слова владельца), а опознанием
    ГОЛОСА — это отдельная задача.
    """

    def test_paraphrase_survives_and_that_is_expected(self):
        utterances = [
            _u(338.6, "system", "Я как админ, смотри, у меня должно быть два пути"),
            _u(335.1, "mic", "да я как админ у меня должно быть два пути "
                             "мои студенты которые изучают курс"),
        ]
        assert len(drop_echo(utterances)) == 2


class TestOwnWordsSurvive:
    """Обратная сторона: фильтр всегда выбрасывает микрофонную копию.

    Если выбросить лишнее, пропадут слова САМОГО владельца, а останется
    пересказ собеседника — это хуже, чем недоловленное эхо."""

    def test_short_reply_inside_a_long_system_line_is_kept(self):
        utterances = [
            _u(10.0, "system", "да конечно давай так и сделаем на следующей неделе"),
            _u(10.5, "mic", "да конечно"),
        ]
        assert len(drop_echo(utterances)) == 2

    def test_unrelated_line_in_the_window_is_kept(self):
        utterances = [
            _u(10.0, "system", "я пришлю архив вечером"),
            _u(11.0, "mic", "тогда я посмотрю базу и напишу"),
        ]
        assert len(drop_echo(utterances)) == 2

    def test_containment_threshold_is_a_real_guard(self):
        short = "х" * (CONTAINED_MIN_CHARS - 1)
        assert not looks_like_echo(short, f"вот {short} и дальше ещё много слов")


class TestLooksLikeEcho:
    def test_near_identical_strings(self):
        assert looks_like_echo("Давай перенесём встречу на четверг!",
                               "давай перенесём встречу на четверг")

    def test_containment_in_either_direction(self):
        long = "и всем им нужна площадка через которую это делать я помню"
        short = "всем им нужна площадка через которую это делать"
        assert looks_like_echo(long, short)
        assert looks_like_echo(short, long)

    def test_empty_is_never_echo(self):
        assert not looks_like_echo("", "что-нибудь длинное для сравнения")
        assert not looks_like_echo("что-нибудь длинное для сравнения", "")


class TestNormalize:
    def test_runs_of_spaces_collapse(self):
        """Иначе «а  б» не находится в «а б в» и вхождение не срабатывает."""
        assert normalize("Привет,   Коля!!!") == "привет коля"

    def test_similar_unaffected(self):
        assert similar("Привет, Коля!", "привет коля") > 0.95


class TestMarkingKeepsOwnWords:
    """Помечаем, а не выбрасываем — иначе теряются слова владельца.

    В один кусок микрофона попадает и голос из динамиков, и речь владельца.
    Ревью воспроизвело это на живом примере, и в замере такое нашлось:
    «…всем им нужна площадка через которую это делать я помню» — до «я помню»
    говорит собеседник, дальше владелец. Звук не хранится, значит выброшенное
    не восстановить ничем.
    """

    def _mixed(self):
        return [
            _u(200.7, "system", "И всем им нужна площадка, через которую это делать."),
            _u(195.2, "mic", "всем им нужна площадка через которую это делать я помню"),
        ]

    def test_mixed_chunk_is_marked_not_lost(self):
        marked = mark_echo(self._mixed())
        assert len(marked) == 2
        mic = next(u for u in marked if u["stream"] == "mic")
        assert mic["echo"] is True
        assert "я помню" in mic["text"]

    def test_owner_repeating_and_adding_is_kept_in_the_record(self):
        """Сценарий из ревью: владелец дословно повторил и добавил своё."""
        marked = mark_echo([
            _u(100.0, "system", "курс называется Full Stack Web Developer"),
            _u(103.0, "mic", "правильно понимаю: курс называется Full Stack "
                             "Web Developer, я подберу вам группу"),
        ])
        mic = next(u for u in marked if u["stream"] == "mic")
        assert mic["echo"] is True, "как эхо — да, это правда неотличимо"
        assert "я подберу вам группу" in mic["text"], "но слова владельца целы"

    def test_clean_lines_carry_no_flag(self):
        """Лишний ключ у каждой реплики раздул бы хранилище без пользы."""
        marked = mark_echo([
            _u(10.0, "system", "я пришлю архив вечером"),
            _u(11.0, "mic", "тогда я посмотрю базу и напишу"),
        ])
        assert all("echo" not in u for u in marked)

    def test_input_is_not_mutated(self):
        source = self._mixed()
        mark_echo(source)
        assert all("echo" not in u for u in source)

    def test_drop_echo_is_the_same_decision(self):
        """Осмысление получает ровно непомеченное — две функции не разъезжаются."""
        utterances = self._mixed()
        kept = drop_echo(utterances)
        assert [u["text"] for u in kept] == [
            u["text"] for u in mark_echo(utterances) if not u.get("echo")]
