"""Служебные слова ru/uk/en/id — один список на все построители tsquery.

Запрос — всегда OR префиксов, поэтому каждое служебное слово расширяет
выборку: «bagaimana:*» или «який:*» матчит пол-корпуса и топит редкие
содержательные слова. Русский стоп-лист Postgres знает только русский, а у
`indonesian` стоп-листа нет вовсе, так что фильтруем до tsquery сами.
"""
from __future__ import annotations

import re

_RU = """
и в во на о об по у к ко с со для это ли ну же то был была были было
быть есть не ни при из за от до про над под без через или но а да нет как
что кто где когда почему зачем сколько какой какая какие какое каких каком
который которая которые чем чей ты я мне мы вы он она они оно мой моя мои
твой наш ваш их его её им ему себя там тут здесь вот так уже еще ещё тоже
только можно нужно надо будет всё все весь
"""
_UK = """
і й та в у на о по до від з із зі для це що ні чи ну же то був була були
було бути є не при за про над під без через або але так як хто де коли
чому навіщо скільки який яка які яке яких якому котрий чим чий ти я мені
ми ви він вона вони воно мій моя мої твій наш ваш їх його її їм йому себе
там тут ось вже ще теж тільки можна треба буде всі все весь
"""
_EN = """
the and or of to in on for is are was were be been being at by an a it with
from this that these those who whom whose what which when where why how
do does did done can could should would will shall may might must have has
had having i me my we us our you your he him his she her they them their
its as if then than so not no yes about into over under up down out off
there here also just only any some all
"""
_ID = """
yang dan di ke dari untuk ini itu dengan atau pada ada tidak juga adalah
akan sudah belum telah sedang masih bisa dapat harus saya aku kami kita
kamu anda dia mereka nya apa siapa kapan dimana mana kenapa mengapa
bagaimana berapa apakah oleh dalam sebagai karena jika kalau
agar supaya lebih sangat hanya semua setiap para sebuah seorang
"""

STOPWORDS: frozenset[str] = frozenset((_RU + _UK + _EN + _ID).split())

# Без дефиса: он служебный символ tsquery, «it-step:*» разобралось бы как И-выражение
_WORD_RE = re.compile(r"\w+", re.UNICODE)
MIN_WORD_LEN = 2


def is_stopword(word: str) -> bool:
    return word.lower() in STOPWORDS


def content_words(question: str) -> list[str]:
    """Значимые слова вопроса в исходном регистре и порядке."""
    return [w for w in _WORD_RE.findall(question)
            if len(w) >= MIN_WORD_LEN and not is_stopword(w)]
