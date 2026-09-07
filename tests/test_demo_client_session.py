"""The demo client must not take a session key from the page's address.

The security review of 2026-09-07 demonstrated the hole: `readSession()` preferred a `?session=`
parameter over localStorage, so a link someone was sent seated their conversation on a key its
sender already held, and the reviewer read a customer's own message back from a second
connection. The same parameter is written verbatim into every access log between the browser and
the app, which is the half of phase W's finding W9 that was never fixed.

The key is the whole of this channel's access control (`support_core/channels/web_chat.py`), so
this is not a demo-only concern: the page is what a first deployment copies.

These tests read the shipped JavaScript rather than executing it. That is a real limitation and
worth stating plainly - they check the source says the right thing, not that a browser does it -
but the alternative is a headless browser in the suite for one file, and the failure mode being
guarded against is someone reintroducing the URL read, which is visible in the source.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CLIENT = Path(__file__).resolve().parents[1] / "support_core" / "api" / "static" / "app.js"


@pytest.fixture(scope="module")
def source() -> str:
    return CLIENT.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """The file with block comments removed, so a comment *about* the URL is not a match."""
    return re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)


def test_the_session_key_is_never_read_from_the_url(source: str) -> None:
    """The reviewer's exact hole: `?session=<attacker's key>` adopted as the conversation."""
    code = _code_only(source)
    assert 'get("session")' not in code, (
        "the client reads a session key from the query string; a crafted link then seats a "
        "customer's conversation on a key its sender already holds"
    )
    assert "URLSearchParams" not in code or "delete(" in code, (
        "the query string is parsed for something other than discarding the session parameter"
    )


def test_the_session_parameter_is_stripped_from_the_address_bar(source: str) -> None:
    """A key that reaches the URL must not linger in history, bookmarks or referrers."""
    code = _code_only(source)
    assert "discardSessionInUrl" in code
    assert 'params.delete("session")' in code
    assert "history.replaceState" in code


def test_the_stripper_runs_before_the_session_is_read(source: str) -> None:
    """Order matters: reading first would adopt the key and only then tidy the address."""
    code = _code_only(source)
    strip_call = code.index("discardSessionInUrl();")
    read_call = code.index("state.session = readSession();")
    assert strip_call < read_call


def test_the_key_still_survives_a_reload(source: str) -> None:
    """The fix must not cost the property it protects: DESIGN.md 7.2's resume across connections."""
    code = _code_only(source)
    assert "localStorage.getItem(SESSION_STORAGE_KEY)" in code
    assert "localStorage.setItem(SESSION_STORAGE_KEY" in code
