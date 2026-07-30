"""Target and origin policy for browser-facing authentication boundaries."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .env import disco_env

PREVIEW_COOKIE = "disco_preview_cap"
PREVIEW_BOOTSTRAP_PATH = "/__disco/preview-auth"
PATH_PREVIEW_BOOTSTRAP_PATH = "/__disco/path-preview-auth"
ISOLATED_PATH_PREVIEW_PREFIX = "/__disco/isolated-preview"
PATH_PREVIEW_HOST_PREFIX = "p3s"
PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS = 40
LOCAL_PREVIEW_COOKIE_PREFIX = "disco_local_preview_"
DEFAULT_LOCAL_PREVIEW_PORT_START = 19120
DEFAULT_LOCAL_PREVIEW_PORT_COUNT = 32
MAX_PREVIEW_TARGET_PATH_CHARS = 4096
PREVIEW_APP_HTTP_METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")
_PREVIEW_HTTP_METHODS = frozenset(PREVIEW_APP_HTTP_METHODS)

_DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:8800",
    "http://127.0.0.1:8800",
    "http://localhost:8088",
    "http://127.0.0.1:8088",
)
_LOCALHOST_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_LOOPBACK_BINDS = ("", "127.0.0.1", "localhost", "::1")
_PROXY_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-host",
    "forwarded",
    "cf-connecting-ip",
    "x-real-ip",
)
_HEX8_RE = re.compile(r"^[0-9a-f]{8}$")
_PATH_PREVIEW_LABEL_RE = re.compile(
    rf"^{PATH_PREVIEW_HOST_PREFIX}-([0-9a-f]{{8}})-"
    rf"([0-9a-f]{{{PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS}}})-([0-9]+)$"
)
_LIVE_PREVIEW_LABEL_RE = re.compile(r"^(?:p2-)?([0-9a-f]{8})-([0-9]+)$")


def path_preview_cookie_name(cid8: str) -> str:
    normalized = "".join(ch for ch in cid8.lower() if ch in "0123456789abcdef")[:8]
    if len(normalized) != 8:
        raise ValueError("path preview cookie requires an eight-character hex id")
    return f"disco_path_preview_{normalized}"


def local_preview_cookie_name(listener_port: int) -> str:
    if not 1 <= listener_port <= 65535:
        raise ValueError("local preview listener port is invalid")
    return f"{LOCAL_PREVIEW_COOKIE_PREFIX}{listener_port}"


def local_preview_gateway_ports() -> tuple[int, ...]:
    raw_start = disco_env("LOCAL_PREVIEW_PORT_START", str(DEFAULT_LOCAL_PREVIEW_PORT_START))
    raw_count = disco_env("LOCAL_PREVIEW_PORT_COUNT", str(DEFAULT_LOCAL_PREVIEW_PORT_COUNT))
    try:
        start = int(raw_start or "")
        count = int(raw_count or "")
    except ValueError as exc:
        raise ValueError("local preview port range must be numeric") from exc
    if start < 1024 or count < 2 or count > 253 or start + count - 1 > 65535:
        raise ValueError("local preview port range is outside the bounded user-port range")
    return tuple(range(start, start + count))


def local_preview_gateway_host(listener_port: int) -> str:
    try:
        slot = local_preview_gateway_ports().index(listener_port)
    except ValueError as exc:
        raise ValueError("local preview listener is outside the configured pool") from exc
    return f"127.0.0.{slot + 2}"


def cookie_header_values(cookie_header: str | None, name: str) -> tuple[str, ...]:
    if not cookie_header or not name:
        return ()
    values: list[str] = []
    for part in cookie_header.split(";"):
        raw_name, separator, value = part.strip().partition("=")
        if separator and raw_name == name:
            values.append(value)
    return tuple(values)


def cookie_header_from_headers(headers: Any) -> str | None:
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        listed: Any = getlist("cookie")
        if isinstance(listed, (str, bytes)):
            listed = (listed,)
        values = [str(value) for value in listed if value]
        if values:
            return "; ".join(values)
    get = getattr(headers, "get", None)
    if callable(get):
        value = get("cookie")
        return str(value) if value else None
    return None


def path_preview_host_label(conversation_id: str, port: int) -> str:
    cid = conversation_id.removeprefix("conv_")
    cid8 = cid[:8].lower()
    if _HEX8_RE.fullmatch(cid8) is None:
        raise ValueError("path preview host requires an eight-character hex id")
    if not 1 <= port <= 65535:
        raise ValueError("path preview host requires a valid TCP port")
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[
        :PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS
    ]
    return f"{PATH_PREVIEW_HOST_PREFIX}-{cid8}-{digest}-{port}"


def validated_isolated_path_preview_url(
    base_url: str,
    conversation_id: str,
    port: int,
    bootstrap_url: str,
) -> str | None:
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
        valid = (
            bool(cid8)
            and bool(base_hostname)
            and bootstrap.scheme == base.scheme
            and (bootstrap.hostname or "").lower() == expected_hostname
            and bootstrap.port == base.port
            and bootstrap.path == f"{PATH_PREVIEW_BOOTSTRAP_PATH}/{cid8}"
            and not bootstrap.query
            and not bootstrap.fragment
            and bootstrap.username is None
            and bootstrap.password is None
        )
    except (ValueError, UnicodeError):
        return None
    if not valid:
        return None
    return urlunsplit(
        (
            bootstrap.scheme,
            bootstrap.netloc,
            f"{ISOLATED_PATH_PREVIEW_PREFIX}/{conversation_id}/",
            "",
            "",
        )
    )


def _canonical_parts(
    base_url: str,
    conversation_id: str,
    port: int,
    bootstrap_url: str,
) -> tuple[SplitResult, SplitResult, str, str] | None:
    try:
        base = urlsplit(base_url)
        bootstrap = urlsplit(bootstrap_url)
        base_hostname = (base.hostname or "").lower().rstrip(".")
        cid8 = conversation_id.removeprefix("conv_")[:8].lower()
        valid = (
            _HEX8_RE.fullmatch(cid8) is not None
            and 1 <= port <= 65535
            and bool(base_hostname)
            and bootstrap.path == PREVIEW_BOOTSTRAP_PATH
            and not bootstrap.query
            and not bootstrap.fragment
            and bootstrap.username is None
            and bootstrap.password is None
        )
    except (ValueError, UnicodeError):
        return None
    return (base, bootstrap, base_hostname, cid8) if valid else None


def _is_loopback_hostname(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _local_canonical_matches(bootstrap: SplitResult) -> bool:
    listener_port = bootstrap.port
    return (
        bootstrap.scheme == "http"
        and listener_port in local_preview_gateway_ports()
        and (bootstrap.hostname or "").lower().rstrip(".")
        == local_preview_gateway_host(listener_port)
    )


def _remote_canonical_matches(
    base: SplitResult,
    bootstrap: SplitResult,
    cid8: str,
    port: int,
    configured: str,
) -> bool:
    origin_base = urlsplit(configured) if configured else base
    origin_hostname = (origin_base.hostname or "").lower().rstrip(".")
    return (
        bool(origin_hostname)
        and bootstrap.scheme == origin_base.scheme
        and (bootstrap.hostname or "").lower().rstrip(".") == f"p2-{cid8}-{port}.{origin_hostname}"
        and bootstrap.port == origin_base.port
    )


def validated_canonical_preview_url(
    base_url: str,
    conversation_id: str,
    port: int,
    bootstrap_url: str,
) -> str | None:
    parts = _canonical_parts(base_url, conversation_id, port, bootstrap_url)
    if parts is None:
        return None
    base, bootstrap, base_hostname, cid8 = parts
    configured = (disco_env("PREVIEW_ORIGIN_BASE", "") or "").strip()
    try:
        matches = (
            _local_canonical_matches(bootstrap)
            if _is_loopback_hostname(base_hostname) and not configured
            else _remote_canonical_matches(base, bootstrap, cid8, port, configured)
        )
    except (ValueError, UnicodeError):
        return None
    if not matches:
        return None
    return urlunsplit((bootstrap.scheme, bootstrap.netloc, "/", "", ""))


def allowed_frontend_origins() -> tuple[str, ...]:
    raw = disco_env("FRONTEND_ORIGINS", "")
    if raw:
        origins = [part.strip().rstrip("/") for part in raw.replace(",", " ").split()]
        return tuple(origin for origin in origins if origin)
    public_ui = (disco_env("PUBLIC_UI_URL", "") or "").strip().rstrip("/")
    return (*_DEFAULT_ALLOWED_ORIGINS, public_ui) if public_ui else _DEFAULT_ALLOWED_ORIGINS


def origin_allowed(origin: str | None) -> bool:
    return bool(origin and origin.rstrip("/") in allowed_frontend_origins())


def origin_matches_request_host(origin: str | None, request_host: str | None) -> bool:
    if not origin or not request_host:
        return False
    try:
        netloc = urlsplit(origin.strip().rstrip("/")).netloc
    except ValueError:
        return False
    return bool(netloc) and netloc.lower() == request_host.strip().lower()


def origin_permitted(origin: str | None, request_host: str | None = None) -> bool:
    return origin_allowed(origin) or origin_matches_request_host(origin, request_host)


def _origin_hostname(origin: str | None) -> str:
    if not origin:
        return ""
    try:
        return (urlsplit(origin.strip().rstrip("/")).hostname or "").lower()
    except ValueError:
        return ""


def _request_hostname(request_host: str | None) -> str:
    if not request_host:
        return ""
    try:
        return (urlsplit(f"//{request_host.strip()}").hostname or "").lower()
    except ValueError:
        return ""


def _valid_generated_port(raw_port: str) -> bool:
    return 2 <= len(raw_port) <= 5 and raw_port.isdigit() and 1 <= int(raw_port) <= 65535


def _is_generated_preview_label(label: str) -> bool:
    match = _LIVE_PREVIEW_LABEL_RE.fullmatch(label)
    if match is not None:
        return _valid_generated_port(match.group(2))
    match = _PATH_PREVIEW_LABEL_RE.fullmatch(label)
    return match is not None and _valid_generated_port(match.group(3))


def generated_preview_request_host(request_host: str | None) -> bool:
    hostname = _request_hostname(request_host).rstrip(".")
    label, separator, _suffix = hostname.partition(".")
    return bool(separator and _is_generated_preview_label(label))


def _is_local_preview_hostname(hostname: str) -> bool:
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
    origin_hostname = _origin_hostname(origin)
    return _is_local_preview_hostname(origin_hostname) and origin_hostname != _request_hostname(
        request_host
    )


def _is_localhost_origin(origin: str | None) -> bool:
    return _origin_hostname(origin).rstrip(".") in _LOCALHOST_HOSTNAMES


def _local_bind_declared() -> bool:
    bind = (disco_env("BIND", "") or "").strip()
    if bind:
        return bind in _LOOPBACK_BINDS
    return (disco_env("HOST", "") or "").strip() in _LOOPBACK_BINDS


def request_traversed_proxy(headers: Any) -> bool:
    get = getattr(headers, "get", None)
    return bool(get is not None and any(get(header) for header in _PROXY_HEADERS))


def localhost_auto_pair_allowed(
    origin: str | None,
    request_host: str | None,
    *,
    via_proxy: bool = False,
) -> bool:
    override = (disco_env("AUTH_LOCAL_AUTO_PAIR", "") or "").strip().lower()
    if override in {"0", "false", "no", "off"} or via_proxy:
        return False
    forced_on = override in {"1", "true", "yes", "on"}
    if not forced_on and not _local_bind_declared():
        return False
    return _is_localhost_origin(origin) and origin_matches_request_host(origin, request_host)
