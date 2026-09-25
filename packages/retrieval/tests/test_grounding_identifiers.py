"""Grounding preparation preserves technical text before independent verification."""

import json
import re
from pathlib import Path

import pytest
from disco.retrieval.grounding import (
    _claim_spans,
    _clean_premise,
    _plain,
    _retain_supported_claims,
    _verify_claims,
)
from disco.retrieval.models import Passage

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/grounding_sqlite_identifiers_f83.json").read_text()
)


class RecordingVerifier:
    def __init__(self):
        self.pairs = []

    def entail(self, premise, hypothesis):
        self.pairs.append((premise, hypothesis))
        return "contradict"

    def score(self, premise, hypothesis):
        return 0.1


@pytest.mark.parametrize("row", FIXTURE["rows"], ids=lambda row: str(row["claim_index"]))
def test_captured_sqlite_identifiers_reach_independent_verifier_unchanged(row):
    verifier = RecordingVerifier()
    source = Passage(
        id=row["source_id"], source_url=row["url"], source_title="SQLite", text=row["source_text"]
    )
    text = f"{row['claim']} [[{source.id}]]."
    checked = _verify_claims(text, {source.id: source}, verifier)
    assert verifier.pairs
    premise, hypothesis = verifier.pairs[0]
    assert row["identifier"] in premise
    for identifier in re.findall(r"\b\w+_\w+\b", row["claim"]):
        assert identifier in hypothesis
    assert checked[0]["verification_status"] == "contradicted"  # No classifier override.
    assert checked[0]["claim"]["cited_passage_ids"] == [source.id]
    span = _claim_spans(text)[0]
    assert text[span.start : span.end] == text


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "page_size sqlite3_rsync SQLITE_CHECKPOINT_RESTART",
            "page_size sqlite3_rsync SQLITE_CHECKPOINT_RESTART",
        ),
        ("`__init__` and `_private_name`", "__init__ and _private_name"),
        ("**`page_size`** and _sqlite3_rsync_", "page_size and sqlite3_rsync"),
        ("_normal_ __strong__ ***both***", "normal strong both"),
        ("**outer _inner_**", "outer inner"),
        (r"page\_size and \_literal\_", "page_size and _literal_"),
        ("https://example.test/a_b?q=x_y#part_z", "https://example.test/a_b?q=x_y#part_z"),
        ("`3*4 > 2` and C#", "3*4 > 2 and C#"),
        ("# Heading\n> **quoted** _text_", "Heading\nquoted text"),
    ],
)
def test_markdown_delimiters_do_not_consume_literal_identifier_characters(text, expected):
    assert _plain(text) == expected
    assert _clean_premise(text) == expected


def test_link_labels_escaped_underscores_and_url_fragments_preserve_meaning():
    text = (
        r"[page\_size](https://example.test/a_b#c_d) and "
        "https://example.test/a_b#c_d ![image](https://example.test/image_x.png)"
    )
    assert _clean_premise(text).strip() == "page_size and https://example.test/a_b#c_d"


def test_identifier_table_headers_keep_exact_row_offsets_and_supported_rendering():
    text = "| `page_size` | **wal_mode** |\n| --- | --- |\n| 4096 [[s]] | _enabled_ |\n"
    span = _claim_spans(text)[0]
    assert span.text == "page_size: 4096; wal_mode: _enabled_"
    assert _plain(span.text) == "page_size: 4096; wal_mode: enabled"
    assert text[span.start : span.end] == "| 4096 [[s]] | _enabled_ |"
    assert span.cited_passage_ids == ("s",)
    kept = _retain_supported_claims(
        text,
        [{"claim": {"text": span.text, "cited_passage_ids": ["s"]}, "verdict": "supported"}],
    )
    assert kept == text.strip()
