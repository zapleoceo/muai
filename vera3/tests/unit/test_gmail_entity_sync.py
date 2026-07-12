"""ingestor_gmail.poller.correspondent_of — who becomes the person entity."""
from __future__ import annotations

import os

os.environ.setdefault("GMAIL_CLIENT_ID", "test")
os.environ.setdefault("GMAIL_CLIENT_SECRET", "test")

from ingestor_gmail.poller import correspondent_of  # noqa: E402

ME = "zaporozec_d@itstep.org"


def test_received_uses_from_with_display_name():
    got = correspondent_of(ME, 'Maria Ivanova <maria@corp.com>', f"Dima <{ME}>")
    assert got == ("maria@corp.com", "Maria Ivanova")


def test_sent_uses_first_to():
    got = correspondent_of(ME, f"Dima <{ME}>", "Ольга <olga@x.com>, second@x.com")
    assert got == ("olga@x.com", "Ольга")


def test_bare_address_falls_back_to_local_part():
    got = correspondent_of(ME, "noreply@service.io", ME)
    assert got == ("noreply@service.io", "noreply")


def test_own_address_and_garbage_skipped():
    assert correspondent_of(ME, f"Dima <{ME}>", "") is None      # sent, no To
    assert correspondent_of(ME, "", "") is None                   # nothing
    assert correspondent_of(ME, "not-an-address", "") is None     # no @
    # self-addressed note (me → me) must not become a "person"
    assert correspondent_of(ME, ME, ME) is None
