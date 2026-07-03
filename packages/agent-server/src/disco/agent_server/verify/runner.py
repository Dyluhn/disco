"""disco-verify API-first runner (W15).

Drives the app through its HTTP/WS API exactly like a frontend would — no
browser, no internal calls — and writes a redacted evidence dossier per run.

Key invariant (from codex): ``disco-verify`` is API-FIRST and has NO
Playwright/browser dependency. Every signal comes from the real HTTP/WS
boundary the frontend uses.

Usage::

    from disco.agent_server.verify.runner import run_scenario
    from disco.agent_server.verify.scenarios import slides_from_research_report

    result = await run_scenario(slides_from_research_report, agent_base="http://127.0.0.1:8000")
    print(result.passed, result.dossier_path)

Injectable transport (``_client`` param) keeps IO decoupled from logic so unit
tests can feed canned events without a live server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import disco.tools.verify.artifact_validators as _av
import httpx
from disco.core.evidence.schema import redact
from websockets.asyncio.client import connect as _ws_connect  # has py.typed

from .probe import app_body_problem, validate_app_deliverables
from .reliability import run_reliability_metrics
from .schema import Scenario, VerifyResult

log = logging.getLogger(__name__)

# Statuses from which the conversation will never advance without user action.
_TERMINAL: frozenset[str] = frozenset({"FINISHED", "ERROR", "STUCK", "IDLE"})
_MAX_AUTO_ANSWERS = 3  # cap auto-answers to a question-asking live model (no infinite Q&A)

# Seconds between HTTP state polls.
_POLL_INTERVAL: float = 2.0


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
        """GET the conversation's LIVE PREVIEW (the `/preview-app/` proxy the UI iframes) →
        (status, body_bytes). For a URL-less app handoff this is the REAL output the user sees
        — probing it (not the declared-artifact jail) is the production boundary (codex r7).
        Returning the BODY lets the validator reject a bare http.server directory listing that
        the preview falls back to when there's no real app entry file (codex r8)."""
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
# Production HTTP/WS client
# ---------------------------------------------------------------------------


