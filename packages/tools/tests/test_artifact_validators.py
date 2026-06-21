"""Tests for artifact validators."""

import os
import tempfile

from disco.tools.verify.artifact_validators import (
    validate_audio,
    validate_deck_file,
    validate_pdf,
    validate_sheet,
)


def test_validate_pdf_ok():
    """Test that a valid PDF returns an empty problems list."""
    result = validate_pdf("packages/tools/tests/fixtures/artifacts/sample_report.pdf")
    assert result == []


def test_validate_pdf_missing():
    """Test that a missing PDF returns a non-empty problems list."""
    result = validate_pdf("/no/such.pdf")
    assert len(result) > 0


def test_validate_sheet_missing():
    """Test that a missing sheet returns a non-empty problems list."""
    result = validate_sheet("/no/such.xlsx")
    assert len(result) > 0


# ---------------------------------------------------------------------------
# New robustness tests: missing binary → return finding, not exception
# ---------------------------------------------------------------------------


def test_validate_pdf_missing_binary(monkeypatch):
    """validate_pdf returns a problem when pdfinfo is not installed (no crash)."""
    import subprocess

    original_run = subprocess.run

    def raise_fnf(args, **kwargs):
        if args[0] == "pdfinfo":
            raise FileNotFoundError("pdfinfo: No such file or directory")
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", raise_fnf)
    problems = validate_pdf("/any/path.pdf")
    assert len(problems) > 0, "expected at least one problem when pdfinfo is missing"
    assert not any("Traceback" in p for p in problems), (
        "problems list must not contain a traceback — validator should not raise"
    )


def test_validate_audio_missing_binary(monkeypatch):
    """validate_audio returns a problem when ffprobe is not installed (no crash)."""
    import subprocess

    original_run = subprocess.run

    def raise_fnf(args, **kwargs):
        if args[0] == "ffprobe":
            raise FileNotFoundError("ffprobe: No such file or directory")
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", raise_fnf)
    problems = validate_audio("/any/path.mp3")
    assert len(problems) > 0, "expected at least one problem when ffprobe is missing"
    assert not any("Traceback" in p for p in problems), (
        "problems list must not contain a traceback — validator should not raise"
    )


def test_validate_deck_file_corrupt_pptx():
    """validate_deck_file returns a problem for a corrupt .pptx (no crash)."""
    with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as f:
        f.write(b"this is not a valid zip/pptx file at all")
        corrupt_path = f.name
    try:
        problems = validate_deck_file(corrupt_path)
        assert len(problems) > 0, (
            "expected at least one problem for a corrupt pptx file"
        )
        assert not any("Traceback" in p for p in problems), (
            "problems list must not contain a traceback — validator should not raise"
        )
    finally:
        os.unlink(corrupt_path)
