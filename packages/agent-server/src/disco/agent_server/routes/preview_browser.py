"""Browser-facing preview origins, upstream reads, and live-view routes."""

from __future__ import annotations

import ipaddress
import json as _json
import re
import secrets
import urllib.parse
from typing import Any

import httpx
import idna
from disco.core import ConversationStatus, StatusEvent
from disco.core.auth import (
    PATH_PREVIEW_BOOTSTRAP_PATH,
    PREVIEW_BOOTSTRAP_PATH,
    local_preview_gateway_host,
    origin_matches_request_host,
    path_preview_host_label,
)
from disco.core.env import disco_env
from disco.core.loop.preview_target import is_managed_host_preview_port
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..auth import current_session
from ..preview_inject import inject_element_mention_picker, inject_selection_agent
from ..runtime import ConversationRuntime
from ._common import require_owned_conversation
from .preview_handoff import _capture_hooks

_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _preview_websocket_origin_allowed(
    origin: str | None,
    host: str | None,
    *,
    websocket_scheme: str,
    forwarded_proto: str | None = None,
) -> bool:
    forwarded_proto = (forwarded_proto or "").split(",", 1)[0].strip().lower()
    websocket_scheme = websocket_scheme.lower()
    expected_origin_scheme = {
        "ws": "http",
        "wss": "https",
    }.get(websocket_scheme, websocket_scheme)
    if forwarded_proto in {"http", "https"}:
        expected_origin_scheme = forwarded_proto
    try:
        origin_scheme = urllib.parse.urlsplit(origin or "").scheme.lower()
    except ValueError:
        origin_scheme = ""
    return origin_matches_request_host(origin, host) and origin_scheme == expected_origin_scheme


def _site_boundary_key(hostname: str) -> tuple[str, ...]:
    labels = tuple(part for part in hostname.lower().rstrip(".").split(".") if part)
    return labels[-2:] if len(labels) >= 2 else labels


def _canonical_dns_hostname(hostname: str) -> str:
    try:
        canonical = idna.encode(
            hostname.rstrip("."),
            uts46=True,
            transitional=False,
            std3_rules=True,
        ).decode("ascii")
    except idna.IDNAError:
        return ""
    labels = canonical.split(".")
    if (
        not canonical
        or len(canonical.encode("ascii")) > 253
        or any(not _DNS_LABEL_RE.fullmatch(label) for label in labels)
    ):
        return ""
    return canonical


def _configured_origin_parts(
    configured: str,
) -> tuple[urllib.parse.SplitResult, str, int | None]:
    try:
        parsed = urllib.parse.urlsplit(configured)
        configured_port = parsed.port
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail={"reason": "preview_origin_base_invalid"},
        ) from exc
    hostname = _canonical_dns_hostname(parsed.hostname or "")
    if not _configured_origin_valid(parsed, hostname, configured_port):
        raise HTTPException(
            status_code=500,
            detail={"reason": "preview_origin_base_invalid"},
        )
    return parsed, hostname, configured_port


