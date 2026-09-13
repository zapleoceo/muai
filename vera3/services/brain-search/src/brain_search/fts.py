"""Полнотекстовые SQL-выражения по events.content_text — в одном месте.

До 2026-09-13 поиск стоял на одной конфигурации `russian`. Замер на 5%
выборке прода (22 тыс. событий из 445 тыс.): английский она уже стеммит
сама (в `russian` латиница идёт в `english_stem`: payments → payment),
украинский ловит префиксом русского стема (зустріч:* 55 из 55), а
индонезийский — нет: pendaftaran 2 против 15, pembayaran 4 против 9.

Поэтому вторая конфигурация — `indonesian` (snowball, есть в pg16):
латиницу она стеммит по-индонезийски (pembayaran/membayar → bayar), а
кириллицу не трогает, то есть для украинского это `simple` — точный
токен в нижнем регистре (размер вектора 991 449 против 991 559 у simple).
Третий словарь `simple` ничего бы не добавил. Hunspell uk в образе нет.

Индексы раздельные (`ix_events_fts_russian` на проде уже есть — ставился
руками, 031 его фиксирует; `ix_events_fts_indonesian` — новый 031), условие
через OR → BitmapOr двух GIN. Объединённый вектор `ru || id` отвергнут:
индекс в 1.58× больше русского и его пришлось бы строить заново рядом со
старым, а отдельный — ~1.19× (~150 МБ) и старый не трогает.
"""
from __future__ import annotations

#: Порядок значим: первая — основная, её ts_rank сохраняет прежний порядок.
FTS_CONFIGS: tuple[str, ...] = ("russian", "indonesian")

#: У `indonesian` в Postgres нет стоп-листа, а русский стоп-лист не знает
#: английских и индонезийских служебных слов — «the:*» во второй
#: конфигурации матчил бы почти каждое английское письмо.
_FOREIGN_STOPWORDS = frozenset({
    "the", "and", "or", "of", "to", "in", "on", "for", "is", "are", "at",
    "by", "an", "it", "be", "with", "from", "this", "that",
    "yang", "dan", "di", "ke", "dari", "untuk", "ini", "itu", "dengan",
    "atau", "pada", "ada", "tidak", "juga",
})


def build_ts_query(words: list[str]) -> str:
    """Слова пользователя → `w1:* | w2:*`; одна строка для всех конфигураций."""
    kept = [w for w in words if w.lower() not in _FOREIGN_STOPWORDS]
    return " | ".join(f"{w}:*" for w in kept)


def _vec(cfg: str) -> str:
    # Дословно как в индексе: иначе планировщик индекс не возьмёт.
    return f"to_tsvector('{cfg}', content_text)"


def _query(cfg: str, param: str) -> str:
    return f"to_tsquery('{cfg}', :{param})"


def fts_match_sql(param: str = "tsq") -> str:
    """Условие WHERE: совпадение хотя бы в одной конфигурации."""
    ors = " OR ".join(f"{_vec(c)} @@ {_query(c, param)}" for c in FTS_CONFIGS)
    return f"({ors})"


def fts_rank_sql(param: str = "tsq") -> str:
    """ts_rank основной конфигурации, а где она дала 0 — следующей.

    Запрос всегда OR префиксов, поэтому ts_rank > 0 ⇔ совпадение (проверено
    на проде: несовпавший документ даёт ровно 0). Строки, найденные русским
    раньше, получают прежний rank — их взаимный порядок не меняется.
    NULLIF, а не CASE WHEN … @@ …: так to_tsvector на строку считается на
    раз меньше (recheck GIN и так пересчитывает его из content_text)."""
    ranks = [f"NULLIF(ts_rank({_vec(c)}, {_query(c, param)}), 0)"
             for c in FTS_CONFIGS]
    return f"COALESCE({', '.join(ranks)}, 0.0)"
