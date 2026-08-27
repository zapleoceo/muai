"""Карточка события `/events/{id}` — единственный путь к дословной стенограмме.

Без неё «сможем обратиться к тексту» было бы обещанием: `content_extra` не
читает ни триаж, ни поиск, ни журнал событий — только эта страница.
"""
from __future__ import annotations

from dashboard.events_routes import STREAM_LABEL, transcript_html


def _extra(**over):
    base = {
        "kind": "voice_transcript",
        "chars": 42,
        "utterances": [
            {"at": 0.0, "stream": "mic", "text": "Ли, по разделу шесть?"},
            {"at": 65.4, "stream": "system", "text": "Пусть будет пятьдесят."},
        ],
    }
    base.update(over)
    return base


class TestTranscriptHtml:
    def test_shows_who_said_what(self):
        html = transcript_html(_extra())
        assert "Ли, по разделу шесть?" in html
        assert "Пусть будет пятьдесят." in html
        assert STREAM_LABEL["mic"] in html and STREAM_LABEL["system"] in html

    def test_timestamps_are_minutes_and_seconds(self):
        html = transcript_html(_extra())
        assert ">00:00<" in html
        assert ">01:05<" in html

    def test_header_counts_replies_and_characters(self):
        html = transcript_html(_extra(chars=1234))
        assert "2 реплик" in html and "1234 символов" in html

    def test_escapes_everything_from_the_transcript(self):
        """Текст пришёл из распознавания чужой речи — доверять ему нельзя."""
        html = transcript_html(_extra(utterances=[
            {"at": 1.0, "stream": "mic", "text": "<script>alert(1)</script>"},
            {"at": 2.0, "stream": "<img onerror=x>", "text": "ок"},
        ]))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "<img onerror" not in html

    def test_unknown_stream_is_shown_as_is_not_guessed(self):
        html = transcript_html(_extra(utterances=[
            {"at": 0.0, "stream": "speaker_2", "text": "привет"}]))
        assert "speaker_2" in html


class TestNothingToShow:
    def test_no_extra_at_all(self):
        assert transcript_html(None) == ""

    def test_event_of_another_kind(self):
        """У чужих событий в content_extra лежит что угодно — не наше дело."""
        assert transcript_html({"kind": "gmail_headers", "from": "x@y"}) == ""

    def test_empty_transcript(self):
        assert transcript_html(_extra(utterances=[])) == ""

    def test_missing_fields_do_not_crash(self):
        html = transcript_html({"kind": "voice_transcript",
                                "utterances": [{"text": "без метки и времени"}]})
        assert "без метки и времени" in html
        assert ">00:00<" in html
