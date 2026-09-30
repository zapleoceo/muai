"""Кодовая фраза: ловим искажения whisper, не ловим обычную речь.

Искажения взяты из того, как whisper реально пишет русскую речь в этом
слушателе: «Веро» вместо «Вера», «помошь», пропавшие запятые, капс.
"""
from __future__ import annotations

import pytest

from vera_listener.codeword import DEFAULT_PHRASE, find

OWNER_TEST = "Вера, мне нужна помощь, срочно напиши мне что-то в телеграм"


def test_owner_test_phrase_gives_instruction():
    hit = find(OWNER_TEST)
    assert hit is not None
    assert hit.instruction == "срочно напиши мне что-то в телеграм"


@pytest.mark.parametrize("text, instruction", [
    ("Веро мне нужна помошь срочно напиши", "срочно напиши"),
    ("вера мне нужна помощь найди письмо от Ли", "найди письмо от Ли"),
    ("ВЕРА МНЕ НУЖНА ПОМОЩЬ. Напомни про встречу", "Напомни про встречу"),
    ("Вера, мне нужно помощь, найди письмо от Ли", "найди письмо от Ли"),
    ("Веру мне нужна помочь посчитай выручку", "посчитай выручку"),
    ("Вера мне нужна помощ — что там с договором", "что там с договором"),
    ("Слушай, секунду. Вера, мне нужна помощь: сводка за день", "сводка за день"),
    ("Вёра, мне нужна помощь, ответь", "ответь"),
])
def test_distorted_phrase_is_recognised(text, instruction):
    hit = find(text)
    assert hit is not None, text
    assert hit.instruction == instruction


@pytest.mark.parametrize("text", [
    "Мне нужна помощь с отчётом",                 # без обращения
    "Вера сказала что ей нужна помощь",           # «вера» в обычной речи
    "Я верно говорю, мне нужна помощь",           # похожее слово
    "Вера, мне не нужна помощь",                  # отрицание
    "Мера, мне нужна помощь",                     # не то обращение
    "Вера мне помощь нужна",                      # перестановка — не фраза
    "Вера Павловна просила помощи с отчётом",
    "Ведро мне нужно помыть",
    "",
])
def test_ordinary_speech_does_not_trigger(text):
    assert find(text) is None, text


def test_phrase_at_end_of_chunk_has_empty_instruction():
    hit = find("так, Вера, мне нужна помощь.")
    assert hit is not None
    assert hit.instruction == ""


def test_repeated_phrase_in_one_line_is_not_part_of_instruction():
    hit = find("Вера мне нужна помощь, Вера, мне нужна помощь, напиши мне")
    assert hit is not None
    assert hit.instruction == "напиши мне"


def test_phrase_is_configurable():
    assert find("Слушай, Вера, запиши: купить молоко", phrase="Вера запиши") \
        .instruction == "купить молоко"
    assert find(OWNER_TEST, phrase="Вера запиши") is None


def test_default_phrase_is_the_owners():
    assert DEFAULT_PHRASE == "Вера, мне нужна помощь"
