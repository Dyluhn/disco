"""The single-front-door same-origin rule (origin_matches_request_host /
origin_permitted).

With nginx proxying both servers under the page's own origin (2026-07-09), a
legitimate browser request's Origin netloc EQUALS the forwarded Host — for ANY
hostname, with zero origin config. These tests pin the exact comparison
semantics, because this check now gates every authenticated request in the
front-door deploy."""

from __future__ import annotations

import pytest
from disco.core.auth import (
    localhost_auto_pair_allowed,
    origin_allowed,
    origin_matches_request_host,
    origin_permitted,
    request_traversed_proxy,
    session_cookie_secure,
)


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
    # Allowlisted split-origin layout still passes with NO host match…
    assert origin_allowed("http://localhost:8088")
    assert origin_permitted("http://localhost:8088", "testserver")
    # …and an unlisted front-door origin passes ONLY via the same-host rule.
    assert not origin_allowed("http://mybox.tail1234.ts.net:8088")
    assert origin_permitted("http://mybox.tail1234.ts.net:8088", "mybox.tail1234.ts.net:8088")
    assert not origin_permitted("http://mybox.tail1234.ts.net:8088", "other.host:8088")


def test_vite_dev_port_is_not_trusted_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """5173 is the Vite DEFAULT port, i.e. one any unrelated local project can
    occupy. Pre-trusting it made every such page same-site with the app AND a
    permitted CORS origin, so it could read the CSRF token from the public
    /api/auth/session and issue authenticated writes. Opt in explicitly instead."""
    monkeypatch.delenv("DISCO_FRONTEND_ORIGINS", raising=False)
    monkeypatch.delenv("PMX_FRONTEND_ORIGINS", raising=False)
    assert not origin_allowed("http://localhost:5173")
    assert not origin_allowed("http://127.0.0.1:5173")
    # …and it is NOT reachable through the same-host rule either.
    assert not origin_permitted("http://localhost:5173", "localhost:8800")
    # The documented split-origin dev workflow opts in with the existing env var.
    monkeypatch.setenv("DISCO_FRONTEND_ORIGINS", "http://localhost:5173")
    assert origin_allowed("http://localhost:5173")


# ---- localhost auto-pair (trust follows the bind) -----------------------------

