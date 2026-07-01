"""[REL-RC-E] AgentErrorEvent.detail carries the tool's recovery guidance to the model.

`error` stays the canonical CODE (the stuck-detector + classifier key on it); `detail` carries the
human-readable text so a domain error like bad_range shows the valid range instead of a bare code.
"""
from __future__ import annotations

from disco.core.events import AgentErrorEvent


def test_detail_appended_when_it_adds_info():
    e = AgentErrorEvent(
        error="bad_range",
        detail="bad range [40,41] for index.html (12 lines). start_line must be 1..13.",
    )
    msg = e.to_llm_message()
    assert msg.content.startswith("ERROR: bad_range")
    assert "(12 lines)" in msg.content
    assert "1..13" in msg.content


def test_no_detail_is_bare_error():
    e = AgentErrorEvent(error="bad_range")
    assert e.to_llm_message().content == "ERROR: bad_range"


def test_detail_equal_to_error_not_duplicated():
    e = AgentErrorEvent(error="tool failed", detail="tool failed")
    assert e.to_llm_message().content == "ERROR: tool failed"


def test_preformatted_error_ignores_detail():
    # a <system-reminder>-wrapped error is rendered as-is; detail must not be appended
    e = AgentErrorEvent(error="<system-reminder>stop</system-reminder>", detail="ignored")
    assert e.to_llm_message().content == "<system-reminder>stop</system-reminder>"