class HttpVerifyClient(AbstractVerifyClient):
    """Production transport: drives the real app over HTTP and WebSocket."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def create_conversation(
        self, surface: str, model_override: str | None, *, appkit_mode: bool = False
    ) -> str:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
            body: dict[str, Any] = {"surface": surface, "model_override": model_override}
            if appkit_mode:
                body["appkit_mode"] = True
            resp = await hc.post("/conversations", json=body)
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
        ws_base = (
            self._base_url.replace("http://", "ws://").replace("https://", "wss://")
        )
        ws_url = f"{ws_base}/ws/conversations/{cid}"
        deadline = time.monotonic() + timeout_s
        answers = 0
        sent_cmds = False
        try:
            async with _ws_connect(ws_url) as ws:
                first_frame: dict[str, Any] = {"type": "send_message", "content": prompt}
                if send_build_brief:
                    # EPIC M: presence signals the Build first-send. The server RECOMPUTES
                    # the brief from `content` (classify_build_brief) and ignores this value,
                    # so an empty (all-default) BuildBrief is a valid, sufficient signal.
                    first_frame["build_brief"] = {}
                await ws.send(json.dumps(first_frame))
                # Drive EVERY gate to terminal on ONE long-lived WS — a live model
                # hits plan-approval AND mid-run questions, and closing the socket
                # right after approve_plan raced the frame delivery (ConnectionClosed
                # → a stuck AWAITING_PLAN_APPROVAL). Stay open until terminal/timeout.
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        log.warning(
                            "run_ws_exchange: timed out driving gates on %s", cid
                        )
                        break
                    try:
                        raw: str = await asyncio.wait_for(
                            ws.recv(decode=True), timeout=min(remaining, 10.0)
                        )
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
                        # A live model legitimately asks/clarifies; an unanswered gate
                        # stalls the run. Auto-answer via a fresh user message (the
                        # same channel the UI uses), capped so we never loop forever.
                        answers += 1
                        await ws.send(
                            json.dumps({"type": "send_message", "content": auto_answer})
                        )
                    # Gap #3: scripted extra command frames (steer/stop/resume/…),
                    # once, after the run is underway.
                    if not sent_cmds and ws_commands:
                        for cmd in ws_commands:
                            await ws.send(json.dumps(cmd))
                        sent_cmds = True
                    if status in _TERMINAL:
                        break
        except Exception as exc:  # noqa: BLE001
            log.warning("run_ws_exchange error on %s: %s", cid, exc)

    async def export_report(self, cid: str, fmt: str) -> tuple[int, bytes] | None:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=60.0, follow_redirects=True
            ) as hc:
                resp = await hc.post(
                    f"/conversations/{cid}/report/export", params={"format": fmt}
                )
                return resp.status_code, resp.content
        except Exception as exc:  # noqa: BLE001 — unreachable → report, don't crash
            log.warning("export_report(%s, %s) failed: %s", cid, fmt, exc)
            return None

    async def fire_schedule_now(self, cid: str, schedule_id: str) -> bool:
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
                resp = await hc.post(
                    f"/conversations/{cid}/schedules/{schedule_id}/fire-now"
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
        deadline = time.monotonic() + timeout_s
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
            while True:
                resp = await hc.get(f"/conversations/{cid}/state")
                resp.raise_for_status()
                state: dict[str, Any] = resp.json()
                status = str(state.get("execution_status", ""))
                if status in _TERMINAL:
                    return state
                if time.monotonic() >= deadline:
                    log.warning(
                        "poll_until_terminal: timeout for %s (last status=%s)", cid, status
                    )
                    state["_timed_out"] = True  # codex P0: a timeout must FAIL, not pass
                    return state
                await asyncio.sleep(_POLL_INTERVAL)

    async def get_events(self, cid: str) -> list[dict[str, Any]]:
        all_events: list[dict[str, Any]] = []
        after_seq: int | None = None
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
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
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
            resp = await hc.get(f"/conversations/{cid}/state")
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result

    async def get_trace(self, cid: str) -> dict[str, Any] | None:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
            resp = await hc.get(f"/api/debug/trace/{cid}")
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()
                return result
            return None

    async def get_manifest(self, cid: str) -> dict[str, Any] | None:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as hc:
            resp = await hc.get(f"/api/projects/{cid}/manifest")
            if resp.status_code == 200:
                result: dict[str, Any] = resp.json()
                return result
            return None

    async def download_artifact(self, cid: str, path: str) -> bytes | None:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=60.0) as hc:
            resp = await hc.get(f"/conversations/{cid}/artifacts/{path}")
            if resp.status_code == 200:
                return resp.content
            return None

    async def fetch_app(self, url: str) -> tuple[int, bytes] | None:
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as hc:
                resp = await hc.get(url)
                return resp.status_code, resp.content
        except Exception as exc:  # noqa: BLE001 — unreachable URL → report, don't crash
            log.warning("fetch_app(%s) failed: %s", url, exc)
            return None

    async def fetch_preview(self, cid: str) -> tuple[int, bytes] | None:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=30.0, follow_redirects=True
            ) as hc:
                resp = await hc.get(f"/conversations/{cid}/preview-app/")
                return resp.status_code, resp.content
        except Exception as exc:  # noqa: BLE001 — unreachable → report, don't crash
            log.warning("fetch_preview(%s) failed: %s", cid, exc)
            return None


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


def _locate_deliverables(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scan the event log and return workspace-relative deliverable paths.

    Recognises the same tool names as ``_common._declared_artifacts`` so that
    both the download jail and the verifier agree on what the run produced.

    Each entry is ``{"path": str, "kind": str, "tool": str}``.
    """
    deliverables: list[dict[str, Any]] = []
    for evt in events:
        kind = evt.get("kind")
        if kind == "observation":
            tr: dict[str, Any] = evt.get("tool_result") or {}
            if not tr.get("success"):
                continue
            tool_name = str(tr.get("tool_name", ""))
            structured: dict[str, Any] = tr.get("structured") or {}
            if tool_name in ("sheet_generate", "slides_generate"):
                fn = structured.get("filename")
                if isinstance(fn, str) and fn:
                    deliverables.append({"path": fn, "kind": "file", "tool": tool_name})
                es = structured.get("editable_source")
                if isinstance(es, str) and es:
                    deliverables.append({"path": es, "kind": "editable_source", "tool": tool_name})
            elif tool_name == "image_generate":
                p = structured.get("path")
                if isinstance(p, str) and p:
                    deliverables.append({"path": p, "kind": "image", "tool": tool_name})
            elif tool_name == "audio_overview":
                for key in ("mp3_path", "transcript_path"):
                    p = structured.get(key)
                    if isinstance(p, str) and p:
                        deliverables.append(
                            {"path": p, "kind": key.replace("_path", ""), "tool": tool_name}
                        )
        elif kind == "deliverable":
            artifact_kind = str(evt.get("artifact_kind", "app"))
            path = str(evt.get("path", ""))
            if artifact_kind == "files":
                if path:
                    deliverables.append({"path": path, "kind": "files", "tool": "deliverable"})
            elif artifact_kind == "app":
                # codex round-5: a live-app handoff (the build surface's PRIMARY output for
                # "make me a website") — collect it so a scenario expecting an app can't pass
                # with nothing delivered, and so its preview URL gets validated.
                deliverables.append(
                    {
                        "path": path,
                        "kind": "app",
                        "tool": "deliverable",
                        "deployment_url": str(evt.get("deployment_url", "")),
                    }
                )
    return deliverables


