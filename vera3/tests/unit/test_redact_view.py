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


def test_truncated_pem_keeps_trailing_prose():
    head = "-----BEGIN " + "PRIVATE KEY-----\nCANARYBODYAAAA+/=\nCANARYMORE==\n"
    out = redact_view(head + "\nHello world, дальше обычный текст")
    assert "CANARY" not in out and out.endswith("Hello world, дальше обычный текст")


@pytest.mark.parametrize("text", [
    "git token: " + CANARIES["ghp"],
    "git token " + CANARIES["ghp"],
])
def test_incident_shape_with_and_without_colon(text):
    out = redact_view(text)
    assert "CANARY" not in out and out.startswith("git token")


_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
_END = "-----END " + "PRIVATE KEY-----"
_AIZA = "AIza" + "CANARY" * 5 + "Zz012"  # 4 + 35
_TG = "bot123456789:AA" + "CANARY" * 6


@pytest.mark.parametrize("text", [
    '{"private_key": "' + _BEGIN + "\\nCANARY" + "A" * 300 + "\\nCANARYB==\\n" + _END + '\\n", "n": 1}',
    _BEGIN + "\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,CANARY0123\n\nCANARYAAAA\nCANARYB\n" + _END + " tail",
    _BEGIN + "\n" + "CANARY" * 60 + "\n" + _END,
    "glpat-" + "CANARY" * 4, "sk_live_" + "CANARY" * 4, "rk_test_" + "CANARY" * 4,
    "xapp-1-CANARY-123456", "https://api.telegram.org/" + _TG + "/getMe", _AIZA,
    "Authorization: Bearer CANARYop", "x Bearer CANARYopaque1234567 y",
    "SECRET_KEY=CANARY1", "aws_secret_access_key = CANARYabc", "passwd=CANARY",
    "DJANGO_API_KEY='CANARY12'", "DB_PASSWORD=CANARY", "BOT_TOKEN=CANARY",
    "пароль от почты: CANARYpw", "password for x: CANARYpw", "токен бота: CANARYtk",
    "password: CAN12", "redis://:CANARYpw@host:6379/0",
    "https://www.google.com/maps/embed?key=" + _AIZA + "&q=x", "https://x.test/p?key=CANARYkey" + "a" * 20,
])
def test_followup_secrets_masked(text):
    out = redact_view(text)
    assert "CANARY" not in out and "AIza" not in out and MASK in out
    assert redact_view(out) == out


def test_pem_without_end_keeps_prose_after_headers_and_blank_line():
    out = redact_view(_BEGIN + "\nProc-Type: 4,ENCRYPTED\nCANARYAAAA\n\nHello world prose")
    assert "CANARY" not in out and out.endswith("\nHello world prose")


@pytest.mark.parametrize("text", [
    "TOKEN_COUNT=42", "MAX_TOKENS=4096", "password: abc", "token: null", "password is required",
    "Bearer of bad news", "https://x.test/p?key=page2&a=1", "https://x.test/p?key=abc123",
    "key=value pairs are fine", "bot123456789:short", "AIza-short",
    "https://api.telegram.org/bot/getMe", "passwords are hashed", "ключ: кабинет 5",
])
def test_followup_look_alikes_intact(text):
    assert redact_view(text) == text


def test_maps_embed_masks_only_the_key():
    out = redact_view("https://maps.test/embed?key=" + _AIZA + "&q=x&key=page2")
    assert out == f"https://maps.test/embed?key={MASK}&q=x&key=page2"


def test_tree_key_suffix_match_on_segment_boundary():
    kept = {"token_count": 42, "tokens_used": 7, "max_tokens": 9, "secret_santa": "bob",
            "keyboard": "k", "tokenizer": "t", "n": 1}
    src = {"x-api-key": "CANARY", "accessToken": "CANARY", "Set-Cookie": "CANARY", "client_secret": 1,
           "private_key": {"a": "CANARY"}, "credentials": ["CANARY"], "id_token": "CANARY", **kept}
    out = redact_tree(src)
    for k in ("x-api-key", "accessToken", "Set-Cookie", "client_secret", "id_token"):
        assert out[k] == MASK
    assert out["private_key"] == {"a": MASK} and out["credentials"] == [MASK]
    assert {k: out[k] for k in kept} == kept


def test_gcp_service_account_json_text_masked():
    text = ('{"type": "service_account", "private_key_id": "f47ac10b58cc4372a5670e02b2c3d479", '
            '"private_key": "' + _BEGIN + "\\n" + "CANARY" * 50 + "\\n" + _END + '\\n"}')
    out = redact_view(text)
    assert "CANARY" not in out and '"type": "service_account"' in out


@pytest.mark.parametrize("chunk", [
    "A_" * 30, "Bearer " * 20, "password " * 20, "-----BEGIN PRIVATE KEY-----\\n" * 3,
    "TOKEN=" * 20, "key=" + "a" * 19 + "?", "1234567:" * 20, "authorization:" * 10,
])
def test_followup_linear_time_on_100kb(chunk):
    text = chunk * (100_000 // len(chunk))
    t0 = time.perf_counter()
    redact_view(text)
    assert time.perf_counter() - t0 < 2.0
