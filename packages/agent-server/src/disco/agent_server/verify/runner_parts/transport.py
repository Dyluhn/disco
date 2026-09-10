"""Injectable transport contract + production HTTP/WS client (PKG-08 extraction).

The transport layer owns the real httpx + websockets connections and the
abstract contract unit tests inject. Extracted from ``runner.py`` so the
client's public-method count and the WS/preview McCabe scores are scoped to
this module, not the whole runner.

The public symbols (``AbstractVerifyClient``, ``HttpVerifyClient``) are
re-exported by ``runner.py`` so existing imports are unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
from disco.agent_server.preview_bootstrap import (
    MAX_PREVIEW_REDEMPTION_BODY_BYTES,
    parse_preview_storage_handoff,
)
from disco.core.auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    validated_canonical_preview_url,
)
from disco.core.host_egress import EgressDenied, guarded_get
from disco.core.loop.preview_target import is_managed_host_preview_port
from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS
from websockets.asyncio.client import connect as _ws_connect  # has py.typed
from websockets.typing import Origin

log = logging.getLogger(__name__)

# Statuses from which the conversation will never advance without user action.
_TERMINAL: frozenset[str] = frozenset({"FINISHED", "ERROR", "STUCK", "IDLE"})
_MAX_AUTO_ANSWERS = 3  # cap auto-answers to a question-asking live model (no infinite Q&A)

# Seconds between HTTP state polls.
_POLL_INTERVAL: float = 2.0
_PREVIEW_STORAGE_RESET_BODY_RE = re.compile(rb'\bbody:("(?:\\.|[^"\\])*")')


# ---------------------------------------------------------------------------
# Abstract transport (injectable for testing)
# ---------------------------------------------------------------------------


class AbstractVerifyClient(ABC):
    """Injectable IO layer for ``run_scenario``.

    The production implementation (``HttpVerifyClient``) uses real httpx and
    websockets connections. Unit tests inject a ``FakeVerifyClient`` that
    returns pre-canned responses without a live server.
    """

    @abstractmethod
    async def create_conversation(
        self, surface: str, model_override: str | None, *, appkit_mode: bool = False
    ) -> str:
        """POST /conversations → return the new conversation_id.

        ``appkit_mode`` (EPIC M) creates the conversation under the strict AppKit
        tool allowlist (EPIC F) — the same flag the Build UI sends for an AppKit app.
        """
        ...

    @abstractmethod
    async def run_ws_exchange(
        self,
        cid: str,
        prompt: str,
        *,
        approve_plan: bool,
        timeout_s: float,
        ws_commands: list[dict[str, Any]] | None = None,
        auto_answer: str | None = None,
        send_build_brief: bool = False,
    ) -> None:
        """Open the WS, send the user message, optionally approve the plan, then
        send any extra ``ws_commands`` frames (gap #3 — steer/stop/resume/…).

        ``send_build_brief`` (EPIC M) attaches the Build first-send brief signal to
        the initial ``send_message`` frame (the server recomputes the brief from the
        prompt — the value is advisory). The connection is closed before returning;
        the caller then polls HTTP state separately.
        """
        ...

    @abstractmethod
    async def poll_until_terminal(
        self,
        cid: str,
        *,
        timeout_s: float,
    ) -> dict[str, Any]:
        """GET /conversations/{cid}/state in a loop until a terminal status or timeout."""
        ...

    @abstractmethod
    async def get_events(self, cid: str) -> list[dict[str, Any]]:
        """GET /conversations/{cid}/events (all pages)."""
        ...

    @abstractmethod
    async def get_state(self, cid: str) -> dict[str, Any]:
        """GET /conversations/{cid}/state (one-shot snapshot after run)."""
        ...

    @abstractmethod
    async def get_trace(self, cid: str) -> dict[str, Any] | None:
        """GET /api/debug/trace/{cid} — None when DISCO_INSPECT is off or no trace."""
        ...

    @abstractmethod
    async def get_manifest(self, cid: str) -> dict[str, Any] | None:
        """GET /api/projects/{cid}/manifest — None when project storage is unavailable."""
        ...

    async def download_artifact(self, cid: str, path: str) -> bytes | None:
        """GET /conversations/{cid}/artifacts/{path} → the real delivered file BYTES, or
        None if not downloadable. NON-abstract (default None) so fakes opt in; the live
        client overrides it. This is what lets the runner validate the ACTUAL output bytes
        instead of a path that happens to exist in the runner's cwd (codex P0)."""
        return None

    async def fetch_app(self, url: str) -> tuple[int, bytes] | None:
        """GET a live-app deliverable's preview/deployment URL → (status_code, body_bytes), or
        None if it can't be fetched. Lets the runner prove a 'build me an app' handoff is
        actually reachable + real, not just declared (codex round-5)."""
        return None

    async def fetch_preview(self, cid: str) -> tuple[int, bytes] | None:
        """Fetch the same generation-bound canonical Preview a browser receives.

        For a URL-less app handoff this is the real selected output, not the
        declared-artifact jail. The production client redeems through a clean
        cookie jar; a full application session is never sent to generated content.
        """
        return None

    async def export_report(self, cid: str, fmt: str) -> tuple[int, bytes] | None:
        """Gap #54: POST /conversations/{cid}/report/export?format=fmt → (status, body_bytes),
        or None if unreachable. Report export bypasses the event-log deliverable path, so this
        is the ONLY way the runner can see + validate it. NON-abstract (default None) so fakes
        opt in; the live client overrides it."""
        return None

    async def fire_schedule_now(self, cid: str, schedule_id: str) -> bool:
        """Gap #98 (REGRESSION seam): POST the schedule "fire now" test hook so cron-driven
        behavior runs WITHOUT waiting on wall-clock. Returns True if the hook accepted the
        request. NON-abstract default False so fakes opt in. Requires the backend test endpoint
        (cross-file dependency)."""
        return False


# ---------------------------------------------------------------------------
# Session authority — owns pairing, cookies, CSRF and WS credentials.
# ---------------------------------------------------------------------------


class _SessionAuth:
    """Sole owner of the paired agent-server session.

    Owns the base URL, browser origin, cookie jar, CSRF token and httpx
    transport, and is the only place that establishes or reads them.
    ``HttpVerifyClient`` composes one of these and delegates; no class borrows
    another class's session attributes.
    """

    def __init__(
        self,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.origin = Origin(self.base_url)
        self.transport = transport
        self._cookies = httpx.Cookies()
        self._csrf_token = ""

    def client(
        self, *, timeout: float = 30.0, follow_redirects: bool = False
    ) -> httpx.AsyncClient:
        """Return one httpx client bound to this session's jar and origin."""

        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=follow_redirects,
            cookies=self._cookies,
            headers={"Origin": self.origin},
            transport=self.transport,
        )

    async def ensure_session(self) -> None:
        """Pair with the agent server unless this session is already valid."""

        if self._csrf_token and self._cookies.get(SESSION_COOKIE):
            return
        async with self.client() as hc:
            session = await hc.get("/api/auth/session")
            body = session.json() if session.status_code == 200 else {}
            if not body.get("authenticated"):
                pairing = await hc.get("/api/auth/pairing-token")
                pairing.raise_for_status()
                token = str(pairing.json().get("pairing_token") or "")
                if not token:
                    raise RuntimeError("agent-server pairing route returned no token")
                minted = await hc.post(
                    "/api/auth/mint",
                    json={"pairing_token": token},
                    headers={"Origin": self.origin},
                )
                minted.raise_for_status()
                body = minted.json()
            self._cookies.update(hc.cookies)
        self._csrf_token = str(body.get("csrf_token") or "")
        if not self._csrf_token or not self._cookies.get(SESSION_COOKIE):
            raise RuntimeError("agent-server pairing did not establish a session")

    async def mutation_headers(self) -> dict[str, str]:
        """Return the CSRF headers required for one state-changing request."""

        await self.ensure_session()
        return {CSRF_HEADER: self._csrf_token}

    def session_cookie(self) -> str | None:
        """Return the current session cookie value, if one is established."""

        return self._cookies.get(SESSION_COOKIE)

    async def authenticated_request(
        self,
        method: str,
        path: str,
        *,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue one session-authenticated API request with CSRF when required."""

        await self.ensure_session()
        method_upper = method.upper()
        headers = dict(kwargs.pop("headers", {}) or {})
        if method_upper in {"POST", "PUT", "PATCH", "DELETE"}:
            headers.setdefault(CSRF_HEADER, self._csrf_token)
        async with self.client(timeout=timeout) as hc:
            return await hc.request(method_upper, path, headers=headers, **kwargs)

    async def websocket_credentials(self) -> tuple[Origin, dict[str, str]]:
        """Return the authenticated browser-origin/cookie headers for one WS."""

        await self.ensure_session()
        session_cookie = self.session_cookie()
        if not session_cookie:
            raise RuntimeError("agent-server WebSocket has no authenticated session cookie")
        return self.origin, {"Cookie": f"{SESSION_COOKIE}={session_cookie}"}


# ---------------------------------------------------------------------------
# Production HTTP/WS client
# ---------------------------------------------------------------------------


class HttpVerifyClient(AbstractVerifyClient):
    """Production transport: drives the real app over HTTP and WebSocket.

    Session establishment, CSRF and WS credentials belong to the composed
    ``auth`` collaborator, which is public so consumers that need only the
    authenticated-request surface can hold it directly.
    """

    def __init__(
        self,
        base_url: str,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.auth = _SessionAuth(base_url, _transport)

    async def create_conversation(
        self, surface: str, model_override: str | None, *, appkit_mode: bool = False
    ) -> str:
        headers = await self.auth.mutation_headers()
        async with self.auth.client() as hc:
            body: dict[str, Any] = {"surface": surface, "model_override": model_override}
            if appkit_mode:
                body["appkit_mode"] = True
            resp = await hc.post("/conversations", json=body, headers=headers)
            resp.raise_for_status()
            return str(resp.json()["conversation_id"])

    async def run_ws_exchange(
        self,
        cid: str,
        prompt: str,
        *,
        approve_plan: bool,
        timeout_s: float,
        ws_commands: list[dict[str, Any]] | None = None,
        auto_answer: str | None = None,
        send_build_brief: bool = False,
    ) -> None:
        await self.auth.ensure_session()
        ws_base = self.auth.base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_base}/ws/conversations/{cid}"
        deadline = time.monotonic() + timeout_s
        session_cookie = self.auth.session_cookie()
        if not session_cookie:
            raise RuntimeError("agent-server WebSocket has no authenticated session cookie")
        async with _ws_connect(
            ws_url,
            origin=self.auth.origin,
            additional_headers={"Cookie": f"{SESSION_COOKIE}={session_cookie}"},
        ) as ws:
            # The server's first application frame is the connection-time state
            # snapshot. Consume it before submitting a new run: a fresh
            # conversation is IDLE, and treating that pre-message snapshot as
            # the submitted run's terminal state closes the socket before plan
            # approval can be delivered.
            await _receive_initial_state(ws, deadline=deadline)
            await _send_first_frame(ws, prompt, send_build_brief)
            await _drive_ws_gates(
                ws,
                cid,
                deadline=deadline,
                approve_plan=approve_plan,
                auto_answer=auto_answer,
                ws_commands=ws_commands,
            )

    async def export_report(self, cid: str, fmt: str) -> tuple[int, bytes] | None:
        try:
            headers = await self.auth.mutation_headers()
            async with self.auth.client(timeout=60.0, follow_redirects=True) as hc:
                resp = await hc.post(
                    f"/api/conversations/{cid}/report/export",
                    params={"fmt": fmt},
                    headers=headers,
                )
                return resp.status_code, resp.content
        except Exception as exc:  # noqa: BLE001 — unreachable → report, don't crash
            log.warning("export_report(%s, %s) failed: %s", cid, fmt, exc)
            return None

    async def fire_schedule_now(self, cid: str, schedule_id: str) -> bool:
        try:
            headers = await self.auth.mutation_headers()
            async with self.auth.client() as hc:
                resp = await hc.post(
                    f"/conversations/{cid}/schedules/{schedule_id}/fire-now",
                    headers=headers,
                )
                return 200 <= resp.status_code < 300
        except Exception as exc:  # noqa: BLE001
            log.warning("fire_schedule_now(%s, %s) failed: %s", cid, schedule_id, exc)
            return False

    async def poll_until_terminal(
        self,
        cid: str,
        *,
        timeout_s: float,
    ) -> dict[str, Any]:
        await self.auth.ensure_session()
        deadline = time.monotonic() + timeout_s
        async with self.auth.client() as hc:
            while True:
                resp = await hc.get(f"/conversations/{cid}/state")
                resp.raise_for_status()
                state: dict[str, Any] = resp.json()
                status = str(state.get("execution_status", ""))
                if status in _TERMINAL:
                    return state
                if time.monotonic() >= deadline:
                    log.warning("poll_until_terminal: timeout for %s (last status=%s)", cid, status)
                    state["_timed_out"] = True  # codex P0: a timeout must FAIL, not pass
                    return state
                await asyncio.sleep(_POLL_INTERVAL)

    async def get_events(self, cid: str) -> list[dict[str, Any]]:
        await self.auth.ensure_session()
        all_events: list[dict[str, Any]] = []
        after_seq: int | None = None
        async with self.auth.client() as hc:
            while True:
                params: dict[str, Any] = {"limit": 200}
                if after_seq is not None:
                    params["after_seq"] = after_seq
                resp = await hc.get(f"/conversations/{cid}/events", params=params)
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                page_events: list[dict[str, Any]] = data.get("events") or []
                all_events.extend(page_events)
                next_cursor = data.get("next_cursor")
                if next_cursor is None:
                    break
                after_seq = int(next_cursor)
        return all_events

    async def get_state(self, cid: str) -> dict[str, Any]:
        await self.auth.ensure_session()
        async with self.auth.client() as hc:
            resp = await hc.get(f"/conversations/{cid}/state")
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def get_trace(self, cid: str) -> dict[str, Any] | None:
        await self.auth.ensure_session()
        async with self.auth.client() as hc:
            resp = await hc.get(f"/api/debug/trace/{cid}")
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()
                return result
            return None

    async def get_manifest(self, cid: str) -> dict[str, Any] | None:
        await self.auth.ensure_session()
        async with self.auth.client() as hc:
            resp = await hc.get(f"/api/projects/{cid}/manifest")
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()
                return result
            return None

    async def download_artifact(self, cid: str, path: str) -> bytes | None:
        await self.auth.ensure_session()
        async with self.auth.client(timeout=60.0) as hc:
            resp = await hc.get(f"/conversations/{cid}/artifacts/{path}")
            if resp.status_code == 200:
                return resp.content
            return None

    async def fetch_app(self, url: str) -> tuple[int, bytes] | None:
        """Fetch a MODEL-AUTHORED deployment URL under the host egress policy.

        This runs in the agent-server process — on the host network, with the
        container socket mounted — so an unguarded fetch here is a server-side
        request forgery primitive whose response bytes flow back into the verify
        report. ``_canonical_deployment_url`` range-checks bare IP literals only
        and returns any DNS hostname unchanged, so a name resolving to link-local
        metadata, loopback (disco's own API) or an ``*.internal`` address reaches
        this call, as does a public host that redirects there.

        :func:`guarded_get` resolves before connecting, rejects every non-global
        address, re-validates each redirect hop, checks the peer it actually
        connected to (so a DNS rebind loses the race), strips credentials across
        hops and ignores proxy environment variables.

        A denied URL is reported, not raised: returning ``None`` is the existing
        "unreachable" signal that ``app_body_problem`` turns into a report entry,
        so a blocked target fails the deliverable instead of crashing the run.
        """
        try:
            resp = await guarded_get(url, timeout_s=30.0)
            return resp.status_code, resp.content
        except EgressDenied as exc:
            # Distinct from "unreachable": the operator needs to see that WE
            # refused the target, not that the deployment is down.
            log.warning("fetch_app(%s) blocked by host egress policy: %s", url, exc)
            return None
        except Exception as exc:  # noqa: BLE001 — unreachable URL → report, don't crash
            log.warning("fetch_app(%s) failed: %s", url, exc)
            return None

    async def fetch_preview(self, cid: str) -> tuple[int, bytes] | None:
        try:
            headers = await self.auth.mutation_headers()
            async with self.auth.client() as hc:
                minted = await hc.post(
                    f"/conversations/{cid}/preview/capability",
                    headers=headers,
                    json={
                        "target_path": "/",
                        "transport": "canonical",
                    },
                )
            if minted.status_code != 200:
                return minted.status_code, minted.content
            body = minted.json()
            preview_target = _resolve_preview_target(self.auth.base_url, cid, body)
            if preview_target is None:
                return None
            bootstrap_url, intent, canonical_url = preview_target
            return await _redeem_preview(
                bootstrap_url, intent, canonical_url, self.auth.origin, self.auth.transport
            )
        except Exception as exc:  # noqa: BLE001 — unreachable → report, don't crash
            log.warning("fetch_preview(%s) failed: %s", cid, exc)
            return None


# ---------------------------------------------------------------------------
# WS exchange helpers (extracted so run_ws_exchange stays under the McCabe cap)
# ---------------------------------------------------------------------------


async def _send_first_frame(ws: Any, prompt: str, send_build_brief: bool) -> None:
    """Send the initial ``send_message`` frame, optionally with the Build brief signal."""
    first_frame: dict[str, Any] = {"type": "send_message", "content": prompt}
    if send_build_brief:
        # EPIC M: presence signals the Build first-send. The server RECOMPUTES
        # the brief from `content` (classify_build_brief) and ignores this value,
        # so an empty (all-default) BuildBrief is a valid, sufficient signal.
        first_frame["build_brief"] = {}
    await ws.send(json.dumps(first_frame))


async def _receive_initial_state(ws: Any, *, deadline: float) -> dict[str, Any]:
    """Consume the server's mandatory connection-time state snapshot.

    This frame describes the conversation *before* the new user message.  It
    therefore cannot be used to decide whether the new run is terminal.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("conversation WebSocket timed out before initial state")
    raw: str = await asyncio.wait_for(ws.recv(decode=True), timeout=min(remaining, 10.0))
    frame: Any = json.loads(raw)
    if not isinstance(frame, dict) or frame.get("type") != "state":
        raise RuntimeError("conversation WebSocket did not begin with a state frame")
    state = frame.get("state")
    if not isinstance(state, dict):
        raise RuntimeError("conversation WebSocket initial state frame is malformed")
    return state


async def _drive_ws_gates(
    ws: Any,
    cid: str,
    *,
    deadline: float,
    approve_plan: bool,
    auto_answer: str | None,
    ws_commands: list[dict[str, Any]] | None,
) -> None:
    """Drive EVERY gate to terminal on ONE long-lived WS — a live model hits
    plan-approval AND mid-run questions, and closing the socket right after
    approve_plan raced the frame delivery (ConnectionClosed → a stuck
    AWAITING_PLAN_APPROVAL). Stay open until terminal/timeout."""
    answers = 0
    sent_cmds = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning("run_ws_exchange: timed out driving gates on %s", cid)
            break
        try:
            raw: str = await asyncio.wait_for(ws.recv(decode=True), timeout=min(remaining, 10.0))
        except TimeoutError:
            continue
        frame: dict[str, Any] = json.loads(raw)
        status = _status_from_frame(frame)
        if status == "AWAITING_PLAN_APPROVAL" and approve_plan:
            await ws.send(json.dumps({"type": "approve_plan"}))
        elif (
            status in ("AWAITING_USER_QUESTION", "AWAITING_USER_DECISION")
            and auto_answer is not None
            and answers < _MAX_AUTO_ANSWERS
        ):
            # A live model legitimately asks/clarifies; an unanswered gate stalls
            # the run. Auto-answer via a fresh user message (the same channel the
            # UI uses), capped so we never loop forever.
            answers += 1
            await ws.send(json.dumps({"type": "send_message", "content": auto_answer}))
        # Gap #3: scripted extra command frames (steer/stop/resume/…), once,
        # after the run is underway.
        if not sent_cmds and ws_commands:
            for cmd in ws_commands:
                await ws.send(json.dumps(cmd))
            sent_cmds = True
        if status in _TERMINAL:
            break


# ---------------------------------------------------------------------------
# Preview capability helpers (extracted so fetch_preview stays under the McCabe cap)
# ---------------------------------------------------------------------------


def _resolve_preview_target(
    base_url: str, cid: str, body: dict[str, Any]
) -> tuple[str, str, str] | None:
    """Validate the preview-capability response and return
    ``(bootstrap_url, intent, canonical_url)`` or None if it must fail closed."""
    selected_port = body.get("port")
    if (
        body.get("transport") != "canonical"
        or body.get("target_path") != "/"
        or not isinstance(body.get("preview_authority"), str)
        or not body.get("preview_authority")
        or not isinstance(selected_port, int)
        or isinstance(selected_port, bool)
        or (selected_port not in USER_PORTS and not is_managed_host_preview_port(selected_port))
        or selected_port == NOVNC_PORT
    ):
        return None
    bootstrap_url = str(body.get("bootstrap_url") or "")
    intent = str(body.get("bootstrap_intent") or "")
    canonical_url = validated_canonical_preview_url(
        base_url,
        cid,
        selected_port,
        bootstrap_url,
    )
    if not intent or canonical_url is None:
        return None
    return bootstrap_url, intent, canonical_url


async def _redeem_preview(
    bootstrap_url: str,
    intent: str,
    canonical_url: str,
    origin: Origin,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[int, bytes] | None:
    """Redeem the one-use bootstrap intent through a clean cookie jar and fetch
    the canonical preview URL.

    Deliberately clean: the verifier's full application session stays out of
    generated content. The bootstrap response installs only path-preview
    capability cookies. Fail closed if a route regression ever tries to smuggle
    a full app session into generated content.
    """
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as preview:
        redeemed = await preview.post(
            bootstrap_url,
            data={"intent": intent},
            headers={"Origin": origin},
        )
        if redeemed.status_code != 200:
            return redeemed.status_code, redeemed.content
        if "cookies" in redeemed.headers.get("clear-site-data", "").lower():
            completed = await _complete_preview_storage_reset(
                preview,
                redeemed,
                bootstrap_url=bootstrap_url,
                origin=origin,
            )
            if completed is None:
                return None
            if completed.status_code != 200:
                return completed.status_code, completed.content
        if any(cookie.name == SESSION_COOKIE for cookie in preview.cookies.jar):
            # A path bootstrap must install only its scoped preview cookies.
            # Fail closed if a route regression ever tries to smuggle a full
            # app session into generated content.
            return None
        resp = await preview.get(canonical_url)
        return resp.status_code, resp.content


async def _complete_preview_storage_reset(
    preview: httpx.AsyncClient,
    redeemed: httpx.Response,
    *,
    bootstrap_url: str,
    origin: Origin,
) -> httpx.Response | None:
    """Complete the browser document's bounded second POST without executing JS."""

    if tuple(preview.cookies.jar):
        return None
    handoff = _preview_storage_handoff(redeemed.content)
    if handoff is None:
        return None
    completed = await preview.post(
        bootstrap_url,
        data={"handoff": handoff},
        headers={"Origin": origin},
    )
    if any(cookie.name == SESSION_COOKIE for cookie in preview.cookies.jar):
        return None
    if completed.status_code != 200:
        return completed
    try:
        payload = completed.json()
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("target") != "/":
        return None
    return completed


def _preview_storage_handoff(document: bytes) -> str | None:
    """Extract one bounded handoff from the product's locked reset document."""

    if not document or len(document) > MAX_PREVIEW_REDEMPTION_BODY_BYTES:
        return None
    matches = _PREVIEW_STORAGE_RESET_BODY_RE.findall(document)
    if len(matches) != 1:
        return None
    try:
        encoded = json.loads(matches[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(encoded, str):
        return None
    return parse_preview_storage_handoff(encoded.encode("utf-8"))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _status_from_frame(frame: dict[str, Any]) -> str:
    """Extract an execution_status string from a WS server frame, or ''."""
    if frame.get("type") == "state":
        state = frame.get("state") or {}
        return str(state.get("execution_status", ""))
    if frame.get("type") == "event":
        event = frame.get("event") or {}
        if event.get("kind") == "status":
            return str(event.get("status", ""))
    return ""


__all__ = [
    "AbstractVerifyClient",
    "HttpVerifyClient",
    "_MAX_AUTO_ANSWERS",
    "_POLL_INTERVAL",
    "_TERMINAL",
    "_status_from_frame",
]