LOCAL = "http://localhost:8088"
LOCAL_HOST = "localhost:8088"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for var in (
        "DISCO_BIND",
        "PMX_BIND",
        "DISCO_HOST",
        "PMX_HOST",
        "DISCO_AUTH_LOCAL_AUTO_PAIR",
        "PMX_AUTH_LOCAL_AUTO_PAIR",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_auto_pair_on_loopback_bound_default(clean_env: pytest.MonkeyPatch) -> None:
    # The zero-config posture: no bind declared → loopback → localhost page pairs.
    assert localhost_auto_pair_allowed(LOCAL, LOCAL_HOST)
    assert localhost_auto_pair_allowed("http://127.0.0.1:8088", "127.0.0.1:8088")
    assert localhost_auto_pair_allowed("http://[::1]:8088", "[::1]:8088")


def test_auto_pair_disabled_when_exposed(clean_env: pytest.MonkeyPatch) -> None:
    # DISCO_BIND=0.0.0.0 is a deliberate exposure → the token requirement returns,
    # even for a (forgeable, from non-browsers) localhost Origin.
    clean_env.setenv("DISCO_BIND", "0.0.0.0")
    assert not localhost_auto_pair_allowed(LOCAL, LOCAL_HOST)


def test_auto_pair_compose_bind_wins_over_container_host(clean_env: pytest.MonkeyPatch) -> None:
    # In compose the container-internal DISCO_HOST must be 0.0.0.0 (nginx needs to
    # reach it) — that says NOTHING about exposure; DISCO_BIND decides.
    clean_env.setenv("DISCO_HOST", "0.0.0.0")
    clean_env.setenv("DISCO_BIND", "127.0.0.1")
    assert localhost_auto_pair_allowed(LOCAL, LOCAL_HOST)


def test_auto_pair_host_process_exposure_counts(clean_env: pytest.MonkeyPatch) -> None:
    # No DISCO_BIND (plain host-process run): the server's own bind address IS the
    # exposure — 0.0.0.0 without a bind declaration must NOT auto-pair.
    clean_env.setenv("DISCO_HOST", "0.0.0.0")
    assert not localhost_auto_pair_allowed(LOCAL, LOCAL_HOST)


def test_auto_pair_requires_localhost_family_origin(clean_env: pytest.MonkeyPatch) -> None:
    # A same-host but non-localhost origin (tailnet/LAN page) never auto-pairs…
    assert not localhost_auto_pair_allowed(
        "http://mybox.tail1234.ts.net:8088", "mybox.tail1234.ts.net:8088"
    )
    # …and neither does a DNS-rebound page (its Origin is the attacker's name).
    assert not localhost_auto_pair_allowed("http://evil.example:8088", "evil.example:8088")


def test_auto_pair_requires_own_front_door_page(clean_env: pytest.MonkeyPatch) -> None:
    # Another local app's page (different port) must not ride the shortcut.
    assert not localhost_auto_pair_allowed("http://localhost:3000", LOCAL_HOST)


def test_auto_pair_explicit_override(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DISCO_AUTH_LOCAL_AUTO_PAIR", "0")
    assert not localhost_auto_pair_allowed(LOCAL, LOCAL_HOST)  # forced off
    clean_env.setenv("DISCO_AUTH_LOCAL_AUTO_PAIR", "1")
    clean_env.setenv("DISCO_BIND", "0.0.0.0")
    assert localhost_auto_pair_allowed(LOCAL, LOCAL_HOST)  # forced on (explicit)


def test_auto_pair_refused_behind_a_proxy(clean_env: pytest.MonkeyPatch) -> None:
    # codex 2026-07-09: a loopback-bound instance exposed via a Host-preserving
    # tunnel (ngrok/cloudflared/CF Access/reverse proxy) must NOT tokenless-mint.
    # Every such proxy stamps a forwarding header; via_proxy=True refuses — even
    # under the explicit force-on (a proxy in front == exposed).
    assert not localhost_auto_pair_allowed(LOCAL, LOCAL_HOST, via_proxy=True)
    clean_env.setenv("DISCO_AUTH_LOCAL_AUTO_PAIR", "1")
    assert not localhost_auto_pair_allowed(LOCAL, LOCAL_HOST, via_proxy=True)


def test_request_traversed_proxy_detects_forwarding_headers() -> None:
    # Starlette Headers are case-insensitive; a plain dict get() is not, so the
    # helper is exercised with lowercase keys (what Starlette normalizes to).
    for header in (
        "x-forwarded-for",
        "x-forwarded-host",
        "forwarded",
        "cf-connecting-ip",
        "x-real-ip",
    ):
        assert request_traversed_proxy({header: "1.2.3.4"})
    # The front door's OWN header (X-Forwarded-Proto) must NOT trip it — else the
    # legit local case would always be refused.
    assert not request_traversed_proxy({"x-forwarded-proto": "https"})
    assert not request_traversed_proxy({})


# ---- session cookie Secure flag (SEC-6a) ------------------------------------


def test_session_cookie_not_secure_on_plain_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser DISCARDS a Secure cookie on an http origin, so first-run
    pairing on plain localhost must stay unflagged — including when the
    operator has also declared an https public URL for the remote front door."""
    monkeypatch.delenv("DISCO_PUBLIC_UI_URL", raising=False)
    monkeypatch.delenv("PMX_PUBLIC_UI_URL", raising=False)
    assert not session_cookie_secure("http", None, "localhost:8088")
    assert not session_cookie_secure("http", "http", "127.0.0.1:8000")
    monkeypatch.setenv("DISCO_PUBLIC_UI_URL", "https://box.tail1234.ts.net")
    assert not session_cookie_secure("http", None, "localhost:8088")
    assert not session_cookie_secure("http", None, "127.0.0.1:8088")


def test_session_cookie_secure_behind_a_tls_front_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real remote shape: TLS terminated by a proxy (Tailscale HTTPS,
    nginx, cloudflared) that forwards to loopback http. Hardcoding
    secure=False shipped the session cookie over the public leg unflagged."""
    monkeypatch.delenv("DISCO_PUBLIC_UI_URL", raising=False)
    monkeypatch.delenv("PMX_PUBLIC_UI_URL", raising=False)
    # 1. the forwarded scheme header the front-door nginx sets
    assert session_cookie_secure("http", "https", "box.tail1234.ts.net")
    assert session_cookie_secure("http", "https, http", "box.tail1234.ts.net")
    # 2. TLS terminated by the app itself
    assert session_cookie_secure("https", None, "box.example.com")
    # 3. a proxy that forwards no scheme header — the declared public URL
    assert not session_cookie_secure("http", None, "box.tail1234.ts.net")
    monkeypatch.setenv("DISCO_PUBLIC_UI_URL", "https://box.tail1234.ts.net")
    assert session_cookie_secure("http", None, "box.tail1234.ts.net")
    # An http public URL keeps it off.
    monkeypatch.setenv("DISCO_PUBLIC_UI_URL", "http://box.lan:8088")
    assert not session_cookie_secure("http", None, "box.lan:8088")


def test_session_cookie_secure_defaults_off_with_no_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISCO_PUBLIC_UI_URL", raising=False)
    monkeypatch.delenv("PMX_PUBLIC_UI_URL", raising=False)
    assert not session_cookie_secure()