# File extensions the runner can download + validate. A deliverable without one of these
# (e.g. a live "app" deliverable) is NOT a file to fetch, so it's skipped — not a failure.
_VALIDATABLE_EXTS = (
    ".pdf", ".mp3", ".wav", ".xlsx", ".pptx", ".html", ".htm", ".png", ".jpg", ".jpeg"
)

# Expected-deliverable-type → the extensions that satisfy it. A scenario that declares
# `expect.deliverable_type` must actually produce a matching file (codex round-2 false-pass).
_DELIVERABLE_TYPE_EXTS: dict[str, tuple[str, ...]] = {
    "deck": (".pptx", ".pdf", ".html", ".htm"),
    "pdf": (".pdf",),
    "audio": (".mp3", ".wav"),
    "sheet": (".xlsx",),
    "image": (".png", ".jpg", ".jpeg"),
}


def _deliverable_type_satisfied(
    deliverables: list[dict[str, Any]], dtype: str | None
) -> bool:
    """True if the scenario's expected deliverable_type is satisfied. None/unrecognised type
    → no gate. ``app`` is matched by deliverable KIND (a live-app handoff, no file extension);
    other types require ≥1 deliverable with a matching file extension."""
    if not dtype:
        return True
    if str(dtype).lower() == "app":
        return any(d.get("kind") == "app" for d in deliverables)
    exts = _DELIVERABLE_TYPE_EXTS.get(str(dtype).lower())
    if exts is None:
        return True
    return any(str(d.get("path", "")).lower().endswith(exts) for d in deliverables)


