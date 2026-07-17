"""ingestor_gmail.poller._html_to_text — script/style удаляются целиком."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "ingestor-gmail", "src"))

os.environ.setdefault("GMAIL_CLIENT_ID", "test-cid")
os.environ.setdefault("GMAIL_CLIENT_SECRET", "test-csec")

from ingestor_gmail.poller import _html_to_text  # noqa: E402


def test_strips_tags_and_collapses_whitespace():
    assert _html_to_text("<p>Привет,\n  <b>Дима</b>!</p>") == "Привет, Дима !"


def test_script_content_removed_entirely():
    html = "<p>до</p><script>var t = 'секретный js';</script><p>после</p>"
    out = _html_to_text(html)
    assert "секретный js" not in out
    assert out == "до после"


def test_style_content_removed_entirely():
    out = _html_to_text("<style>.x{color:red}</style><div>текст</div>")
    assert "color" not in out
    assert out == "текст"


def test_script_with_attributes_and_case():
    html = '<SCRIPT type="text/javascript">alert(1)</SCRIPT>ok'
    assert _html_to_text(html) == "ok"


def test_multiline_script_removed():
    html = "a<script>\nline1();\nline2();\n</script>b"
    assert _html_to_text(html) == "a b"


def test_html_entities_decoded():
    assert _html_to_text("Tom &amp; Jerry &lt;3 &quot;hi&quot;&nbsp;&#39;x&#39;") == \
        'Tom & Jerry <3 "hi" \'x\''


def test_plain_text_passthrough():
    assert _html_to_text("просто текст") == "просто текст"
    assert _html_to_text("") == ""
