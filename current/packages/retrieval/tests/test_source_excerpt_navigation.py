"""Navigation-aware relevance windows."""

from disco.retrieval.source_excerpts import _navigation_line_indexes, relevant_excerpt


def test_dot_leader_contents_does_not_beat_specification_body() -> None:
    contents = "Table of Contents\n" + "\n".join(
        [
            "1.  Introduction  . . . . . . . . . . . . . .   3",
            "5.  Performance Considerations  . . . . . . .   9",
            "8.  Security Considerations  . . . . . . . .  11",
            "9.  References  . . . . . . . . . . . . . . .  12",
        ]
    )
    body = (
        "\n5. Performance Considerations\n"
        "TLS session reuse reduces handshake latency and implementations should "
        "measure throughput.\n"
        "\n8. Security Considerations\n"
        "TLS protects confidentiality and integrity while endpoint authentication "
        "prevents interception.\n"
    )
    source = contents + "\n" + ("Background prose. " * 18) + body

    excerpt = relevant_excerpt(
        source,
        "Find the performance considerations section and the security considerations section",
        max_chars=220,
    )

    assert "Table of Contents" not in excerpt.text
    assert "TLS session reuse" in excerpt.text
    assert excerpt.text == source[excerpt.start : excerpt.end]


def test_markdown_anchor_contents_does_not_beat_document_body() -> None:
    contents = "# Contents\n" + "\n".join(
        [
            "- [Performance considerations](#performance)",
            "- [Security considerations](#security)",
            "- [References](#references)",
            "- [Appendix](#appendix)",
        ]
    )
    body = (
        "\n## Performance considerations\n"
        "The measured performance consideration is connection latency.\n"
        "\n## Security considerations\n"
        "The security consideration is authenticated confidential transport.\n"
    )
    source = contents + "\n" + ("Background prose. " * 20) + body

    excerpt = relevant_excerpt(
        source,
        "performance considerations security considerations",
        max_chars=220,
    )

    assert "# Contents" not in excerpt.text
    assert "measured performance" in excerpt.text
    assert excerpt.text == source[excerpt.start : excerpt.end]


def test_multiple_links_in_html_toc_rows_do_not_beat_document_body() -> None:
    contents = "Table of Contents\n" + "\n".join(
        [
            "5[](https://example.test/spec#section-5). Performance considerations "
            ". . . 9[](https://example.test/spec#page-9)",
            "8[](https://example.test/spec#section-8). Security considerations "
            ". . . 11[](https://example.test/spec#page-11)",
            "9[](https://example.test/spec#section-9). References "
            ". . . 12[](https://example.test/spec#page-12)",
            "A[](https://example.test/spec#appendix-A). Appendix "
            ". . . 16[](https://example.test/spec#page-16)",
        ]
    )
    body = (
        "\n5[](https://example.test/spec#section-5).  Performance considerations\n"
        "Measured performance requires connection reuse to reduce startup latency.\n"
        "\n8[](https://example.test/spec#section-8).  Security considerations\n"
        "Authenticated transport protects confidentiality from active interception.\n"
    )
    source = contents + "\n" + ("Background prose. " * 20) + body

    excerpt = relevant_excerpt(
        source,
        "performance considerations security considerations",
        max_chars=240,
    )

    assert "Table of Contents" not in excerpt.text
    assert "Measured performance" in excerpt.text or "Authenticated transport" in excerpt.text
    assert excerpt.text == source[excerpt.start : excerpt.end]


def test_ordinary_prose_with_multiple_links_is_not_navigation() -> None:
    source = (
        "See [performance guidance](https://example.test/performance#details) and "
        "[security guidance](https://example.test/security#details) in this prose.\n"
        "The linked material is discussed here for context.\n"
    ) * 12

    excerpt = relevant_excerpt(source, "performance security", max_chars=220)

    assert _navigation_line_indexes(source) == set()
    assert "this prose" in excerpt.text
    assert excerpt.text == source[excerpt.start : excerpt.end]


def test_navigation_only_source_keeps_legacy_bounded_fallback() -> None:
    source = (
        "Contents\n"
        "1. [Performance considerations](#performance)\n"
        "2. [Security considerations](#security)\n"
        "3. [References](#references)\n"
    )

    excerpt = relevant_excerpt(source, "performance security", max_chars=100)

    assert excerpt.start == 0
    assert len(excerpt.text) == 100
    assert excerpt.text == source[excerpt.start : excerpt.end]


def test_numeric_data_table_is_not_classified_as_navigation() -> None:
    source = ("Introductory prose. " * 18) + (
        "Metric | Performance | Security\n"
        "Latency | 20 ms | protected\n"
        "Throughput | 100 requests/s | authenticated\n"
    )

    excerpt = relevant_excerpt(source, "performance security 20 ms", max_chars=180)

    assert "20 ms" in excerpt.text
    assert excerpt.text == source[excerpt.start : excerpt.end]


def test_section_heading_outweighs_introductory_mentions_of_multiple_sections() -> None:
    source = (
        "1.  Introduction\n"
        "This guide offers performance considerations and security considerations.\n"
        + "General introduction. " * 35
        + "\n5.  Performance Considerations\n"
        + "Session startup incurs additional latency; connection reuse reduces that cost.\n"
        + "Further operational details. " * 20
        + "\n8.  Security Considerations\n"
        + "Authentication is required to protect against an active interceptor.\n"
    )
    excerpt = relevant_excerpt(
        source,
        "Find the performance considerations section and the security considerations section",
        max_chars=240,
    )
    assert (
        "Session startup incurs additional latency" in excerpt.text
        or "Authentication is required" in excerpt.text
    )
    assert excerpt.text == source[excerpt.start : excerpt.end]


def test_requested_section_beats_broader_document_title():
    source = (
        "# Document 142: Usage Profiles for Several Transports and Deployment Settings\n"
        "This document discusses usage profiles.\n"
        + "Background material. " * 120
        + "\n5.  Usage Profiles\n"
        + "Strict operation authenticates the peer and refuses insecure fallback.\n"
        + "Further qualifications. " * 20
    )
    excerpt = relevant_excerpt(source, "Find the Usage Profiles section", max_chars=400)
    assert "Strict operation authenticates the peer" in excerpt.text
    assert excerpt.text == source[excerpt.start : excerpt.end]


def test_annotated_section_read_includes_body_and_preserves_offsets():
    def annotated(number, body):
        return f"  {number}[](https://example.test/spec#line-{number}) {body}\n"

    source = annotated(1, "1.  Introduction")
    source += annotated(2, "Consult the security considerations for restrictions.")
    source += "".join(annotated(i, "General background material.") for i in range(3, 30))
    source += annotated(30, "8[](https://example.test/spec#section-8).  Security Considerations")
    source += annotated(31, "Authentication must succeed before protected data is transmitted.")
    source += annotated(32, "An unauthenticated peer must be refused.")
    source += "".join(annotated(i, "Further discussion.") for i in range(33, 40))
    excerpt = relevant_excerpt(source, "Find the Security Considerations section", max_chars=400)
    assert "Authentication must succeed before protected data" in excerpt.text
    assert excerpt.text == source[excerpt.start : excerpt.end]