def _configured_origin_valid(
    parsed: urllib.parse.SplitResult,
    hostname: str,
    configured_port: int | None,
) -> bool:
    if (
        parsed.scheme != "https"
        or not hostname
        or len(hostname.split(".")) < 2
        or configured_port == 0
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return False


def _public_ui_hostname() -> str:
    public_ui = disco_env("PUBLIC_UI_URL", "").strip()
    try:
        public_ui_url = urllib.parse.urlsplit(public_ui)
        public_ui_hostname = _canonical_dns_hostname(public_ui_url.hostname or "")
    except ValueError:
        public_ui_url = urllib.parse.SplitResult("", "", "", "", "")
        public_ui_hostname = ""
    if public_ui_url.scheme not in {"http", "https"} or not public_ui_hostname:
        raise HTTPException(
            status_code=500,
            detail={"reason": "preview_origin_base_public_ui_required"},
        )
    return public_ui_hostname


def _configured_preview_origin(
    request: Request,
    configured: str,
) -> tuple[str, str, int | None]:
    parsed, hostname, configured_port = _configured_origin_parts(configured)
    privileged_hosts = (
        _canonical_dns_hostname(request.url.hostname or "localhost"),
        _public_ui_hostname(),
    )
    if any(
        privileged and _site_boundary_key(hostname) == _site_boundary_key(privileged)
        for privileged in privileged_hosts
    ):
        raise HTTPException(
            status_code=500,
            detail={"reason": "preview_origin_base_not_isolated"},
        )
    return parsed.scheme, hostname, configured_port


def _request_preview_hostname(request: Request) -> str:
    hostname = (request.url.hostname or "localhost").lower().rstrip(".")
    if hostname in {"127.0.0.1", "::1", "localhost"}:
        return "localhost"
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        base_hostname = _canonical_dns_hostname(hostname)
        if base_hostname:
            return base_hostname
    raise HTTPException(
        status_code=409,
        detail={"reason": "preview_wildcard_dns_required"},
    ) from None


def _preview_origin_base(request: Request) -> tuple[str, str, int | None]:
    configured = disco_env("PREVIEW_ORIGIN_BASE", "").strip()
    if configured:
        return _configured_preview_origin(request, configured)
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    scheme = forwarded if forwarded in {"http", "https"} else request.url.scheme
    return scheme, _request_preview_hostname(request), request.url.port


def _preview_netloc(
    label: str,
    base_hostname: str,
    base_port: int | None,
) -> str:
    preview_host = f"{label}.{base_hostname}"
    if len(preview_host.encode("ascii")) > 253:
        raise HTTPException(
            status_code=500,
            detail={"reason": "preview_origin_host_too_long"},
        )
    return preview_host if base_port is None else f"{preview_host}:{base_port}"


def _preview_bootstrap_url(request: Request, cid8: str, port: int) -> str:
    scheme, base_hostname, base_port = _preview_origin_base(request)
    netloc = _preview_netloc(
        f"p2-{cid8}-{port}",
        base_hostname,
        base_port,
    )
    return urllib.parse.urlunparse((scheme, netloc, PREVIEW_BOOTSTRAP_PATH, "", "", ""))


def _local_preview_request(request: Request) -> bool:
    hostname = (request.url.hostname or "").lower().rstrip(".")
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _local_preview_bootstrap_url(
    request: Request,
    listener_port: int,
) -> str:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
    scheme = forwarded if forwarded in {"http", "https"} else request.url.scheme
    if scheme != "http":
        raise HTTPException(
            status_code=409,
            detail={"reason": "local_preview_https_requires_preview_origin_base"},
        )
    host = local_preview_gateway_host(listener_port)
    return f"http://{host}:{listener_port}{PREVIEW_BOOTSTRAP_PATH}"


def _path_preview_bootstrap_url(
    request: Request,
    conversation_id: str,
    port: int,
) -> str:
    cid8 = conversation_id.removeprefix("conv_")[:8]
    scheme, base_hostname, base_port = _preview_origin_base(request)
    netloc = _preview_netloc(
        path_preview_host_label(conversation_id, port),
        base_hostname,
        base_port,
    )
    path = f"{PATH_PREVIEW_BOOTSTRAP_PATH}/{cid8}"
    return urllib.parse.urlunparse((scheme, netloc, path, "", "", ""))


def _path_preview_port_from_label(
    conversation_id: str,
    request_label: str,
) -> int | None:
    for port in sorted(USER_PORTS - {NOVNC_PORT}):
        try:
            expected = path_preview_host_label(conversation_id, port)
        except ValueError:
            return None
        if secrets.compare_digest(
            expected,
            request_label.strip().lower(),
        ):
            return port
    return None


def _canonical_preview_port(
    runtime: ConversationRuntime,
    conversation_id: str,
) -> int | None:
    try:
        port = runtime.preview.preview_target_port(conversation_id)
    except Exception:  # noqa: BLE001 — corrupt selection cannot choose an upstream
        return None
    if port is None:
        return None
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or (port not in USER_PORTS and not is_managed_host_preview_port(port))
        or port == NOVNC_PORT
    ):
        return None
    return port


async def _fetch_inside_response(
    runtime: ConversationRuntime,
    conversation_id: str,
    port: int,
    rel_path: str,
    *,
    inject_selection: bool = False,
) -> Response | None:
    if port == NOVNC_PORT:
        return None
    try:
        session = runtime.live_sessions.live_session(conversation_id)
        if session is None:
            return None
        got = await session.fetch_inside(port, rel_path)
    except Exception:  # noqa: BLE001 — liveness cannot fail the route
        return None
    if got is None:
        return None
    status, body, content_type = got
    media_type = content_type or "application/octet-stream"
    if inject_selection:
        body = inject_selection_agent(body, media_type)
    return Response(
        content=inject_element_mention_picker(body, media_type),
        status_code=status,
        media_type=media_type,
    )


async def _upstream_http_response(
    runtime: ConversationRuntime,
    conversation_id: str,
    port: int,
    upstream: str,
    rel_path: str,
    *,
    inject_selection: bool,
) -> Response | None:
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(f"{upstream}/{rel_path}")
    except Exception:  # noqa: BLE001 — lost generation is unavailable
        return await _fetch_inside_response(
            runtime,
            conversation_id,
            port,
            rel_path,
            inject_selection=inject_selection,
        )
    media_type = response.headers.get("content-type", "text/html")
    body = response.content
    if inject_selection:
        body = inject_selection_agent(body, media_type)
    return Response(
        content=inject_element_mention_picker(body, media_type),
        status_code=response.status_code,
        media_type=media_type,
    )


