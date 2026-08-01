"""The CORS allowlist + single-front-door same-host + localhost auto-pair rules.

Extracted verbatim from ``auth.py`` to reduce module size; the parent module
re-imports every name here unchanged (see ``auth_parts/__init__.py``).
"""

from __future__ import annotations

from typing import Any

from ..env import disco_env

_DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:8800",
    "http://127.0.0.1:8800",
    # The compose self-host frontend (nginx :8088). Missing from this list, even
    # the documented localhost quickstart had its API responses CORS-blocked —
    # found live 2026-07-09 on the first browser test of a fresh compose deploy.
    "http://localhost:8088",
    "http://127.0.0.1:8088",
)


def allowed_frontend_origins() -> tuple[str, ...]:
    raw = disco_env("FRONTEND_ORIGINS", "")
    if raw:
        origins = [part.strip().rstrip("/") for part in raw.replace(",", " ").split()]
        return tuple(origin for origin in origins if origin)
    # Zero-config remote access: when the deployment declares its public UI URL
    # (DISCO_PUBLIC_UI_URL — the same value the boot banner prints), that origin
    # is allowed alongside the localhost defaults. Without this, a LAN/tailnet
    # browser passes the env.js host derivation but then has every API response
    # CORS-blocked — the second layer of the 2026-07-09 remote fresh-install bug.
    public_ui = (disco_env("PUBLIC_UI_URL", "") or "").strip().rstrip("/")
    if public_ui:
        return (*_DEFAULT_ALLOWED_ORIGINS, public_ui)
    return _DEFAULT_ALLOWED_ORIGINS


def origin_allowed(origin: str | None) -> bool:
    return bool(origin and origin.rstrip("/") in allowed_frontend_origins())


def origin_matches_request_host(origin: str | None, request_host: str | None) -> bool:
    """Same-origin check for the single-front-door deploy: nginx proxies BOTH
    servers under the page's own origin and forwards the browser's Host header
    untouched, so a legitimate request's Origin netloc EQUALS the request Host.
    This is what makes any hostname (localhost, LAN IP, tailnet name, domain)
    work with zero origin config. A cross-site page can't match: the browser
    stamps ITS origin (attacker.example), which never equals the Host it posted
    to. Non-browser clients can forge both headers, but they always could —
    the session cookie remains the actual authentication; origin checks are the
    CSRF layer. Netloc comparison is case-insensitive, scheme-agnostic."""
    if not origin or not request_host:
        return False
    from urllib.parse import urlsplit

    try:
        netloc = urlsplit(origin.strip().rstrip("/")).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc.lower() == request_host.strip().lower()


def origin_permitted(origin: str | None, request_host: str | None = None) -> bool:
    """The general request-gating check: the static allowlist (split-origin dev
    layout) OR the same-host front-door rule. NOT for the tokenless auto-pair /
    pairing-token-fetch conveniences — those keep the STRICT allowlist so a DNS-
    rebound page (whose Origin would match the rebound Host) can never reach the
    credential-granting shortcuts."""
    return origin_allowed(origin) or origin_matches_request_host(origin, request_host)


def _origin_hostname(origin: str | None) -> str:
    if not origin:
        return ""
    from urllib.parse import urlsplit

    try:
        return (urlsplit(origin.strip().rstrip("/")).hostname or "").lower()
    except ValueError:
        return ""


def _request_hostname(request_host: str | None) -> str:
    if not request_host:
        return ""
    from urllib.parse import urlsplit

    try:
        return (urlsplit(f"//{request_host.strip()}").hostname or "").lower()
    except ValueError:
        return ""


_LOCALHOST_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_localhost_origin(origin: str | None) -> bool:
    return _origin_hostname(origin).rstrip(".") in _LOCALHOST_HOSTNAMES


_LOOPBACK_BINDS = ("", "127.0.0.1", "localhost", "::1")


