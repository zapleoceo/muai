"""Тесты dashboard auth — HMAC binding к OWNER_ID, TG widget signature."""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time

import pytest


@pytest.fixture
def auth_module(monkeypatch):
    """Подменяем env vars и (пере)загружаем модуль.

    importlib.reload вместо del+import: `from dashboard import auth` после
    del возвращает старый объект из атрибута пакета (не в sys.modules),
    и последующий reload падает с ImportError.
    """
    import importlib
    monkeypatch.setenv("TOKEN_SECRET", "test-secret-very-long-and-random")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:test-bot-token")
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "169510539")
    sys.path.insert(0, os.path.join(
        os.path.dirname(__file__), "..", "..",
        "services", "dashboard", "src",
    ))
    if "dashboard.auth" in sys.modules:
        module = importlib.reload(sys.modules["dashboard.auth"])
    else:
        module = importlib.import_module("dashboard.auth")
    return module


def test_session_cookie_round_trip(auth_module):
    cookie, ttl = auth_module.issue_session()
    assert ttl == 60 * 60 * 24 * 30
    assert auth_module._verify(cookie) is True


def test_session_invalid_hmac_rejected(auth_module):
    cookie, _ = auth_module.issue_session()
    payload, _sig = cookie.rsplit(".", 1)
    tampered = f"{payload}.{'a' * 64}"
    assert auth_module._verify(tampered) is False


def test_session_owner_id_binding(auth_module, monkeypatch):
    """Cookie выпущенный для одного OWNER_ID не валиден если OWNER_ID сменили."""
    import importlib
    cookie, _ = auth_module.issue_session()
    # Симулируем смену owner — reload модуля перечитывает env
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "999999")
    auth2 = importlib.reload(auth_module)
    assert auth2._verify(cookie) is False


def test_session_no_dot_rejected(auth_module):
    assert auth_module._verify("no-dot-here") is False


def test_session_malformed_payload_rejected(auth_module):
    """payload без owner_id (старый формат "owner:<ts>") не должен пройти."""
    old_format = "owner:" + str(int(time.time()))
    sig = hmac.new(b"test-secret-very-long-and-random",
                   old_format.encode(), hashlib.sha256).hexdigest()
    bad = f"{old_format}.{sig}"
    assert auth_module._verify(bad) is False


def test_session_expired_rejected(auth_module):
    """Cookie старше TTL отклоняется."""
    old_ts = int(time.time()) - (60 * 60 * 24 * 31)  # 31 день назад
    payload = f"owner:169510539:{old_ts}"
    sig = hmac.new(b"test-secret-very-long-and-random",
                   payload.encode(), hashlib.sha256).hexdigest()
    expired = f"{payload}.{sig}"
    assert auth_module._verify(expired) is False


def test_owner_id_zero_refused(auth_module, monkeypatch):
    """OWNER_TELEGRAM_ID=0 = fail-fast при загрузке модуля."""
    import importlib
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "0")
    with pytest.raises(RuntimeError, match="не должен быть 0"):
        importlib.reload(auth_module)
    # Восстанавливаем рабочее состояние модуля для остальных тестов
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "169510539")
    importlib.reload(auth_module)


def test_widget_signature_valid(auth_module):
    """Telegram widget HMAC подпись с правильным bot_token и id Димы."""
    bot_token = "12345:test-bot-token"
    user_id = 169510539
    auth_date = int(time.time())
    fields = {
        "id": str(user_id),
        "first_name": "Dima",
        "auth_date": str(auth_date),
    }
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    h = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    data = {**fields, "hash": h}
    assert auth_module.verify_telegram_auth(data) == user_id


def test_widget_signature_tampered_rejected(auth_module):
    """Изменённое поле → подпись больше не валидна."""
    bot_token = "12345:test-bot-token"
    fields = {"id": "169510539", "first_name": "Dima",
              "auth_date": str(int(time.time()))}
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    h = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    # Подменяем user_id после подписи
    data = {**fields, "id": "999999999", "hash": h}
    assert auth_module.verify_telegram_auth(data) is None


def test_widget_signature_old_auth_date_rejected(auth_module):
    """auth_date старше 5 мин (WIDGET_AUTH_TTL_S) — отказ."""
    bot_token = "12345:test-bot-token"
    old_ts = int(time.time()) - 600  # 10 мин назад
    fields = {"id": "169510539", "first_name": "Dima", "auth_date": str(old_ts)}
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    h = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    data = {**fields, "hash": h}
    assert auth_module.verify_telegram_auth(data) is None
