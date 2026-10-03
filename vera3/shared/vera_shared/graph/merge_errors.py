"""Ошибки слияния и правок сущностей — отдельно, чтобы не тянуть merge ради исключения."""
from __future__ import annotations


class MergeError(ValueError):
    """Слияние невозможно: нет сущности, пустой список или keep среди drop."""
