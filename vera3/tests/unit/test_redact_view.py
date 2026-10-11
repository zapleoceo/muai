"""redact_view / redact_tree: только значение секрета, look-alike целы. Синтетика CANARY."""
from __future__ import annotations

import time

import pytest
from vera_shared.redact_view import MASK, redact_tree, redact_view

# Куски склеены, чтобы секрет-скан CI не принял синтетику за настоящий ключ.
CANARIES = {
    "ghp": "ghp_" + "CANARY" * 4,
    "pat": "github_pat_" + "CANARY" * 4,
    "slack": "xoxb-" + "CANARY-1234567890",
    "aws": "AKIA" + "CANARY0000000000",
    "sk": "sk-" + "CANARY" * 4,
    "jwt": "eyJ" + "CANARY.eyJ" + "CANARY.sigCANARY",
    "pem": "-----BEGIN " + "RSA PRIVATE KEY-----\nCANARYBODY+/=\nMORECANARY\n-----END " + "RSA PRIVATE KEY-----",
    "label": "CANARY-pw-1",
    "url_token": "CANARYtok123456",
    "amz": "CANARYsig99aa",
    "userinfo": "CANARYpw",
}


@pytest.mark.parametrize("key", ["ghp", "pat", "slack", "aws", "sk", "jwt", "pem"])
def test_prefixed_secret_is_masked(key):
    secret = CANARIES[key]
    out = redact_view(f"до {secret} после")
    assert "CANARY" not in out
    assert out.startswith("до ") and out.endswith(" после")


@pytest.mark.parametrize(("text", "canary"), [
    ("password: CANARY-pw-1", "CANARY-pw-1"),
    ("Пароль = CANARY-pw-1", "CANARY-pw-1"),
    ('{"api_key": "CANARY-key-123"}', "CANARY-key-123"),
    ("https://api.x.test/cb?token=CANARYtok123456&lang=ru", "CANARYtok123456"),
    ("https://s3.x.test/f?X-Amz-Credential=AKIACANARY0000000000/20261011/s3"
     "&X-Amz-Signature=CANARYsig99aa&id=7", "CANARYsig99aa"),
    ("https://x.test/auth?code=Q9canary&state=s1", "Q9canary"),
    ("https://x.test/cb#code=Q9canary", "Q9canary"),
    ("ftp://user:CANARYpw@h.test/p", "CANARYpw"),
    ("https://x.test/y#access_token=eyJcanary&type=bearer", "eyJcanary"),
])
def test_contextual_secret_is_masked_value_only(text, canary):
    out = redact_view(text)
    assert canary not in out and MASK in out


def test_url_examples_keep_everything_but_the_value():
    assert redact_view("https://api.x.test/cb?token=CANARYtok123456&lang=ru") \
        == f"https://api.x.test/cb?token={MASK}&lang=ru"
    assert redact_view("ftp://user:CANARYpw@h.test/p") == f"ftp://user:{MASK}@h.test/p"
    assert redact_view("https://x.test/y#access_token=eyJcanary&type=bearer") \
        == f"https://x.test/y#access_token={MASK}&type=bearer"
    assert redact_view("https://x.test/auth?code=Q9canary&state=s1") \
        == f"https://x.test/auth?code={MASK}&state=s1"


@pytest.mark.parametrize("text", [
    "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "da39a3ee5e6b4b0d3255bfef95601890afd80709",
    "d41d8cd98f00b204e9800998ecf8427e",
    "транзакция 4930118822771 прошла",
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
    "https://x/y/123456",
    "https://my.sepay.vn/transactions/details/85261396",
    "https://x.test/promo?code=RU2026",
    "https://x.test/list?page=2&sort=asc",
    "task-sk-learn and disk-0123456789abcdefgh",
    "ключ от кабинета, пароль не помню",
    "Код: f47ac10b-58cc-4372-a567-0e02b2c3d479",
])
def test_look_alikes_are_intact(text):
    assert redact_view(text) == text


def test_idempotent():
    text = " ".join(CANARIES.values()) + " token: CANARYtok123456 ftp://u:pw12345@h/p?token=CANARYtok99999"
    once = redact_view(text)
    assert redact_view(once) == once


def test_empty_and_plain():
    assert redact_view("") == ""
    assert redact_view("просто текст") == "просто текст"


@pytest.mark.parametrize("chunk", [
    "a.b-" * 30,
    "?" + "a=1&" * 30,
    "-----BEGIN " + "RSA PRIVATE KEY----- " * 3,
    "token:" + " " * 30 + "x",
    "eyJabcde." * 30,
    "http://" + "a:" * 30,
    "sk-" * 40,
])
def test_linear_time_on_100kb(chunk):
    text = chunk * (100_000 // len(chunk))
    t0 = time.perf_counter()
    redact_view(text)
    assert time.perf_counter() - t0 < 2.0


def test_tree_masks_secret_keys_and_leaves_structure():
    src = {
        "Authorization": "Bearer abc", "cookie": {"sid": "x", "n": 5}, "password": 12345678,
        "note": f"key {CANARIES['ghp']}", "id": 7, "uuid": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "items": [{"token": "t"}, "plain", "password: CANARY-pw-1"], "flag": True, "none": None,
        "api_key": None,
    }
    out = redact_tree(src)
    assert out["Authorization"] == MASK and out["cookie"] == {"sid": MASK, "n": MASK}
    assert out["password"] == MASK and out["items"][0] == {"token": MASK}
    assert "CANARY" not in str(out)
    assert out["id"] == 7 and out["uuid"] == src["uuid"] and out["flag"] is True
    assert out["none"] is None and out["api_key"] is None
    assert out["items"][1] == "plain" and src["items"][0] == {"token": "t"}
    assert out.keys() == src.keys()
