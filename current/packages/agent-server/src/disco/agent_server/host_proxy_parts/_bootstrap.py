"""The preview-bootstrap POST flow: intent redemption and storage handoff.

Extracted from ``HostPreviewProxyMiddleware._handle_preview_bootstrap``
(PKG-10-SANDBOX), which exceeded the per-callable logical-line and McCabe
budgets as one flat method. Split by phase (request validation, bounded body
read, intent redemption, response construction) so each stays trivially under
budget; the orchestrating ``_handle_preview_bootstrap`` is now a straight-line
sequence of "did this phase already respond?" checks. Every status code and
response body string is reproduced byte-for-byte from the original method —
the agent loop and soak oracles key on this prose.
"""

from __future__ import annotations

import inspect
import json
import time
import urllib.parse
from typing import TYPE_CHECKING

from disco.agent_server.preview_bootstrap import (
    MAX_PREVIEW_REDEMPTION_BODY_BYTES,
    cross_site_iframe_headers,
    parse_preview_redemption,
    parse_preview_storage_handoff,
    preview_navigation_document,
    preview_redemption_content_type,
    preview_storage_reset_document,
)
from disco.core.auth import PREVIEW_BOOTSTRAP_PATH, PREVIEW_COOKIE, PreviewCapability, preview_ttl_s
from starlette.types import Receive, Scope, Send

if TYPE_CHECKING:
    from disco.agent_server.host_proxy import HostPreviewProxyMiddleware

    from ._route_resolution import _PreviewRoute


async def _validate_bootstrap_request(scope: Scope, send: Send) -> dict[str, str] | None:
    """Enforce POST + a valid redemption content-type, or respond and return None."""
    method = str(scope.get("method") or "GET").upper()
    headers = {
        name.decode("latin1").lower(): value.decode("latin1")
        for name, value in scope.get("headers", [])
    }
    if method != "POST":
        await send(
            {
                "type": "http.response.start",
                "status": 405,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"method not allowed"})
        return None
    if not preview_redemption_content_type(headers.get("content-type")):
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"invalid preview intent"})
        return None
    return headers


async def _read_bootstrap_body(receive: Receive) -> bytes:
    body = bytearray()
    more_body = True
    while more_body:
        message = await receive()
        if message.get("type") != "http.request":
            body.clear()
            break
        chunk = message.get("body", b"")
        if isinstance(chunk, bytes):
            body.extend(chunk)
        if len(body) > MAX_PREVIEW_REDEMPTION_BODY_BYTES:
            body.clear()
            break
        more_body = bool(message.get("more_body"))
    return bytes(body)


async def _redeem_bootstrap_token(
    self: HostPreviewProxyMiddleware,
    send: Send,
    raw_body: bytes,
    cid8: str,
    port: int,
    *,
    expected_conversation_id: str | None,
    expected_owner_id: str | None,
    expected_authority_id: str | None,
) -> tuple[str, str, PreviewCapability] | None:
    """Parse + redeem the one-shot intent, then verify the minted capability."""
    parsed = parse_preview_redemption(raw_body)
    if parsed is None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"invalid preview intent"})
        return None
    intent = parsed
    redeemed = self.capability_signer.redeem_intent(
        intent,
        cid8=cid8,
        port=port,
        path_scope="host",
        expected_conversation_id=expected_conversation_id,
        expected_owner_id=expected_owner_id,
        expected_authority_id=expected_authority_id,
    )
    if redeemed is None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"invalid preview intent"})
        return None
    token, target = redeemed
    cap = self.capability_signer.verify(
        token,
        cid8=cid8,
        port=port,
        method="GET",
        path=urllib.parse.urlsplit(target).path or "/",
    )
    if cap is None or (
        expected_conversation_id is not None
        and (
            cap.conversation_id != expected_conversation_id
            or cap.owner_id != expected_owner_id
            or cap.authority_id != expected_authority_id
        )
    ):
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"preview intent scope mismatch"})
        return None
    return token, target, cap


async def _build_bootstrap_response(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    send: Send,
    *,
    cap: PreviewCapability,
    token: str,
    target: str,
    headers: dict[str, str],
    cookie_name: str,
    storage_reset_required: bool,
    expected_authority_id: str | None,
) -> None:
    partitioned = cross_site_iframe_headers(headers)
    if storage_reset_required and expected_authority_id is not None:
        handoff = self.capability_signer.mint_storage_handoff(
            cap,
            target,
            partitioned=partitioned,
        )
        response_body, reset_headers = preview_storage_reset_document(handoff)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (name.encode("latin1"), value.encode("latin1"))
                    for name, value in reset_headers.items()
                ],
            }
        )
        await send({"type": "http.response.body", "body": response_body})
        return
    if partitioned:
        cookie = (
            f"{cookie_name}={token}; Path=/; Max-Age={preview_ttl_s()}; "
            "HttpOnly; Secure; SameSite=None; Partitioned"
        ).encode("latin1")
    else:
        forwarded_scheme = (headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        secure = forwarded_scheme == "https" or scope.get("scheme") == "https"
        cookie = (
            f"{cookie_name}={token}; Path=/; Max-Age={preview_ttl_s()}; "
            f"HttpOnly; {'Secure; ' if secure else ''}SameSite=Strict"
        ).encode("latin1")
    response_body, navigation_headers = preview_navigation_document(
        target,
        clear_storage=expected_authority_id is None,
    )
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                *[
                    (name.encode("latin1"), value.encode("latin1"))
                    for name, value in navigation_headers.items()
                ],
                (b"set-cookie", cookie),
            ],
        }
    )
    await send({"type": "http.response.body", "body": response_body})