async def _wake_for_preview(
    runtime: ConversationRuntime,
    cid8: str,
    port: int,
    *,
    owner_id: str,
) -> str | None:
    try:
        return await runtime.preview.wake_for_preview(
            cid8,
            port,
            owner_id=owner_id,
        )
    except TypeError:
        return await runtime.preview.wake_for_preview(cid8, port)


def _json_error(reason: str, message: str) -> Response:
    return Response(
        _json.dumps({"reason": reason, "message": message}),
        status_code=503,
        media_type="application/json",
    )


def _live_browser_config_error(
    runtime: ConversationRuntime,
) -> Response | None:
    try:
        config = runtime._config_store.load()
        if not config.live_browser.enabled:
            return _json_error(
                "disabled",
                "Live browser is off — enable it in Settings → Agent.",
            )
    except Exception:
        return Response(
            "config unavailable",
            status_code=503,
            media_type="text/plain",
        )
    return None


def _live_browser_session(
    runtime: ConversationRuntime,
    conversation_id: str,
) -> tuple[Any | None, Response | None]:
    session = runtime.live_sessions.live_session(conversation_id)
    if session is None:
        return None, _json_error(
            "no_sandbox",
            "No sandbox running for this conversation — start the agent first.",
        )
    if not getattr(session, "supports_live_view", False):
        return None, _json_error(
            "unsupported_backend",
            "Live browser needs the gVisor sandbox — this backend can't run it.",
        )
    return session, None


async def _start_live_stack(session: Any) -> Response | None:
    try:
        health = await session.exec_shell(
            "curl -sf http://127.0.0.1:8901/health",
            timeout_s=3,
        )
        if health.exit_code != 0:
            return _json_error(
                "no_daemon",
                "Browser daemon not running — use the browser tool first.",
            )
        job_escaped = _json.dumps({"action": "live_start"}).replace(
            "'",
            "'\"'\"'",
        )
        started = await session.exec_shell(
            "curl -s -X POST http://127.0.0.1:8901"
            " -H 'Content-Type: application/json'"
            f" -d '{job_escaped}'",
            timeout_s=30,
        )
        if started.exit_code != 0:
            return _json_error(
                "live_start_failed",
                "Failed to start live view stack.",
            )
        data = _json.loads(started.stdout)
        if not data.get("ok"):
            return _json_error(
                "live_start_failed",
                data.get("error", "live_start failed"),
            )
    except Exception as exc:  # noqa: BLE001
        return _json_error("error", str(exc)[:120])
    return None


async def _browser_live_url_response(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
    request: Request,
) -> Response:
    if runtime is None:
        return Response(
            "no runtime",
            status_code=503,
            media_type="text/plain",
        )
    owner_id = current_session(request).owner_id
    conversation_id = await require_owned_conversation(
        request,
        store,
        conversation_id,
    )
    config_error = _live_browser_config_error(runtime)
    if config_error is not None:
        return config_error
    session, session_error = _live_browser_session(
        runtime,
        conversation_id,
    )
    if session_error is not None:
        return session_error
    assert session is not None
    start_error = await _start_live_stack(session)
    if start_error is not None:
        return start_error
    cid8 = conversation_id.removeprefix("conv_")[:8]
    upstream = await _wake_for_preview(
        runtime,
        cid8,
        NOVNC_PORT,
        owner_id=owner_id,
    )
    if upstream is None:
        return _json_error(
            "no_upstream",
            "noVNC port not yet exposed by the sandbox.",
        )
    return JSONResponse(
        {
            "ready": True,
            "novnc_path": "/vnc.html?autoconnect=1&view_only=1",
            "port": NOVNC_PORT,
        }
    )


async def _browser_live_ready_response(
    runtime: ConversationRuntime | None,
    conversation_id: str,
) -> Response:
    if runtime is None:
        return JSONResponse({"ready": False, "reason": "no_runtime"})
    try:
        config = runtime._config_store.load()
        if not config.live_browser.enabled:
            return JSONResponse({"ready": False, "reason": "disabled"})
    except Exception:  # noqa: BLE001
        return JSONResponse({"ready": False, "reason": "config_unavailable"})
    session = runtime.live_sessions.live_session(conversation_id)
    if session is None:
        return JSONResponse({"ready": False, "reason": "no_sandbox"})
    if not getattr(session, "supports_live_view", False):
        return JSONResponse({"ready": False, "reason": "unsupported_backend"})
    try:
        health = await session.exec_shell(
            "curl -sf http://127.0.0.1:8901/health",
            timeout_s=3,
        )
    except Exception:  # noqa: BLE001
        return JSONResponse({"ready": False, "reason": "no_daemon"})
    if health.exit_code != 0:
        return JSONResponse({"ready": False, "reason": "no_daemon"})
    return JSONResponse({"ready": True, "reason": "ready"})


