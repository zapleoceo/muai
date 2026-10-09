"""`redact_secrets`: секреты из распознанной речи не уходят в комнату агентов.

Образцы собираются из кусков: целиком они попали бы в сканер секретов CI.
"""
from __future__ import annotations

import pytest
from vera_shared.redact import MASK, redact_secrets

SK = "sk-" + "Ab3d" * 6
ANT = "sk-" + "ant-" + "x9Y" * 8
BOT = "1234567890" + ":AA" + "b" * 33
HEX = "0123456789abcdef" * 3
B64 = "QmFzZTY0" + "U2VjcmV0" * 4 + "9xZ"


@pytest.mark.parametrize(("text", "secret"), [
    (f"вот ключ {SK} держи", SK),
    (f"anthropic {ANT}", ANT),
    (f"токен бота {BOT}", BOT),
    (f"хэш {HEX}", HEX),
    (f"blob {B64}", B64),
    ("пароль qwerty123 от сервера", "qwerty123"),
    ("password: hunter2", "hunter2"),
    ("пароль ку два шесть", "ку два шесть"),
    ("карта 4111 1111 1111 1111 до вечера", "4111 1111 1111 1111"),
    ("token=abcDEF123456789xyz", "abcDEF123456789xyz"),
])
def test_secret_is_masked(text, secret):
    out = redact_secrets(text)
    assert secret not in out and MASK in out


@pytest.mark.parametrize("text", [
    "упал деплой бота, посмотри логи",
    "позвони Пете в 15:30 по номеру заказа 12345",
    "номер 1234 5678 9012 3456 не карта",   # не проходит Луна
])
def test_plain_text_is_kept(text):
    assert redact_secrets(text) == text
