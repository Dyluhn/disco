"""The single-front-door same-origin rule (origin_matches_request_host /
origin_permitted).

With nginx proxying both servers under the page's own origin (2026-07-09), a
legitimate browser request's Origin netloc EQUALS the forwarded Host — for ANY
hostname, with zero origin config. These tests pin the exact comparison
semantics, because this check now gates every authenticated request in the
front-door deploy."""

from __future__ import annotations

from disco.core.auth import origin_allowed, origin_matches_request_host, origin_permitted


def test_same_netloc_matches() -> None:
    assert origin_matches_request_host("http://100.81.82.115:8088", "100.81.82.115:8088")
    assert origin_matches_request_host("https://disco.example.com", "disco.example.com")
    assert origin_matches_request_host("http://localhost:8088", "localhost:8088")


def test_netloc_comparison_is_case_insensitive() -> None:
    assert origin_matches_request_host("http://Disco.Example.com:8088", "disco.example.com:8088")


def test_port_mismatch_refused() -> None:
    # Origin carries the PAGE's port; the Host must carry the same one — this is
    # why nginx forwards $http_host (with port), not $host (stripped).
    assert not origin_matches_request_host("http://host.example:8088", "host.example:9999")
    assert not origin_matches_request_host("http://host.example:8088", "host.example")


def test_cross_site_origin_refused() -> None:
    # The browser stamps the ATTACKER's origin on a cross-site request — it can
    # never equal the Host the request was sent to.
    assert not origin_matches_request_host("http://evil.example", "disco.example.com:8088")


def test_degenerate_origins_refused() -> None:
    # "null" (sandboxed iframe / file://) has no netloc; empty/missing likewise.
    assert not origin_matches_request_host("null", "host.example:8088")
    assert not origin_matches_request_host("", "host.example:8088")
    assert not origin_matches_request_host(None, "host.example:8088")
    assert not origin_matches_request_host("http://host.example:8088", None)
    assert not origin_matches_request_host("http://host.example:8088", "")


def test_origin_permitted_is_allowlist_or_same_host() -> None:
    # Allowlisted split-origin dev layout still passes with NO host match…
    assert origin_allowed("http://localhost:5173")
    assert origin_permitted("http://localhost:5173", "testserver")
    # …and an unlisted front-door origin passes ONLY via the same-host rule.
    assert not origin_allowed("http://mybox.tail1234.ts.net:8088")
    assert origin_permitted("http://mybox.tail1234.ts.net:8088", "mybox.tail1234.ts.net:8088")
    assert not origin_permitted("http://mybox.tail1234.ts.net:8088", "other.host:8088")