def _local_bind_declared() -> bool:
    """True when the deployment publishes its ports on loopback ONLY. The kernel
    then guarantees no other machine can reach the ports at all, which is what
    makes the localhost auto-pair below sound; anything else (0.0.0.0, a LAN IP)
    is a DELIBERATE exposure and flips the token requirement back on.

    Two layers of truth: `DISCO_BIND` is the compose HOST-port publish address —
    when set (compose always passes it; default 127.0.0.1) it decides, because
    the container-internal `DISCO_HOST` must be 0.0.0.0 for nginx to reach the
    backend and says nothing about exposure. Without `DISCO_BIND` (a plain
    host-process run) the server's own bind address `DISCO_HOST` (default
    127.0.0.1) IS the exposure."""
    bind = (disco_env("BIND", "") or "").strip()
    if bind:
        return bind in _LOOPBACK_BINDS
    return (disco_env("HOST", "") or "").strip() in _LOOPBACK_BINDS


# Headers that betray a reverse proxy / tunnel in front (ngrok, cloudflared,
# CF Access, a hand-rolled nginx-with-XFF). The front-door nginx deliberately
# forwards ONLY X-Forwarded-Proto — never these — so a genuinely-local browser
# load carries none of them. Their PRESENCE means the request is exposed.
_PROXY_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "forwarded",
    "cf-connecting-ip",
    "x-real-ip",
)


def request_traversed_proxy(headers: Any) -> bool:
    """True when a hop-revealing forwarding header is present — the signal that a
    reverse proxy / tunnel sits in front of the front-door nginx (i.e. the deploy
    is exposed, whatever its bind posture claims). Used to REFUSE the localhost
    auto-pair on any proxied request. `headers` is any case-insensitive mapping
    (Starlette Request/WebSocket .headers). A client that forges one of these
    only causes a REFUSAL — fail-safe."""
    get = getattr(headers, "get", None)
    if get is None:
        return False
    return any(get(h) for h in _PROXY_HEADERS)


def localhost_auto_pair_allowed(
    origin: str | None, request_host: str | None, *, via_proxy: bool = False
) -> bool:
    """Zero-friction first run on the operator's own machine: allow a TOKENLESS
    admin mint when ALL THREE hold —

      1. The deployment is loopback-bound (`_local_bind_declared`): the ports
         are kernel-unreachable from any other machine, so whoever reached them
         is on this host (or came through a tunnel the operator opened).
      2. The page is a localhost-family origin: the browser itself asserts it
         loaded the app from this machine. A cross-site page (or a DNS-rebound
         one) carries ITS OWN origin, never localhost.
      3. Origin == request Host: it is THIS app's own front-door page pairing,
         not some other local app on a different port riding the allowlist.

    The containerized deploy can never use the TCP-peer loopback check (the
    server sees the Docker gateway for everyone), so the bind posture stands in
    as the unforgeable signal: a non-browser client that forges a localhost
    Origin could only do so while already ON the machine — i.e. the operator.
    `DISCO_AUTH_LOCAL_AUTO_PAIR` overrides explicitly (1 forces on, 0 off).

    `via_proxy` (a forwarding header was present) hard-disables this: a tunnel /
    reverse proxy in front means the loopback-bind premise no longer holds, so a
    remote client forging a localhost Host must NOT slip through — require the
    token instead. Residual footgun: a RAW TCP port-forward (`ssh -R`) of a
    loopback-bound instance adds no headers and cannot be distinguished from a
    local browser; that is a deliberate exposure — set DISCO_BIND or
    DISCO_AUTH_LOCAL_AUTO_PAIR=0. (codex 2026-07-09)"""
    override = (disco_env("AUTH_LOCAL_AUTO_PAIR", "") or "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return False
    forced_on = override in {"1", "true", "yes", "on"}
    # The proxy guard applies EVEN to the explicit force-on: if you front it with
    # a proxy you are exposed, and the localhost shortcut has no meaning there.
    if via_proxy:
        return False
    if not forced_on and not _local_bind_declared():
        return False
    return _is_localhost_origin(origin) and origin_matches_request_host(origin, request_host)
