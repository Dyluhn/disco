"""Focused display-only citation normalization tests."""

from disco.agent_server.report_export import _sub_citation_markers


def test_adjacent_same_source_markers_collapse_without_touching_raw_text() -> None:
    numbers = {"p1": 3, "p2": 3, "p3": 4}

    rendered = _sub_citation_markers(
        "Literal [3] [3]; cites [[p1]] [[p2]] [[p3]]; "
        "non-adjacent [[p1]] text [[p2]].",
        numbers,
    )

    assert rendered == "Literal [3] [3]; cites [3] [4]; non-adjacent [3] text [3]."