async def _best_effort_live_command(
    runtime: ConversationRuntime | None,
    conversation_id: str,
    *,
    action: str,
    timeout_s: int,
    no_runtime_note: str,
    no_session_note: str,
    failure_note: str,
) -> Response:
    if runtime is None:
        return JSONResponse({"ok": True, "note": no_runtime_note})
    session = runtime.live_sessions.live_session(conversation_id)
    if session is None:
        return JSONResponse({"ok": True, "note": no_session_note})
    try:
        job_escaped = _json.dumps({"action": action}).replace(
            "'",
            "'\"'\"'",
        )
        await session.exec_shell(
            "curl -s -X POST http://127.0.0.1:8901"
            " -H 'Content-Type: application/json'"
            f" -d '{job_escaped}'",
            timeout_s=timeout_s,
        )
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True, "note": failure_note})
    return JSONResponse({"ok": True})


def _register_live_browser_start_route(
    router: APIRouter,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
    @router.post("/conversations/{conversation_id}/browser/live-url")
    async def browser_live_url(
        conversation_id: str,
        request: Request,
    ) -> Response:
        return await _browser_live_url_response(
            store,
            runtime,
            conversation_id,
            request,
        )


def _register_live_browser_status_routes(
    router: APIRouter,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
    @router.get("/conversations/{conversation_id}/browser/live-ready")
    async def browser_live_ready(
        conversation_id: str,
        request: Request,
    ) -> Response:
        conversation_id = await require_owned_conversation(
            request,
            store,
            conversation_id,
        )
        return await _browser_live_ready_response(
            runtime,
            conversation_id,
        )

    @router.post("/conversations/{conversation_id}/browser/live-touch")
    async def browser_live_touch(
        conversation_id: str,
        request: Request,
    ) -> Response:
        conversation_id = await require_owned_conversation(
            request,
            store,
            conversation_id,
        )
        return await _best_effort_live_command(
            runtime,
            conversation_id,
            action="live_touch",
            timeout_s=5,
            no_runtime_note="no runtime",
            no_session_note="no sandbox",
            failure_note="touch best-effort",
        )

    @router.post("/conversations/{conversation_id}/browser/live-stop")
    async def browser_live_stop(
        conversation_id: str,
        request: Request,
    ) -> Response:
        conversation_id = await require_owned_conversation(
            request,
            store,
            conversation_id,
        )
        return await _best_effort_live_command(
            runtime,
            conversation_id,
            action="live_stop",
            timeout_s=10,
            no_runtime_note="no runtime",
            no_session_note="no sandbox",
            failure_note="teardown best-effort",
        )


def _register_preview_meta_routes(
    router: APIRouter,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
    @router.get("/conversations/{conversation_id}/preview")
    async def get_preview(
        conversation_id: str,
        request: Request,
    ) -> dict:
        conversation_id = await require_owned_conversation(
            request,
            store,
            conversation_id,
        )
        if runtime is None:
            return {"available": False, "reason": "no runtime"}
        try:
            events = await store.get_events(conversation_id)
        except Exception:  # noqa: BLE001 — metadata remains passive on read failure
            events = []
        latest_status = next(
            (event for event in reversed(events) if isinstance(event, StatusEvent)),
            None,
        )
        begin_capture, capture_lease = _capture_hooks(runtime)
        complete_capture = runtime.preview.complete_capture
        if (
            latest_status is not None
            and latest_status.status is ConversationStatus.FINISHED
            and begin_capture is not None
            and capture_lease is not None
        ):
            capture_generation = begin_capture(conversation_id)
            async with capture_lease(conversation_id):
                metadata = await runtime.preview.preview(conversation_id)
                complete_capture(conversation_id, capture_generation)
                begin_capture(conversation_id)
                return metadata
        return await runtime.preview.preview(conversation_id)

    @router.post("/conversations/{conversation_id}/preview/restart")
    async def ensure_preview(
        conversation_id: str,
        request: Request,
    ) -> dict:
        conversation_id = await require_owned_conversation(
            request,
            store,
            conversation_id,
        )
        if runtime is None:
            return {"ok": False}
        return {"ok": await runtime.preview.ensure_preview(conversation_id)}
