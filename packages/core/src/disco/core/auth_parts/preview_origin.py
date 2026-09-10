"""Preview origin/hostname minting + recognition.

Extracted from ``auth.py`` to reduce module size; the parent module re-imports
every name here unchanged (see ``auth_parts/__init__.py``). Unlike a prior,
REJECTED attempt at this split, the two previously over-budget callables
(``validated_canonical_preview_url`` and ``_is_generated_preview_label``) are
not moved verbatim — each is genuinely decomposed into small, cohesive,
independently-bounded helpers first (see the module-level private functions
below each of them), so the callables carry their reduced McCabe score to this
module rather than merely relocating the original branch graph.

The five ``PATH_PREVIEW_*`` / ``PREVIEW_BOOTSTRAP_PATH`` /
``ISOLATED_PATH_PREVIEW_PREFIX`` constants stay defined on the parent module
(``ISOLATED_PATH_PREVIEW_PREFIX`` is also used directly by
``PreviewCapabilitySigner.redeem_intent``, which remains there). This module
reads them back through the PARENT MODULE at call time (``auth.CONST``, never
a captured ``from ..auth import CONST``): the parent's own top-level imports
this module before any of its constants are assigned, so a name captured at
import time would be missing. Resolving via the module object defers the
lookup until these functions actually run, by which point `auth` has finished
loading — campaign-normal for parts modules.
"""

from __future__ import annotations

import hashlib
import ipaddress
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .. import auth
from ..env import disco_env
from .local_preview_pool import local_preview_gateway_host, local_preview_gateway_ports
from .origin_policy import _origin_hostname, _request_hostname


def path_preview_host_label(conversation_id: str, port: int) -> str:
    """Return a DNS-safe path-preview label bound to the full conversation ID.

    The first eight hex characters remain visible for the existing live-runtime
    resolver. A 160-bit digest supplies the browser-origin identity; using only
    the routing prefix would let distinct conversations share cookies/storage.
    The longest valid port still leaves this label below DNS's 63-byte limit.
    """

    cid = conversation_id.removeprefix("conv_")
    cid8 = cid[:8].lower()
    if len(cid8) != 8 or any(ch not in "0123456789abcdef" for ch in cid8):
        raise ValueError("path preview host requires an eight-character hex id")
    if not 1 <= port <= 65535:
        raise ValueError("path preview host requires a valid TCP port")
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[
        :auth.PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS
    ]
    return f"{auth.PATH_PREVIEW_HOST_PREFIX}-{cid8}-{digest}-{port}"


def validated_isolated_path_preview_url(
    base_url: str,
    conversation_id: str,
    port: int,
    bootstrap_url: str,
) -> str | None:
    """Validate a server-minted path-preview bootstrap and derive its content URL.

    Capability consumers must never follow an arbitrary server response while holding
    a one-time preview intent.  The bootstrap has to stay on the exact scheme/port and
    full-conversation-bound p3s host derived from the trusted agent-server base URL.
    Userinfo, query, and fragment data are forbidden so the intent remains body-only.
    """

    try:
        base = urlsplit(base_url)
        bootstrap = urlsplit(bootstrap_url)
        cid8 = conversation_id.removeprefix("conv_")[:8]
        label = path_preview_host_label(conversation_id, port)
        base_hostname = (base.hostname or "").lower()
        preview_base_hostname = (
            "localhost" if base_hostname in {"127.0.0.1", "::1", "localhost"} else base_hostname
        )
        expected_hostname = f"{label}.{preview_base_hostname}"
        if (
            not cid8
            or not base_hostname
            or bootstrap.scheme != base.scheme
            or (bootstrap.hostname or "").lower() != expected_hostname
            or bootstrap.port != base.port
            or bootstrap.path != f"{auth.PATH_PREVIEW_BOOTSTRAP_PATH}/{cid8}"
            or bootstrap.query
            or bootstrap.fragment
            or bootstrap.username is not None
            or bootstrap.password is not None
        ):
            return None
    except (ValueError, UnicodeError):
        return None
    return urlunsplit(
        (
            bootstrap.scheme,
            bootstrap.netloc,
            f"{auth.ISOLATED_PATH_PREVIEW_PREFIX}/{conversation_id}/",
            "",
            "",
        )
    )


def _canonical_preview_request_shape_valid(
    cid8: str, port: int, base_hostname: str, bootstrap: SplitResult
) -> bool:
    """The structural checks every canonical Preview bootstrap must satisfy."""
    return not (
        len(cid8) != 8
        or any(ch not in "0123456789abcdef" for ch in cid8)
        or not 1 <= port <= 65535
        or not base_hostname
        or bootstrap.path != auth.PREVIEW_BOOTSTRAP_PATH
        or bootstrap.query
        or bootstrap.fragment
        or bootstrap.username is not None
        or bootstrap.password is not None
    )


def _is_local_preview_base(base_hostname: str) -> bool:
    try:
        return base_hostname == "localhost" or ipaddress.ip_address(base_hostname).is_loopback
    except ValueError:
        return False


def _canonical_preview_local_gateway_match(bootstrap: SplitResult, bootstrap_hostname: str) -> bool:
    """Whether a local-base bootstrap targets one of the configured loopback listeners."""
    listener_port = bootstrap.port
    return not (
        bootstrap.scheme != "http"
        or listener_port not in local_preview_gateway_ports()
        or bootstrap_hostname != local_preview_gateway_host(listener_port)
    )


