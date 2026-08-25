"""Отсев: что уходит в мозг, а что не стоит распознавания."""
from __future__ import annotations

from vera_listener.gate import judge, system_audio_allowed

ALLOW = ("zoom.exe", "telegram.exe")
BROWSERS = ("chrome.exe",)
DENY = ("spotify.exe",)


def _judge(mic: float, system: float, app: str | None):
    return judge({"mic": mic, "system": system}, app=app, allow=ALLOW,
                 browsers=BROWSERS, deny=DENY, min_speech_s=25.0,
                 monologue_speech_s=45.0)


def test_two_sided_call_is_kept():
    assert _judge(30.0, 40.0, "zoom.exe") == (True, "dialogue")


def test_short_exchange_is_dropped():
    assert _judge(5.0, 4.0, "zoom.exe").reason == "too_short"


def test_room_monologue_needs_to_be_long():
    assert _judge(30.0, 0.0, None).keep is False
    assert _judge(50.0, 0.0, None) == (True, "monologue")


def test_video_playing_alone_is_not_a_conversation():
    assert _judge(0.0, 600.0, "chrome.exe") == (False, "media_only")
    assert _judge(0.0, 600.0, "spotify.exe") == (False, "media_only")


def test_browser_call_passes_when_mic_is_active():
    # Meet живёт в chrome.exe там же, где ютуб: пускаем, раз говорят обе стороны.
    assert _judge(20.0, 40.0, "chrome.exe") == (True, "dialogue")


def test_browser_audio_ignored_when_only_video_plays():
    assert system_audio_allowed("chrome.exe", mic_speech_s=1.0, allow=ALLOW,
                                browsers=BROWSERS, deny=DENY) is False
    assert system_audio_allowed("chrome.exe", mic_speech_s=9.0, allow=ALLOW,
                                browsers=BROWSERS, deny=DENY) is True


def test_denylisted_app_never_passes():
    assert system_audio_allowed("spotify.exe", mic_speech_s=99.0, allow=ALLOW,
                                browsers=BROWSERS, deny=DENY) is False


def test_webinar_with_a_couple_of_words_from_me_is_kept():
    assert _judge(2.0, 300.0, "zoom.exe") == (True, "call_one_sided")
