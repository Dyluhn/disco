"""Property-based / fuzz tests for the pure parsers/transformers — the code where
hand-picked cases miss edge cases. Hypothesis generates thousands of inputs
(truncation, escapes, unicode, control chars) and shrinks any failure.

Run: PYTHONPATH=. uv run pytest harness/tests/test_fuzz.py
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st
from disco.core.loop.stream_extract import extract_partial_string_field
from disco.retrieval.bundled_providers import _MarkdownExtractor, chunk_passages

# ---- extract_partial_string_field — the streaming partial-JSON parser --------


@given(st.text())
def test_extractor_never_raises_on_arbitrary_input(s: str):
    # it parses untrusted, possibly-truncated model output — it must NEVER throw.
    extract_partial_string_field(s, "content")
    extract_partial_string_field(s, "content", require_complete=True)


@given(st.text())
def test_complete_json_round_trips(value: str):
    # a COMPLETE {"content": value} must decode back to exactly `value`.
    raw = json.dumps({"path": "p", "content": value})
    assert extract_partial_string_field(raw, "content") == value
    # require_complete agrees once the closing quote is present
    assert extract_partial_string_field(raw, "content", require_complete=True) == value


@given(st.text(min_size=1), st.integers(min_value=0, max_value=400))
def test_streaming_prefix_is_a_prefix_of_the_full_value(value: str, cut: int):
    # THE watch-it-write invariant: as the JSON streams in (any prefix), the
    # decoded-so-far is always a PREFIX of the final decoded value — content only
    # grows, never diverges, never emits a broken trailing escape.
    raw = json.dumps({"content": value})
    partial = extract_partial_string_field(raw[:cut], "content")
    if partial is not None:
        assert value.startswith(partial)


# ---- chunk_passages — the extraction → citable-passage chunker ---------------


@given(st.text())
def test_chunk_passages_invariants(content: str):
    ps = chunk_passages("https://x.test", "Title", content)
    assert len(ps) <= 12  # never exceeds the cap
    assert len({p.id for p in ps}) == len(ps)  # ids are unique + stable
    for p in ps:
        assert p.source_url == "https://x.test"
        assert p.text.strip()  # no empty passages (they'd be dead citations)


# ---- the stdlib HTML→markdown extractor — never crash on arbitrary HTML ------


@given(st.text())
def test_markdown_extractor_never_raises(html: str):
    p = _MarkdownExtractor()
    p.feed(html)
    p.markdown()  # must produce a string for ANY input, never throw
