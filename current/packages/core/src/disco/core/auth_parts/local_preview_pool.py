"""The local, DNS-free Preview gateway listener pool + its cookie naming.

Extracted verbatim from ``auth.py`` to reduce module size; the parent module
re-imports every name here unchanged (see ``auth_parts/__init__.py``).

DNS-free canonical Preview origins use a small, explicitly published loopback
listener pool. A durable lease gives one conversation one browser origin while
it is active. Each slot uses a distinct literal host in 127.0.0.0/8 as well as a
distinct port: browser cookies ignore ports, so 127.0.0.1 on many ports would
leak generated-app cookies between conversations. Literal loopback hosts need
no Firefox DNS preference. The gateway itself strips Disco cookies and exposes
no authenticated application routes.
"""

from __future__ import annotations

from ..env import disco_env

LOCAL_PREVIEW_COOKIE_PREFIX = "disco_local_preview_"
DEFAULT_LOCAL_PREVIEW_PORT_START = 19120
DEFAULT_LOCAL_PREVIEW_PORT_COUNT = 32


def path_preview_cookie_name(cid8: str) -> str:
    """Per-conversation name so concurrent isolated previews cannot clobber each other."""
    normalized = "".join(ch for ch in cid8.lower() if ch in "0123456789abcdef")[:8]
    if len(normalized) != 8:
        raise ValueError("path preview cookie requires an eight-character hex id")
    return f"disco_path_preview_{normalized}"


def local_preview_cookie_name(listener_port: int) -> str:
    """Cookie name scoped to one leased DNS-free Preview listener.

    Cookies are not port-scoped. Encoding the listener in the name prevents two
    concurrent per-conversation Preview origins on 127.0.0.1 from overwriting one
    another's capability cookie.
    """

    if not 1 <= listener_port <= 65535:
        raise ValueError("local preview listener port is invalid")
    return f"{LOCAL_PREVIEW_COOKIE_PREFIX}{listener_port}"


def local_preview_gateway_ports() -> tuple[int, ...]:
    """Configured bounded listener pool for DNS-free local Preview origins."""

    raw_start = disco_env("LOCAL_PREVIEW_PORT_START", str(DEFAULT_LOCAL_PREVIEW_PORT_START))
    raw_count = disco_env("LOCAL_PREVIEW_PORT_COUNT", str(DEFAULT_LOCAL_PREVIEW_PORT_COUNT))
    try:
        start = int(raw_start or "")
        count = int(raw_count or "")
    except ValueError as exc:
        raise ValueError("local preview port range must be numeric") from exc
    # Rotation needs a second origin so generation N's capability cannot be
    # silently reused for generation N+1 on the same browser origin.
    if start < 1024 or count < 2 or count > 253 or start + count - 1 > 65535:
        raise ValueError("local preview port range is outside the bounded user-port range")
    return tuple(range(start, start + count))


def local_preview_gateway_host(listener_port: int) -> str:
    """Return the DNS-free, cookie-isolated loopback host for one listener slot."""

    ports = local_preview_gateway_ports()
    try:
        slot = ports.index(listener_port)
    except ValueError as exc:
        raise ValueError("local preview listener is outside the configured pool") from exc
    # .1 remains the ordinary UI/API loopback identity. Slots begin at .2 and
    # the bounded pool cannot reach the broadcast-like .255 address.
    return f"127.0.0.{slot + 2}"
