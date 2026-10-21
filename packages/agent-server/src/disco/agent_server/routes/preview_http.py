"""Full HTTP requests to a contract-bound finished application runtime."""

from __future__ import annotations

from fastapi import Request, Response

from ..host_proxy_parts._application_http import (
    _application_response,
    _hop_headers,
    _request_body,
)
from ..host_proxy_parts._forwarding import _build_upstream_request_headers
from ..preview_projection import SealedPreviewRuntimeContract
from ..runtime import ConversationRuntime


async def finished_http_response(
    runtime: ConversationRuntime,
    request: Request,
    conversation_id: str,
    contract: SealedPreviewRuntimeContract,
    port: int,
    projection_id: str,
    target: str,
    *,
    inject_selection: bool,
) -> Response:
    headers, declared = _build_upstream_request_headers(
        request.scope,
        _hop_headers(",".join(request.headers.getlist("connection"))),
        local_gateway=True,
    )
    body = await _request_body(request, declared)
    if isinstance(body, Response):
        return body
    # HTTP framing is generated from the complete bounded body, never client metadata.
    headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
    try:
        result = await runtime.preview.content.request_finished_inside(
            conversation_id,
            contract,
            port,
            request.method,
            target,
            headers,
            body,
            projection_id=projection_id,
        )
    except Exception:  # noqa: BLE001 — a failed dispatch has an unknown outcome; never replay
        result = None
    if result is None:
        return Response("sealed runtime preview unavailable", status_code=503)
    return _application_response(
        request,
        f"http://127.0.0.1:{port}",
        result,
        inject_selection=inject_selection,
    )
