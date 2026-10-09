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
    "сумма 12 345 678 и дата 2026-10-10",
])
def test_plain_text_is_kept(text):
    assert redact_secrets(text) == text


# Ревью 10.10.2026: утечки, найденные на первой версии.
LIVE = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"
IBANS = ("DE89 3704 0044 0532 0130 00", "UA21 3223 1300 0002 6007 2335 6600 1",
         "GB82 WEST 1234 5698 7654 32")


@pytest.mark.parametrize(("text", "leaks"), [
    ("мой пароль: Qwerty!23 а дальше", ["Qwerty", "!23"]),
    ("password=Pa$$w0rd, ок", ["Pa$$w0rd"]),
    ("карта 4111 1111 1111 1112 до вечера", ["4111 1111 1111 1112"]),
    ("номер 1234-5678-9012-3456 скинь", ["1234-5678-9012-3456"]),
    *[(f"переведи на {iban} сегодня", [iban]) for iban in IBANS],
    ("подключись к postgres://vera:s3cr3t@db.local/vera", ["s3cr3t"]),
    (f"ключ {LIVE}", [LIVE]),
    (f"api_key={LIVE}", [LIVE]),
    ("пин 4821 от карты", ["4821"]),
    ("пин-код: 4821", ["4821"]),
    ("код из смс 482913", ["482913"]),
])
def test_review_leaks_are_masked(text, leaks):
    out = redact_secrets(text)
    for leak in leaks:
        assert leak not in out, out
    assert MASK in out


@pytest.mark.parametrize("text", [
    "позвони +380 67 123 45 67 после обеда",
    "позвони +380671234567",
    "встреча 10.10.2026 в 14:30",
    "оплати 1 250 000 грн до пятницы",
    "заказ 482913 уехал",
    "упал деплой бота, логи в postgres://db.local/vera",
])
def test_review_negatives_are_kept(text):
    assert redact_secrets(text) == text
