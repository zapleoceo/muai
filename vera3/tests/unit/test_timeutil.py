"""`utc_naive_now()` — «сейчас» в том виде, в каком оно ложится в БД.

Все datetime-колонки наивные и хранят UTC. Раньше это писалось как
`datetime.utcnow()` в 53 местах: она deprecated с Python 3.12 и снята с
плана поддержки. Буквальная замена `datetime.now(UTC).replace(tzinfo=None)`
— три вызова в строке, которые разъезжаются при копировании: достаточно
забыть `.replace()`, и в наивную колонку поедет aware-datetime.
"""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vera_shared.timeutil import utc_naive_now

VERA3 = Path(__file__).resolve().parents[2]


def test_result_is_naive():
    assert utc_naive_now().tzinfo is None


def test_result_is_utc_not_local():
    """Главное свойство: наивное, но именно UTC. Ошибка здесь незаметна на
    UTC-машине и ломает всё на любой другой — а прод в UTC, владелец в UTC+7."""
    ours = utc_naive_now()
    reference = datetime.now(UTC).replace(tzinfo=None)
    assert abs(ours - reference) < timedelta(seconds=5)


def test_matches_what_utcnow_used_to_return():
    """Замена обязана быть поведенчески тождественной старому вызову."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        legacy = datetime.utcnow()      # noqa: DTZ003 — сравниваем со старым поведением
    assert abs(utc_naive_now() - legacy) < timedelta(seconds=5)


def test_no_utcnow_left_in_the_tree():
    """Включая передачу как CALLABLE (`default_factory=datetime.utcnow`) —
    единственный случай, который не ловят ни grep по `utcnow()`, ни ruff
    DTZ003; он нашёлся только по DeprecationWarning из pydantic."""
    hits = subprocess.run(
        ["git", "grep", "-n", "datetime.utcnow", "--",
         "vera3/shared", "vera3/services", "vera3/scripts",
         # сам timeutil объясняет, ПОЧЕМУ этот вызов запрещён, и обязан
         # называть его по имени
         ":!vera3/shared/vera_shared/timeutil.py"],
        cwd=VERA3.parent, capture_output=True, text=True,
    ).stdout.strip()
    assert not hits, f"datetime.utcnow вернулась:\n{hits}"
