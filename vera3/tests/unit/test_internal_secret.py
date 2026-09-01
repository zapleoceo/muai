"""Общая проверка X-Internal-Secret: fail-closed и постоянная по времени.

Проверка жила в двух независимых копиях (gateway и brain-search), обе
сравнивали обычным `!=`. Тесты держат оба свойства сразу на общей функции и
на обёртках обеих служб — чтобы третья копия не отросла молча.
"""
from __future__ import annotations

import hmac
import inspect

import pytest
from fastapi import HTTPException
from vera_shared.auth import internal_secret_ok


@pytest.mark.parametrize(("provided", "expected", "ok"), [
    ("s3cret", "s3cret", True),
    ("s3cret", "other", False),
    ("", "s3cret", False),
    (None, "s3cret", False),
    # секрет не сконфигурирован → закрыто для всех, а не открыто
    ("anything", "", False),
    ("anything", None, False),
    (None, None, False),
    ("", "", False),
    # префикс не считается совпадением
    ("s3cr", "s3cret", False),
    ("s3cretX", "s3cret", False),
])
def test_internal_secret_ok(provided, expected, ok):
    assert internal_secret_ok(provided, expected) is ok


def test_comparison_is_constant_time():
    """Обычный != выходит на первом несовпавшем байте. Проверяем по исходнику,
    а не по таймингам: замер времени на CI — заведомо флейки тест."""
    src = inspect.getsource(internal_secret_ok)
    assert "compare_digest" in src
    assert hmac.compare_digest("a", "a")


def test_gateway_wrapper_uses_shared_check(monkeypatch):
    from gateway import auth as gw
    from gateway.config import Settings

    # get_settings — module-level singleton, а не lru_cache: подменяем его,
    # чтобы не полагаться на порядок импортов между тестами.
    def _with(secret: str):
        monkeypatch.setattr(gw, "get_settings",
                            lambda: Settings(internal_secret=secret))

    _with("s3cret")
    gw.check_internal_secret("s3cret")
    with pytest.raises(HTTPException):
        gw.check_internal_secret("wrong")
    with pytest.raises(HTTPException):
        gw.check_internal_secret(None)

    # секрет не сконфигурирован → закрыто для всех, а не открыто
    _with("")
    with pytest.raises(HTTPException):
        gw.check_internal_secret("anything")


def test_brain_search_wrapper_uses_shared_check(monkeypatch):
    from brain_search.app import check_internal_secret

    monkeypatch.setenv("INTERNAL_SECRET", "s3cret")
    check_internal_secret("s3cret")
    with pytest.raises(HTTPException):
        check_internal_secret("wrong")
    monkeypatch.setenv("INTERNAL_SECRET", "")
    with pytest.raises(HTTPException):
        check_internal_secret("anything")


def test_no_third_copy_of_the_comparison():
    """Обе службы должны звать общую функцию, а не сравнивать сами."""
    from brain_search import app as bs
    from gateway import auth as gw

    for mod in (gw, bs):
        src = inspect.getsource(mod.check_internal_secret)
        assert "internal_secret_ok" in src, f"{mod.__name__} завёл свою проверку"
        assert "!=" not in src, f"{mod.__name__} сравнивает секрет напрямую"
