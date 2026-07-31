"""Request-shape checks: WebSocket origin, service workers, capability lookup."""

from __future__ import annotations

import urllib.parse

from disco.core.auth import PREVIEW_COOKIE, PreviewCapability, PreviewCapabilitySigner
from starlette.types import Scope

from ._headers import _cookie_values


def _preview_ws_origin_allowed(scope: Scope, host: str) -> bool:
    origin = ""
    forwarded_proto = ""
    for name, value in scope.get("headers", []):
        if name.lower() == b"origin":
            origin = value.decode("latin1")
        elif name.lower() == b"x-forwarded-proto":
            forwarded_proto = value.decode("latin1").split(",", 1)[0].strip().lower()
    if not origin:
        return False
    parsed = urllib.parse.urlparse(origin)
    scope_scheme = str(scope.get("scheme") or "http").lower()
    expected_scheme = {"ws": "http", "wss": "https"}.get(scope_scheme, scope_scheme)
    if forwarded_proto in {"http", "https"}:
        expected_scheme = forwarded_proto
    return parsed.scheme == expected_scheme and parsed.netloc.lower() == host.lower()


def _preview_service_worker_request(scope: Scope) -> bool:
    for name, value in scope.get("headers", []):
        name_lower = name.lower()
        value_lower = value.decode("latin1").strip().lower()
        if name_lower == b"sec-fetch-dest" and value_lower == "serviceworker":
            return True
        if name_lower == b"service-worker" and value_lower == "script":
            return True
    return False


def _verified_preview_capability(
    signer: PreviewCapabilitySigner,
    scope: Scope,
    *,
    cid8: str,
    port: int,
    method: str,
    path: str,
    cookie_name: str = PREVIEW_COOKIE,
) -> PreviewCapability | None:
    """Select a valid signed capability without trusting duplicate order."""

    for token in _cookie_values(scope, cookie_name):
        cap = signer.verify(token, cid8=cid8, port=port, method=method, path=path)
        if cap is not None:
            return cap
    return None
