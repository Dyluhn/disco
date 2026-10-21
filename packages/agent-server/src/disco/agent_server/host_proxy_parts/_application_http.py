"""Shared bounded application HTTP policy for approved sandbox transports."""

from __future__ import annotations

import httpx
from fastapi import Request, Response
from starlette.requests import ClientDisconnect

from .. import host_proxy
from ..preview_inject import (
    MAX_ELEMENT_MENTION_HTML_BYTES,
    inject_element_mention_picker,
    inject_selection_agent,
)
from ._forwarding import _injected_html_headers, _response_forward_headers

_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
}


def _hop_headers(connection: str) -> set[str]:
    return _HOP_HEADERS | {name.strip().lower() for name in connection.split(",")}


async def _request_body(request: Request, declared: int | None) -> bytes | Response:
    if declared is not None and (
        declared < 0 or declared > host_proxy.MAX_PREVIEW_REQUEST_BODY_BYTES
    ):
        return Response("preview request body too large", status_code=413)
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > host_proxy.MAX_PREVIEW_REQUEST_BODY_BYTES:
                return Response("preview request body too large", status_code=413)
            body.extend(chunk)
    except ClientDisconnect:
        return Response("preview request disconnected", status_code=400)
    if declared is not None and len(body) != declared:
        return Response("preview request body incomplete", status_code=400)
    return bytes(body)


def _application_response(
    request: Request,
    upstream: str,
    result: tuple[int, list[tuple[str, str]], bytes],
    *,
    inject_selection: bool,
) -> Response:
    status, headers, body = result
    metadata = httpx.Response(status, headers=httpx.Headers(headers, encoding="latin1"))
    metadata.headers.encoding = "latin1"
    hop = _hop_headers(",".join(metadata.headers.get_list("connection")))
    content_type = metadata.headers.get("content-type", "application/octet-stream")
    encoding = metadata.headers.get("content-encoding", "").strip().lower()
    can_inject = (
        request.method != "HEAD"
        and status not in {204, 304}
        and content_type.split(";", 1)[0].strip().lower() == "text/html"
        and encoding in {"", "identity"}
        and len(body) <= MAX_ELEMENT_MENTION_HTML_BYTES
    )
    if can_inject:
        original = body
        if inject_selection:
            body = inject_selection_agent(body, content_type)
        body = inject_element_mention_picker(body, content_type)
        forwarded = _injected_html_headers(
            metadata,
            hop,
            local_gateway=True,
            request_scope=request.scope,
            upstream=upstream,
            changed=body != original,
            injected=body,
        )
    else:
        forwarded = _response_forward_headers(
            metadata,
            hop,
            local_gateway=True,
            upstream=upstream,
            request_scope=request.scope,
        )
    response = Response(body, status_code=status)
    response.raw_headers = forwarded
    return response