def _run_forbid_checks(
    scenario: Scenario,
    deliverables: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[str]:
    """Forbid checks over the event log + deliverable list — no file IO."""
    problems: list[str] = []
    for item in scenario.forbid:
        if item == "raw_html_default":
            # codex round-4: judge only the DECK deliverables (.html/.htm/.pptx/.pdf), NOT
            # companion outputs (an editable source .json, a .png, a datasource). Otherwise a
            # companion file masks a raw-HTML deck — "not all deliverables are html" → missed.
            deck_exts = (".html", ".htm", ".pptx", ".pdf")

            def _p(d: dict[str, Any]) -> str:
                return str(d.get("path", "")).lower()

            decks = [d for d in deliverables if _p(d).endswith(deck_exts)]
            html_decks = [d["path"] for d in decks if _p(d).endswith((".html", ".htm"))]
            presentable = [d for d in decks if _p(d).endswith((".pptx", ".pdf"))]
            if html_decks and not presentable:
                problems.append(
                    f"forbid.raw_html_default: deck is raw HTML, no pptx/pdf: {html_decks}"
                )
        elif item == "procedural_image_provider":
            for evt in events:
                if evt.get("kind") != "observation":
                    continue
                tr = evt.get("tool_result") or {}
                if tr.get("tool_name") != "image_generate":
                    continue
                if not tr.get("success"):
                    continue
                structured: dict[str, Any] = tr.get("structured") or {}
                # codex round-2: the real image_generate tool emits `placeholder` /
                # `backend` / `backend_connected` — NOT a `provider` field (the old check
                # never fired). A procedural placeholder is `placeholder is True` /
                # backend "pil-procedural" / not backend_connected.
                if (
                    structured.get("placeholder") is True
                    or structured.get("backend") == "pil-procedural"
                    or structured.get("backend_connected") is False
                ):
                    problems.append(
                        "forbid.procedural_image_provider: image_generate produced a "
                        "procedural placeholder image"
                    )
                    break
        else:
            log.debug("Unknown forbid item %r — skipped", item)
    return problems


def _tool_names_used(events: list[dict[str, Any]]) -> set[str]:
    """All tool names the run touched — from ActionEvents (the tool the agent CHOSE,
    ``tool_call.tool_name``) and ObservationEvents (the tool that RAN,
    ``tool_result.tool_name``). EPIC M expect_tools/forbid_tools are judged against this:
    an attempted-but-failed call still counts as "used" so a forbidden escape can't hide
    behind a non-success result."""
    names: set[str] = set()
    for evt in events:
        kind = evt.get("kind")
        if kind == "action":
            tc = evt.get("tool_call") or {}
            name = tc.get("tool_name")
            if isinstance(name, str) and name:
                names.add(name)
        elif kind == "observation":
            tr = evt.get("tool_result") or {}
            name = tr.get("tool_name")
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _tools_with_success(events: list[dict[str, Any]]) -> set[str]:
    """Tool names that produced a SUCCESSFUL OBSERVATION (a real tool RESULT with
    ``success`` truthy) — NOT merely an emitted action. EPIC M ``expect_tools`` is judged
    against this: a golden-path tool that was ATTEMPTED but FAILED (an ActionEvent with no
    successful observation — e.g. an ``app_create`` that errored) must NOT satisfy
    ``expect_tools`` (P1 — codex). Otherwise a degenerate run where app_create was emitted
    but never succeeded would be falsely green on the golden-path check."""
    names: set[str] = set()
    for evt in events:
        if evt.get("kind") != "observation":
            continue
        tr = evt.get("tool_result") or {}
        if not tr.get("success"):
            continue
        name = tr.get("tool_name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _run_tool_checks(
    scenario: Scenario, events: list[dict[str, Any]]
) -> list[str]:
    """EPIC M: prove the run took the strict AppKit golden path. Every ``expect_tools``
    entry must have a SUCCESSFUL OBSERVATION in the event log (not just an emitted action —
    P1); no ``forbid_tools`` entry may appear at all (action OR observation, so a forbidden
    escape can't hide behind a non-success result). Proves ``app_create``/``verify_appkit_app``
    actually SUCCEEDED while the escape hatch (``request_custom_build``) and raw build tools
    (shell/file_write/code_exec) did NOT run."""
    problems: list[str] = []
    if not scenario.expect_tools and not scenario.forbid_tools:
        return problems
    # forbid: ANY use (attempted action OR observation) counts — a forbidden escape that
    # erred still escaped. expect: only a SUCCESSFUL OBSERVATION counts — an attempted-but-
    # failed golden-path tool must NOT satisfy the requirement.
    used = _tool_names_used(events)
    succeeded = _tools_with_success(events)
    missing = [t for t in scenario.expect_tools if t not in succeeded]
    if missing:
        problems.append(
            f"expect_tools: required tool(s) had no successful observation "
            f"(attempted-but-failed or never run): {missing} "
            f"(succeeded: {sorted(succeeded)}, any-use: {sorted(used)})"
        )
    present = [t for t in scenario.forbid_tools if t in used]
    if present:
        problems.append(
            f"forbid_tools: forbidden tool(s) were used: {present} — the strict "
            f"AppKit scope did not hold"
        )
    return problems


def _appkit_verdicts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every SUCCESSFUL ``verify_appkit_app`` structured verdict in the log, in order."""
    verdicts: list[dict[str, Any]] = []
    for evt in events:
        if evt.get("kind") != "observation":
            continue
        tr = evt.get("tool_result") or {}
        if tr.get("tool_name") != "verify_appkit_app" or not tr.get("success"):
            continue
        structured = tr.get("structured")
        if isinstance(structured, dict):
            verdicts.append(structured)
    return verdicts


def _run_appkit_verify_check(events: list[dict[str, Any]]) -> list[str]:
    """EPIC M: require the EPIC G structural verifier (``verify_appkit_app``) to have run
    AND passed. The LAST verdict is authoritative (the agent may iterate to green). A
    missing verdict, a failing verdict, or any failing individual check is a problem —
    each named so the dashboard/dossier can surface the first failing AppKit check."""
    verdicts = _appkit_verdicts(events)
    if not verdicts:
        return ["expect_appkit_verify: no successful verify_appkit_app observation found"]
    final = verdicts[-1]
    if final.get("passed") is True:
        return []
    checks = final.get("checks") or []
    failed = [
        str(c.get("name", "?")) for c in checks if isinstance(c, dict) and not c.get("passed")
    ]
    fp = final.get("failure_fingerprint") or ""
    detail = f" — failing checks: {failed}" if failed else ""
    fp_detail = f" (fingerprint: {fp})" if fp else ""
    return [f"expect_appkit_verify: verify_appkit_app did NOT pass{detail}{fp_detail}"]


async def _run_file_validators(
    deliverables: list[dict[str, Any]],
    *,
    client: AbstractVerifyClient,
    cid: str,
    dest_dir: Path,
) -> list[str]:
    """codex P0: DOWNLOAD each file deliverable from the app and validate the ACTUAL bytes —
    not a path that happens to exist in the runner's cwd (the old check silently skipped every
    live artifact). This is what catches a procedural-image deck or a corrupt artifact live."""
    problems: list[str] = []
    file_deliverables = [
        d for d in deliverables if str(d.get("path", "")).lower().endswith(_VALIDATABLE_EXTS)
    ]
    if file_deliverables:
        dest_dir.mkdir(parents=True, exist_ok=True)
    for d in file_deliverables:
        path = str(d["path"])
        data = await client.download_artifact(cid, path)
        if data is None:
            problems.append(f"deliverable not downloadable from the app: {path}")
            continue
        local = dest_dir / Path(path).name
        local.write_bytes(data)
        low = path.lower()
        if low.endswith(".pdf"):
            problems.extend(_av.validate_pdf(str(local)))
        elif low.endswith((".mp3", ".wav")):
            problems.extend(_av.validate_audio(str(local)))
        elif low.endswith(".xlsx"):
            problems.extend(_av.validate_sheet(str(local)))
        elif low.endswith((".pptx", ".html", ".htm")):
            # deck: detect procedural placeholder images in the real file (W3 live proof)
            problems.extend(_av.validate_deck_file(str(local)))
            if low.endswith(".pptx"):
                # codex round-3: also RENDER the pptx with LibreOffice — a zip-valid deck that
                # won't actually open would otherwise pass. Skips on hosts without soffice (the
                # PR tier); renders for real on the VM 201 nightly host.
                from disco.tools.verify.heavy_validators import validate_pptx_renders

                problems.extend(
                    p for p in validate_pptx_renders(str(local)) if "unavailable" not in p
                )
        elif low.endswith((".png", ".jpg", ".jpeg")):
            # codex round-2: a standalone image deliverable must also be checked for the
            # procedural placeholder signature (not only images embedded in a deck).
            if _av._looks_procedural(data):
                problems.append(f"procedural_placeholder_image: {path}")
    return problems


def _validate_report_export(
    result: tuple[int, bytes] | None, fmt: str, *, dest_dir: Path
) -> list[str]:
    """Gap #54: validate the REAL bytes returned by POST /report/export. Report export
    bypasses the event-log deliverable path (it's a direct blob/FSA download in the UI), so
    _locate_deliverables never sees it — this is what makes it discoverable + validatable by
    disco-verify. Must be reachable, 2xx, non-empty, and pass the format's byte check:
      • pdf  → validate_pdf on the downloaded bytes
      • md   → non-empty, UTF-8-decodable text
    """
    label = f"report.export[{fmt}]"
    if result is None:
        return [f"{label}: export endpoint not reachable"]
    status, body = result
    if not (200 <= status < 300):
        return [f"{label}: HTTP {status}"]
    if not body:
        return [f"{label}: empty body"]
    low = fmt.lower()
    if low == "pdf":
        dest_dir.mkdir(parents=True, exist_ok=True)
        local = dest_dir / "report_export.pdf"
        local.write_bytes(body)
        return [f"{label}: {p}" for p in _av.validate_pdf(str(local))]
    if low == "md":
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return [f"{label}: not UTF-8-decodable markdown"]
        if not text.strip():
            return [f"{label}: markdown body is blank"]
        return []
    # Unknown format → only the reachable+non-empty checks above apply.
    return []


async def _validate_app_deliverables(
    deliverables: list[dict[str, Any]], *, client: AbstractVerifyClient, cid: str
) -> list[str]:
    return await validate_app_deliverables(deliverables, client=client, cid=cid)


def _app_body_problem(result: tuple[int, bytes] | None, *, label: str) -> str | None:
    return app_body_problem(result, label=label)


async def _run_validators(
    scenario: Scenario,
    deliverables: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    client: AbstractVerifyClient,
    cid: str,
    dest_dir: Path,
) -> list[str]:
    """Forbid checks (no IO) + file validators on the real downloaded bytes + live-app
    reachability checks + EPIC M AppKit tool-path / verifier checks (no IO)."""
    problems = _run_forbid_checks(scenario, deliverables, events)
    problems.extend(_run_tool_checks(scenario, events))
    if scenario.expect_appkit_verify:
        problems.extend(_run_appkit_verify_check(events))
    problems.extend(
        await _run_file_validators(deliverables, client=client, cid=cid, dest_dir=dest_dir)
    )
    problems.extend(await _validate_app_deliverables(deliverables, client=client, cid=cid))
    return problems


def _write_dossier(
    dossier: Path,
    cid: str,
    scenario: Scenario,
    final_state: dict[str, Any],
    events: list[dict[str, Any]],
    trace: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
    result: VerifyResult,
) -> None:
    """Write the redacted evidence dossier to *dossier* (created if absent).

    Files written:
    - ``result.json``    — scenario + VerifyResult, redacted
    - ``events.jsonl``  — one event per line, redacted
    - ``state.json``    — final state snapshot, redacted
    - ``trace.json``    — DISCO_INSPECT trace (if available), redacted
    - ``manifest.json`` — project manifest (if available), redacted
    """
    dossier.mkdir(parents=True, exist_ok=True)

    (dossier / "result.json").write_text(
        json.dumps(
            redact(
                {
                    "run_id": dossier.name,
                    "conversation_id": cid,
                    "scenario": scenario.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                }
            ),
            indent=2,
        ),
        encoding="utf-8",
    )

    with (dossier / "events.jsonl").open("w", encoding="utf-8") as fh:
        for evt in events:
            fh.write(json.dumps(redact(evt)) + "\n")

    (dossier / "state.json").write_text(
        json.dumps(redact(final_state), indent=2),
        encoding="utf-8",
    )

    if trace is not None:
        (dossier / "trace.json").write_text(
            json.dumps(redact(trace), indent=2),
            encoding="utf-8",
        )

    if manifest is not None:
        (dossier / "manifest.json").write_text(
            json.dumps(redact(manifest), indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_scenario(
    scenario: Scenario,
    *,
    agent_base: str = "http://127.0.0.1:8000",
    timeout_s: float = 600,
    dossier_base: Path | None = None,
    _client: AbstractVerifyClient | None = None,
) -> VerifyResult:
    """Run one scenario against the live app and return a ``VerifyResult``.

    Drives the app through its HTTP/WS API (no browser, no internal calls):

    (a) POST /conversations with the scenario's surface/model_override.
    (b) Open the WS, send {"type":"send_message",...}; if ``approve_plan`` is
        True, wait for AWAITING_PLAN_APPROVAL and send {"type":"approve_plan"}.
    (c) Poll /conversations/{cid}/state until FINISHED/ERROR/STUCK/IDLE or timeout.
    (d) Fetch evidence: events, state, /api/debug/trace/{cid}, /api/projects/{cid}/manifest.
    (e) Locate deliverables from the event log.
    (f) Run forbid checks and file validators (pdf/audio/sheet from artifact_validators).
    (g) Write a redacted dossier under ``dossier_base/<run-id>/``.

    Parameters
    ----------
    scenario:
        The scenario to execute.
    agent_base:
        Base URL of the agent-server (e.g. ``http://127.0.0.1:8000``).
    timeout_s:
        Hard wall-clock timeout; effective timeout = min(scenario.timeout_s, timeout_s).
    dossier_base:
        Root directory for evidence output. Defaults to ``./test-record/disco-verify``.
    _client:
        Injectable transport for testing. Pass a ``FakeVerifyClient`` to skip live IO.
    """
    if dossier_base is None:
        dossier_base = Path("test-record") / "disco-verify"

    effective_timeout = min(float(scenario.timeout_s), timeout_s)
    client: AbstractVerifyClient = _client if _client is not None else HttpVerifyClient(agent_base)

    run_ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    run_id = f"run_{run_ts}_{uuid.uuid4().hex[:8]}"

    log.info("[%s] scenario=%r surface=%s", run_id, scenario.id, scenario.surface)

    # (a) create conversation (EPIC M: appkit_mode → strict AppKit tool allowlist)
    cid = await client.create_conversation(
        scenario.surface, scenario.model_override, appkit_mode=scenario.appkit_mode
    )
    log.info("[%s] cid=%s", run_id, cid)

    # (b) WS exchange: send message; optionally wait for plan approval; then send
    # any scripted extra command frames (gap #3 — steer/stop/resume/…).
    await client.run_ws_exchange(
        cid,
        scenario.prompt,
        approve_plan=scenario.approve_plan,
        auto_answer=scenario.auto_answer,
        timeout_s=effective_timeout,
        ws_commands=scenario.ws_commands,
        send_build_brief=scenario.send_build_brief,
    )

    # (c) poll until terminal
    final_state = await client.poll_until_terminal(cid, timeout_s=effective_timeout)
    terminal_status = str(final_state.get("execution_status", "UNKNOWN"))
    log.info("[%s] cid=%s terminal_status=%s", run_id, cid, terminal_status)

    # (d) fetch evidence — fresh snapshots after the run terminates
    events = await client.get_events(cid)
    # Re-fetch state for the dossier (clean post-run snapshot distinct from the
    # poll's last-seen state, which may have been fetched mid-sleep).
    evidence_state = await client.get_state(cid)
    trace = await client.get_trace(cid)
    manifest = await client.get_manifest(cid)

    # (e) locate deliverables
    deliverables = _locate_deliverables(events)
    log.info("[%s] deliverables=%d", run_id, len(deliverables))

    # (f) validate — download + inspect the REAL delivered bytes
    validator_problems = await _run_validators(
        scenario,
        deliverables,
        events,
        client=client,
        cid=cid,
        dest_dir=dossier_base / run_id / "artifacts",
    )
    # Gap #54: report export bypasses the event log — call it explicitly + validate bytes.
    if scenario.report_export:
        export_result = await client.export_report(cid, scenario.report_export)
        validator_problems.extend(
            _validate_report_export(
                export_result,
                scenario.report_export,
                dest_dir=dossier_base / run_id / "artifacts",
            )
        )

    # Gap #98 (REGRESSION seam): fire a schedule deterministically (no wall-clock) and
    # assert it produced a schedule_run event.
    if scenario.fire_schedule_id:
        fired = await client.fire_schedule_now(cid, scenario.fire_schedule_id)
        if not fired:
            validator_problems.append(
                f"schedule fire-now hook did not accept schedule {scenario.fire_schedule_id!r}"
            )
        else:
            post_fire_events = await client.get_events(cid)
            if not any(e.get("kind") == "schedule_run" for e in post_fire_events):
                validator_problems.append(
                    f"fired schedule {scenario.fire_schedule_id!r} produced no schedule_run event"
                )

    if validator_problems:
        log.warning("[%s] %d problem(s): %s", run_id, len(validator_problems), validator_problems)

    # compute passed
    expected_status = scenario.expect.get("terminal_status")
    # codex P0: a run that never reached a real terminal status — a timeout, or left at
    # RUNNING / AWAITING_* — must FAIL, even when the scenario declares no expected status
    # (otherwise a silent hang would PASS, defeating the whole point).
    reached_terminal = terminal_status in _TERMINAL and not final_state.get("_timed_out", False)
    status_ok = reached_terminal and (
        expected_status is None or terminal_status == str(expected_status)
    )
    # codex round-2: a scenario that EXPECTS a deliverable type (e.g. a deck) must actually
    # produce one — a FINISHED run with zero matching deliverables is a false pass.
    expected_dtype = scenario.expect.get("deliverable_type")
    deliverable_ok = _deliverable_type_satisfied(deliverables, expected_dtype)
    if not deliverable_ok:
        validator_problems.append(
            f"expected deliverable_type={expected_dtype!r} but no matching deliverable was produced"
        )
    passed = not validator_problems and status_ok and deliverable_ok

    # build result with the dossier path already set
    dossier_path = dossier_base / run_id
    result = VerifyResult(
        scenario_id=scenario.id,
        terminal_status=terminal_status,
        deliverables=deliverables,
        validator_problems=validator_problems,
        passed=passed,
        reliability_metrics=dict(run_reliability_metrics(events)),
        dossier_path=str(dossier_path.resolve()),
    )

    # (g) write dossier — use evidence_state (fresh post-run snapshot) for state.json
    _write_dossier(dossier_path, cid, scenario, evidence_state, events, trace, manifest, result)
    log.info("[%s] dossier=%s passed=%s", run_id, dossier_path, passed)

    return result
