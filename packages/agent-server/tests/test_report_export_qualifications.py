"""Downloads retain the qualifications of the exact stored report."""

import json
from pathlib import Path

import pytest
from disco.agent_server.report_export import _build_pdf_html, serialize_markdown, serialize_pdf
from disco.core import ReportEvent
from disco.core.brand import resolve_theme


def report():
    return ReportEvent.model_validate(
        json.loads((Path(__file__).parent / "fixtures/report_qualifications.json").read_text())
    )


def test_qualified_exports_keep_findings_and_escape_metadata():
    value = report()
    md = serialize_markdown(value)
    html = _build_pdf_html(value, None, resolve_theme("disco", "light"))
    html = html.split("<body>", 1)[1]
    for rendered in (md, html):
        assert "Evidence and review qualifications" in rendered
        assert "2 supported, 1 possible contradictions, 3 unresolved, 4 not checked" in rendered
        assert "it is not a contradiction" in rendered
        assert "The causal direction needs qualification" in rendered
        assert "Unverified: The other participant must fail" in rendered
        assert "Not researched: Long-term behavior" in rendered
        assert "[[p1]]" not in rendered
        assert "<img src=" not in rendered
        assert "&lt;img src=" in rendered
    assert md.index("Evidence and review qualifications") < md.index("## Executive Summary")
    assert html.index("Evidence and review qualifications") < html.index('class="exec-summary"')
    assert r"qualification \[1\]" in md
    assert "qualification [1]" in html
    assert "**not markup**" in html
    assert r"\*\*not markup\*\*" in md


@pytest.mark.parametrize("outcome", ["unavailable", "incomplete", "verdict"])
def test_review_status_survives_without_measurements(outcome):
    value = report().model_copy(update={"meta": {"review_outcome": outcome}})
    md = serialize_markdown(value)
    assert ("editorial review was unavailable" in md) == (outcome == "unavailable")
    assert ("Review found unresolved issues" in md) == (outcome == "incomplete")
    assert ("Evidence and review qualifications" in md) == (outcome != "verdict")
    assert "Automated evidence check" not in md


def test_legacy_and_malformed_metadata_do_not_invent_counts():
    value = report().model_copy(
        update={
            "meta": {
                "unverified_sentences": None,
                "residual_deficiencies": ["Older finding", 12],
                "grounding_counts": {
                    "supported": True,
                    "contradicted": 0,
                    "unresolved": 0,
                    "unavailable": 0,
                },
                "review_notes": "not a list",
                "untested_angles": [False],
            }
        }
    )
    md = serialize_markdown(value)
    assert "Unverified: Older finding" in md
    assert "Automated evidence check" not in md
    assert "not a list" not in md
    assert "Not researched" not in md


def test_pdf_bytes_include_qualifications(tmp_path):
    import subprocess

    path = tmp_path / "qualified.pdf"
    path.write_bytes(serialize_pdf(report()))
    text = subprocess.check_output(["pdftotext", str(path), "-"], text=True)
    assert "Evidence and review qualifications" in text
    assert "Review found unresolved issues" in text
    assert "2 supported, 1 possible contradictions" in text
    assert "The causal direction needs qualification [1]" in text
    assert "Not researched: Long-term behavior" in text