async def _handle_preview_bootstrap(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    receive: Receive,
    send: Send,
    cid8: str,
    port: int,
    *,
    cookie_name: str = PREVIEW_COOKIE,
    expected_conversation_id: str | None = None,
    expected_owner_id: str | None = None,
    expected_authority_id: str | None = None,
    storage_reset_required: bool = False,
    local_listener_port: int | None = None,
) -> None:
    headers = await _validate_bootstrap_request(scope, send)
    if headers is None:
        return

    raw_body = await _read_bootstrap_body(receive)
    handoff = parse_preview_storage_handoff(raw_body)
    if handoff is not None:
        await _complete_storage_handoff(
            self,
            scope,
            send,
            handoff=handoff,
            cid8=cid8,
            port=port,
            cookie_name=cookie_name,
            expected_conversation_id=expected_conversation_id,
            expected_owner_id=expected_owner_id,
            expected_authority_id=expected_authority_id,
            local_listener_port=local_listener_port,
        )
        return

    redeemed = await _redeem_bootstrap_token(
        self,
        send,
        raw_body,
        cid8,
        port,
        expected_conversation_id=expected_conversation_id,
        expected_owner_id=expected_owner_id,
        expected_authority_id=expected_authority_id,
    )
    if redeemed is None:
        return
    token, target, cap = redeemed

    await _build_bootstrap_response(
        self,
        scope,
        send,
        cap=cap,
        token=token,
        target=target,
        headers=headers,
        cookie_name=cookie_name,
        storage_reset_required=storage_reset_required,
        expected_authority_id=expected_authority_id,
    )


async def _maybe_dispatch_bootstrap(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    receive: Receive,
    send: Send,
    route: _PreviewRoute,
    path: str,
) -> bool:
    """Dispatch to the bootstrap POST handler iff this request targets it.

    Returns True iff the request is already fully handled and the caller
    must return.
    """
    if not (
        self.require_capability and scope["type"] == "http" and path == PREVIEW_BOOTSTRAP_PATH
    ):
        return False
    await _handle_preview_bootstrap(
        self,
        scope,
        receive,
        send,
        route.cid8,
        route.port,
        cookie_name=route.cookie_name,
        expected_conversation_id=route.expected_conversation_id,
        expected_owner_id=route.expected_owner_id,
        expected_authority_id=route.expected_authority_id,
        storage_reset_required=route.storage_reset_required,
        local_listener_port=route.listener_port if route.local_gateway else None,
    )
    return True


async def _complete_storage_handoff(
    proxy: HostPreviewProxyMiddleware,
    scope: Scope,
    send: Send,
    *,
    handoff: str,
    cid8: str,
    port: int,
    cookie_name: str,
    expected_conversation_id: str | None,
    expected_owner_id: str | None,
    expected_authority_id: str | None,
    local_listener_port: int | None,
) -> None:
    if (
        expected_conversation_id is None
        or expected_owner_id is None
        or expected_authority_id is None
        or local_listener_port is None
    ):
        redeemed = None
    else:
        redeemed = proxy.capability_signer.redeem_storage_handoff(
            handoff,
            cid8=cid8,
            port=port,
            expected_conversation_id=expected_conversation_id,
            expected_owner_id=expected_owner_id,
            expected_authority_id=expected_authority_id,
        )
    if redeemed is None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"invalid preview handoff"})
        return
    token, target, partitioned = redeemed
    committer = proxy.local_storage_reset_committer
    committed = (
        committer(
            local_listener_port,
            authority_id=expected_authority_id,
            now=int(time.time()),
        )
        if committer is not None
        else False
    )
    if inspect.isawaitable(committed):
        committed = await committed
    if committed is not True:
        await send(
            {
                "type": "http.response.start",
                "status": 409,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"preview authority changed"})
        return
    secure = partitioned or scope.get("scheme") == "https"
    cookie_policy = (
        "Secure; SameSite=None; Partitioned"
        if partitioned
        else f"{'Secure; ' if secure else ''}SameSite=Strict"
    )
    cookie = (
        f"{cookie_name}={token}; Path=/; Max-Age={preview_ttl_s()}; HttpOnly; {cookie_policy}"
    ).encode("latin1")
    response_body = json.dumps({"target": target}, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"private, no-store"),
                (b"x-content-type-options", b"nosniff"),
                (b"set-cookie", cookie),
            ],
        }
    )
    await send({"type": "http.response.body", "body": response_body})
