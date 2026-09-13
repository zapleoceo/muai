"""scripts/pilots/metrics.py — метрики, на которых стоят выводы docs/model-pilots.md.

Держат то, что легко сломать незаметно: эталон gemini сравнивается только
дословной частью после «Текст:», суммы в разных записях (точки, запятые,
пробелы) считаются одной, WER не штрафует «ё/е» и регистр.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pilots" / "metrics.py"


@pytest.fixture(scope="module")
def m():
    spec = importlib.util.spec_from_file_location("pilot_metrics", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gemini_ocr_part_takes_only_verbatim_tail(m):
    assert m.gemini_ocr_part("Скриншот чата. Текст: Price: 209.000/pcs") == "Price: 209.000/pcs"
    assert m.gemini_ocr_part("просто описание") == "просто описание"


def test_char_similarity_ignores_case_markup_and_spacing(m):
    assert m.char_similarity("**Итого**   500", "итого 500") == 1.0
    assert m.char_similarity("", "") == 1.0
    assert m.char_similarity("abc", "xyz") == 0.0
    assert 0.0 < m.char_similarity("Tổng cộng", "Tong cong") < 1.0


def test_token_recall_counts_duplicates_once_each(m):
    assert m.token_recall("да да нет", "да да") == 1.0
    assert m.token_recall("да", "да да") == 0.5
    assert m.token_recall("что угодно", "") == 1.0


def test_numbers_unify_thousand_separators(m):
    assert m.numbers("1.250.000 ₫") == m.numbers("1 250 000") == m.numbers("1,250,000") == {"1250000"}
    assert m.numbers("шт 5") == set()


def test_number_recall(m):
    assert m.number_recall("итого 209.000", "Price : 209 000/pcs, 22:10") == 0.5
    assert m.number_recall("что угодно", "без цифр") is None


def test_wer_normalizes_yo_and_punctuation(m):
    assert m.wer("Ещё, раз!", "еще раз") == 0.0
    assert m.wer("один два", "один три четыре") == pytest.approx(2 / 3)
    assert m.wer("", "") == 0.0
    assert m.wer("лишнее", "") == 1.0


def test_numbers_do_not_glue_neighbour_cells(m):
    assert m.numbers("12 34") == {"12", "34"}
    assert m.numbers("22:10") == {"2210"}


def test_levenshtein_is_symmetric(m):
    assert m.levenshtein("kitten", "sitting") == m.levenshtein("sitting", "kitten") == 3
