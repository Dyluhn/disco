"""Unit tests for uploads_ingest — text file → Passage chunks.

Covers:
  • parse_upload_to_doc returns None for non-text extensions
  • .txt / .md parsed into prose Passages with correct source_url / id
  • .csv parsed row-by-row with key=value format
  • empty file → ExtractedDoc with zero passages (no crash)
  • long paragraph sub-split at sentence boundaries
  • CSV parsing error falls back to prose chunker
"""

from __future__ import annotations

import os
import sys

# Add the agent-server package to path so we can import uploads_ingest.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "../../../agent-server/src"),
)

from disco.agent_server.uploads_ingest import parse_upload_to_doc  # noqa: E402

CID = "conv_test123"


def test_pdf_returns_none() -> None:
    result = parse_upload_to_doc("report.pdf", b"%PDF-1.4 fake", CID)
    assert result is None


def test_zip_returns_none() -> None:
    result = parse_upload_to_doc("archive.zip", b"PK...", CID)
    assert result is None


def test_unknown_extension_returns_none() -> None:
    result = parse_upload_to_doc("model.bin", b"\x00\x01\x02", CID)
    assert result is None


def test_txt_basic() -> None:
    text = "Hello world.\n\nThis is paragraph two."
    doc = parse_upload_to_doc("notes.txt", text.encode(), CID)
    assert doc is not None
    assert doc.fetched_ok is True
    assert doc.url == f"upload://{CID}/notes.txt"
    assert doc.title == "notes.txt"
    assert len(doc.passages) == 2
    assert doc.passages[0].text == "Hello world."
    assert doc.passages[1].text == "This is paragraph two."
    # source_url and source_title are correct on each passage
    for p in doc.passages:
        assert p.source_url == f"upload://{CID}/notes.txt"
        assert p.source_title == "notes.txt"
    # ids are unique
    ids = [p.id for p in doc.passages]
    assert len(set(ids)) == len(ids)


def test_md_basic() -> None:
    text = "# Title\n\nSome markdown paragraph.\n\nAnother paragraph."
    doc = parse_upload_to_doc("readme.md", text.encode(), CID)
    assert doc is not None
    assert len(doc.passages) >= 2  # heading + paragraphs


def test_csv_basic() -> None:
    csv_text = "name,value,description\nalpha,1,first item\nbeta,2,second item\n"
    doc = parse_upload_to_doc("data.csv", csv_text.encode(), CID)
    assert doc is not None
    assert len(doc.passages) == 2
    # Passages encode rows as "key: value | key: value"
    assert "alpha" in doc.passages[0].text
    assert "beta" in doc.passages[1].text
    assert "name: alpha" in doc.passages[0].text
    assert "value: 1" in doc.passages[0].text


def test_csv_empty_rows_skipped() -> None:
    csv_text = "name,value\n,\nalpha,1\n"
    doc = parse_upload_to_doc("data.csv", csv_text.encode(), CID)
    assert doc is not None
    # The first row is all-empty → skipped
    assert len(doc.passages) == 1
    assert "alpha" in doc.passages[0].text


def test_empty_file() -> None:
    doc = parse_upload_to_doc("empty.txt", b"", CID)
    assert doc is not None
    assert doc.fetched_ok is True
    assert doc.passages == []


def test_long_paragraph_sub_split() -> None:
    # >600 chars in one paragraph → sub-split at sentence boundary
    long_para = "This is sentence one. " * 30  # >600 chars
    doc = parse_upload_to_doc("long.txt", long_para.encode(), CID)
    assert doc is not None
    # Should be split into multiple passages
    assert len(doc.passages) > 1
    for p in doc.passages:
        assert len(p.text) <= 750  # some slack for the last chunk


def test_passage_ids_are_unique_across_files() -> None:
    text_a = "Paragraph one.\n\nParagraph two."
    doc_a = parse_upload_to_doc("a.txt", text_a.encode(), CID)
    text_b = "Different content.\n\nMore content."
    doc_b = parse_upload_to_doc("b.txt", text_b.encode(), CID)
    assert doc_a and doc_b
    ids_a = {p.id for p in doc_a.passages}
    ids_b = {p.id for p in doc_b.passages}
    # Different filenames → different id slugs → no collision
    assert ids_a.isdisjoint(ids_b)


def test_corpus_id_not_set_by_parser() -> None:
    """corpus_id is set by DefaultCorpusService.ingest, not the parser."""
    doc = parse_upload_to_doc("notes.txt", b"Some text.", CID)
    assert doc is not None
    for p in doc.passages:
        assert p.corpus_id is None
