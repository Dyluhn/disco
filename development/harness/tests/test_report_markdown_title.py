"""Report artifact title parity for the acceptance harness."""

from development.harness.research_harness_parts._run import render_report_markdown


def test_legacy_report_markdown_uses_plain_report_title() -> None:
    rendered = render_report_markdown(
        {"query": "Acceptance report", "summary": "Summary", "legacy_replay": True}
    )

    assert rendered.splitlines()[0] == "# Acceptance report"
