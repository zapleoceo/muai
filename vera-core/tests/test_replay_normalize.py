"""Sender-key normalization: same person → same key across rows."""
from app.triage.replay import _normalize_sender


def test_email_extracted_from_display_name():
    assert (_normalize_sender('"Joinposter.com" <contact@joinposter.com>')
            == 'contact@joinposter.com')
    assert (_normalize_sender('Bybit <noreply@email-service.bybit.com>')
            == 'noreply@email-service.bybit.com')


def test_username_extracted():
    assert _normalize_sender('VerandaBot (@VerandamyBot)') == 'verandamybot'
    assert _normalize_sender('@SomeUser') == 'someuser'


def test_plain_name_lowercased():
    assert _normalize_sender('Eva') == 'eva'
    assert _normalize_sender('  Eva ') == 'eva'


def test_empty_returns_empty():
    assert _normalize_sender('') == ''
    assert _normalize_sender(None) == ''
    assert _normalize_sender('   ') == ''


def test_email_wins_over_username():
    # If both present, email is more stable
    assert (_normalize_sender('@bot John <j@example.com>')
            == 'j@example.com')