def _canonical_preview_remote_origin_match(
    bootstrap: SplitResult,
    bootstrap_hostname: str,
    base: SplitResult,
    configured: str,
    cid8: str,
    port: int,
) -> bool:
    """Whether a remote/configured bootstrap targets the expected per-conversation origin."""
    origin_base = urlsplit(configured) if configured else base
    origin_hostname = (origin_base.hostname or "").lower().rstrip(".")
    expected_hostname = f"p2-{cid8}-{port}.{origin_hostname}"
    return not (
        not origin_hostname
        or bootstrap.scheme != origin_base.scheme
        or bootstrap_hostname != expected_hostname
        or bootstrap.port != origin_base.port
    )


def validated_canonical_preview_url(
    base_url: str,
    conversation_id: str,
    port: int,
    bootstrap_url: str,
) -> str | None:
    """Validate a canonical Preview bootstrap and derive its bearer-free root URL.

    Local deployments use one of the configured literal-loopback gateway listeners.
    Remote deployments retain the exact configured isolated preview base (or the
    trusted API host when no separate base is configured). The one-use intent is
    always delivered in the POST body, never copied into the resulting URL.
    """

    try:
        base = urlsplit(base_url)
        bootstrap = urlsplit(bootstrap_url)
        base_hostname = (base.hostname or "").lower().rstrip(".")
        cid8 = conversation_id.removeprefix("conv_")[:8].lower()
        if not _canonical_preview_request_shape_valid(cid8, port, base_hostname, bootstrap):
            return None

        local_base = _is_local_preview_base(base_hostname)
        bootstrap_hostname = (bootstrap.hostname or "").lower().rstrip(".")
        configured = (disco_env("PREVIEW_ORIGIN_BASE", "") or "").strip()
        if local_base and not configured:
            if not _canonical_preview_local_gateway_match(bootstrap, bootstrap_hostname):
                return None
        else:
            if not _canonical_preview_remote_origin_match(
                bootstrap, bootstrap_hostname, base, configured, cid8, port
            ):
                return None
    except (ValueError, UnicodeError):
        return None
    return urlunsplit((bootstrap.scheme, bootstrap.netloc, "/", "", ""))


def _hex8(value: str) -> bool:
    return len(value) == 8 and all(ch in "0123456789abcdef" for ch in value)


def _valid_preview_label_port(value: str) -> bool:
    return 2 <= len(value) <= 5 and value.isdigit() and 1 <= int(value) <= 65535


def _is_bare_preview_label(parts: list[str]) -> bool:
    """Pre-p2 host shape: ``<cid8>-<port>``. No longer served, but already-open
    generated pages must remain quarantined until closed or reloaded onto p2."""
    if len(parts) != 2:
        return False
    cid8, port = parts
    return _hex8(cid8) and _valid_preview_label_port(port)


def _is_live_preview_label(parts: list[str]) -> bool:
    """Live-preview host shape: ``p2-<cid8>-<port>``."""
    if len(parts) != 3 or parts[0] != "p2":
        return False
    _family, cid8, port = parts
    return _hex8(cid8) and _valid_preview_label_port(port)


def _is_path_preview_label(parts: list[str]) -> bool:
    """Isolated path-preview host shape: ``p3s-<cid8>-<digest>-<port>``."""
    if len(parts) != 4 or parts[0] != auth.PATH_PREVIEW_HOST_PREFIX:
        return False
    _family, cid8, digest, port = parts
    return (
        _hex8(cid8)
        and len(digest) == auth.PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS
        and all(ch in "0123456789abcdef" for ch in digest)
        and _valid_preview_label_port(port)
    )


def _is_generated_preview_label(label: str) -> bool:
    """Recognize only hostname labels the live/path preview transports minted."""

    parts = label.split("-")
    return (
        _is_bare_preview_label(parts)
        or _is_live_preview_label(parts)
        or _is_path_preview_label(parts)
    )


def generated_preview_request_host(request_host: str | None) -> bool:
    """True when a request Host starts with an exact generated preview label."""

    hostname = _request_hostname(request_host).rstrip(".")
    label, separator, _suffix = hostname.partition(".")
    return bool(separator and _is_generated_preview_label(label))


def _is_local_preview_hostname(hostname: str) -> bool:
    """Whether a hostname can be used by the local path-preview transport.

    Browsers accept the complete IPv4 loopback block, not only 127.0.0.1.  Keep
    this predicate deliberately broader than the two aliases generated today so
    a preview cannot escape its HostOnly quarantine cookie through another 127/8
    spelling or a DNS name aimed at the privileged service.
    """

    normalized = hostname.rstrip(".")
    if (
        normalized == "localhost"
        or normalized == "::1"
        or normalized == "127"
        or normalized.startswith("127.")
    ):
        return True
    label, separator, suffix = normalized.partition(".")
    if not separator or not (suffix == "localhost" or suffix.endswith(".localhost")):
        return False
    return _is_generated_preview_label(label)


def local_preview_origin_crosses_host(origin: str | None, request_host: str | None) -> bool:
    """Reject a local-preview browser origin targeting another hostname.

    Local path previews use a full-conversation-bound ``p3s.*.localhost`` origin;
    live previews use ``p2.*.localhost``. Cookies are HostOnly, while CORS
    historically permits local aliases. Without this pre-auth check, generated
    JavaScript can target the operator's original alias, regain its full session
    cookie, and reach public credential grants or owner/admin APIs.

    Different ports on the *same* hostname remain valid for the normal split
    current/frontend/server development layout. A local preview origin targeting any
    different hostname (including a DNS name resolving to loopback) is denied.
    """

    origin_hostname = _origin_hostname(origin)
    if not _is_local_preview_hostname(origin_hostname):
        return False
    return origin_hostname != _request_hostname(request_host)
