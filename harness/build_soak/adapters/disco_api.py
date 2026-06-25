"""disco_api.py — async client that drives the LIVE Disco agent-server (PR S3).

The runner ACTS AS THE USER over the product's real surfaces (REST verbs verified in
routes/conversations.py; the plan gate + steer are WS-ONLY, verified in routes/ws.py
`_handle_frame` — there is no REST approval route):

  create_build_conversation  POST /conversations (surface="build")    conversations.py:49
                             + POST /conversations/{cid}/messages      conversations.py:149
                               (appends the user message AND kicks the loop)
  approve_plan               WS {"type":"approve_plan"}    ws.py:95 -> runtime.approve_plan
  confirm                    WS {"type":"confirm"}         ws.py:89 -> runtime.confirm (clears
                             WAITING_FOR_CONFIRMATION — the confirm analogue of approve_plan;
                             a plain message does NOT clear this gate)
  send_followup (message)    WS {"type":"send_message"}    ws.py:48 -> store.append + kick
  send_followup (steer)      WS {"type":"steer",...}       ws.py:58 -> store.append(steer)+kick
  send_followup (request_plan) WS {"type":"request_plan"}  ws.py:98 -> runtime.request_plan
  poll_until_terminal        GET /conversations/{cid}/state           conversations.py:195
  collect_events             read disco.db DIRECTLY, post-terminal (race-free; trace pattern)
  collect_state              GET /conversations/{cid}/state
  collect_workspace          read the host ProjectStore SNAPSHOT directly (authoritative;
                             Bug 9 fix — NOT the dev-server preview proxy, which 404s when
                             the served app isn't up). Falls back to the preview proxy.
  collect_preview            GET /conversations/{cid}/preview + preview-app root

approve_plan / request_plan are WS-ONLY (there is no REST approval route — verified
in routes/conversations.py: only create/messages/followup/events/state/kill/resume
exist; the plan gate lives in routes/ws.py `_handle_frame`). So the runner opens a
short-lived WS, sends the one control frame, and closes — the loop state machine
acts on it; truth is read back from the durable event log / state.

INFRA GATE (codex #3): `pre_create_probe` is the ONLY place INFRA_FAILURE can come
from — a RUNNER-SIDE failure BEFORE a conversation_id exists. A post-create
driver-preflight error surfaces as StatusEvent(ERROR) AFTER the user event and is a
PRODUCT outcome (classified by the oracle), never infra.

Transport is abstracted so the deterministic tests can drive the orchestration with
a FAKE transport (no live model spend); the live path uses HttpTransport (httpx +
websockets) behind the opt-in `@pytest.mark.live` marker.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx  # the adapter MAY import an http client (oracle path stays disco/http-free)

# Pre-create infra error hierarchy (codex P1#2): the runner-side health probe fails
# with built-in OSError/ConnectionError/TimeoutError (fake transport, raw sockets) OR,
# via HttpTransport, with the httpx hierarchy — httpx.ConnectError / ConnectTimeout /
# TimeoutException / TransportError all subclass httpx.HTTPError, which does NOT
# subclass OSError. Catch BOTH so a DEAD server pre-create yields INFRA_FAILURE (§9),
# never a raw crash.
_PRECREATE_INFRA_ERRORS: tuple[type[Exception], ...] = (
    TimeoutError,
    ConnectionError,
    OSError,
    httpx.HTTPError,
)

# ---- status vocabulary (mirrors disco.core.ConversationStatus as plain strings) --

TERMINAL_STATES = frozenset({"FINISHED", "ERROR", "STUCK", "IDLE"})
# A cooperative / no-progress PAUSE (the actionless valve). NOT terminal — it is
# RESUMABLE, so the runner (acting as the user) resumes it a bounded number of times.
PAUSED_STATE = "PAUSED"
# The terminals that mean WORK ENDED (vs IDLE, which is ALSO the pre-kick resting
# state). The drive stops on these unconditionally; IDLE only after the run started.
_WORK_TERMINALS = frozenset({"FINISHED", "ERROR", "STUCK"})
# Gates the runner must ACT on (approve / answer / confirm) rather than wait through.
GATE_STATES = frozenset(
    {
        "AWAITING_PLAN_APPROVAL",
        "AWAITING_USER_DECISION",
        "AWAITING_USER_QUESTION",
        "WAITING_FOR_CONFIRMATION",
    }
)
AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
# The mid-build CLARIFY / CONFIRM gates the runner answers as the user (Bug 17): a
# capable model may ask a clarifying question (AWAITING_USER_QUESTION) or pause for a
# go-ahead (WAITING_FOR_CONFIRMATION) BEFORE/while building. The runner answers them so
# an interactive build proceeds instead of false-stalling into NO_PLAN.
AWAITING_USER_QUESTION = "AWAITING_USER_QUESTION"
WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"

# ---- progress-aware terminal-wait sentinels (Bug 15) ------------------------
# The terminal wait is PROGRESS-AWARE, not a blind wall-clock: while the conversation
# is still emitting NEW events (max seq / event count advancing) or its status is
# actively transitioning, we KEEP WAITING. A wall-clock cutoff only ends the wait when
# the run is GENUINELY INACTIVE (no progress for the inactivity window) or a generous
# HARD CAP bounds a truly-hung run. The two timeout outcomes are NOT the same finding:
#   * PROGRESSING_TIMEOUT — the hard cap was hit while the build was STILL actively
#     progressing (events advanced within the inactivity window). The runner could NOT
#     obtain a terminal verdict; this is INCONCLUSIVE (model-speed / harness limitation),
#     NOT a product BUILD_DID_NOT_FINISH → the caller records INVALID_RUN so §17 re-runs it.
#   * INACTIVE_TIMEOUT — the build went genuinely SILENT (no new events) for the inactivity
#     window: a real wedge. The caller falls through to normal classification (the
#     collected non-terminal run is a genuine finding: BUILD_DID_NOT_FINISH / STUCK).
PROGRESSING_TIMEOUT = "PROGRESSING_TIMEOUT"
INACTIVE_TIMEOUT = "INACTIVE_TIMEOUT"

# Workspace-snapshot manifest bounds (Bug 9 fix): cap per-file captured content and the
# number of files walked so a pathological workspace can't blow up the dossier.
_WS_MANIFEST_MAX_BYTES = 5 * 1024 * 1024  # capture content for files up to 5 MiB
_WS_MANIFEST_MAX_FILES = 2000
_SNAPSHOT_POLL_S = 0.5  # re-read cadence while a just-finished build's snapshot flushes
# Internal dirs the served-root index detection must skip — mirrors the product's
# lifecycle._find_snapshot_index / SandboxSession._detect_serve_dir skip set exactly, so a
# .pmx/.disco/node_modules index.html is NEVER mistaken for the build's served root.
_SERVE_SKIP_DIRS = frozenset({".pmx", ".disco", "node_modules"})
# Reserved control ports the durable serve+probe must NEVER bind (defense in depth — the
# probe binds port 0 so the OS assigns a free EPHEMERAL high port, never these): the
# agent-server (8000), the app-server (8800), and the conventional vite dev port (5173).
_RESERVED_CONTROL_PORTS = frozenset({8000, 8800, 5173})

# Tools that DON'T count as a "file write" for the mid-run steer trigger (the
# planning-safe read/ask set; mirrors ToolScopeOracle.PLANNING_SAFE_TOOLS).
_NON_MUTATING_TOOLS = frozenset(
    {"submit_plan", "file_read", "file_list", "search", "extract", "ask_user", "clarify", "think"}
)


class InconclusiveRunError(Exception):
    """A POST-create terminal-wait that could NOT obtain a verdict because the build was
    cut off by the HARD CAP while it was STILL ACTIVELY PROGRESSING (Bug 15). This is NOT
    a product failure (the loop never failed to finish — it simply hadn't finished by the
    safety ceiling) and NOT pre-create INFRA_FAILURE; the runner degrades it to INVALID_RUN
    so the §17 no-fluke policy re-runs it instead of recording a false BUILD_DID_NOT_FINISH.
    `facts` carries the progress evidence (last seq seen, elapsed) for the dossier."""

    def __init__(self, reason: str, facts: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.facts = facts or {}


class InfraProbeError(Exception):
    """A runner-side, pre-create infrastructure failure that matched an infra
    signature. Carries the matched signature id + a machine-readable detail so the
    runner can emit a §9-compliant INFRA_FAILURE record (never a product failure)."""

    def __init__(self, signature_id: str, detail: dict[str, Any]) -> None:
        super().__init__(f"infra signature matched: {signature_id}")
        self.signature_id = signature_id
        self.detail = detail


# ---- transport abstraction --------------------------------------------------


class Transport(Protocol):
    """The minimal surface the client needs. Real impl = HttpTransport; tests
    supply a fake so orchestration runs with no live model spend."""

    async def post_json(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]: ...

    async def get_json(self, path: str) -> tuple[int, dict[str, Any]]: ...

    async def get_text(self, path: str) -> tuple[int, str, dict[str, str]]: ...

    async def ws_control(self, conversation_id: str, frame: dict[str, Any]) -> None: ...

    async def health(self) -> tuple[int, dict[str, Any]]:
        """Liveness of the agent-server itself (pre-create)."""
        ...


@dataclass
class CollectedRun:
    """Everything the dossier-assembler + classifier need from one live run."""

    conversation_id: str
    events: list[dict[str, Any]]
    state_initial: dict[str, Any]
    state_final: dict[str, Any]
    workspace_manifest: dict[str, Any]
    preview: dict[str, Any] | None
    timeline: list[str] = field(default_factory=list)


# ---- the live client --------------------------------------------------------


class DiscoApiClient:
    def __init__(
        self,
        transport: Transport,
        *,
        db_path: str,
        poll_interval_s: float = 1.0,
        projects_root: str | None = None,
        snapshot_wait_s: float = 0.0,
    ) -> None:
        self._t = transport
        self._db_path = db_path
        self._poll = poll_interval_s
        # ProjectStore root for the authoritative workspace SNAPSHOT read (Bug 9 fix).
        # None ⇒ snapshot read DISABLED (deterministic fake-transport tests keep the
        # pure preview-proxy path, byte-identical to before). The live CLI passes ""
        # so it mirrors the agent-server's OWN default root resolution (same env), and
        # the snapshot unit test injects a tmp root.
        self._projects_root = projects_root
        # How long to wait for the snapshot to flush after the run reaches a terminal
        # state — the build appends FINISHED INSIDE loop.run(), then `_maybe_snapshot`
        # writes the workspace to the ProjectStore; a fast collect can read between the
        # two. Re-read the snapshot until every declared file is present (or this budget
        # elapses) so a genuinely-present file is never reported missing.
        self._snapshot_wait_s = snapshot_wait_s
        # The conversation_id this client most recently CREATED (set in
        # create_build_conversation). Lets the runner reach the cid for teardown even when
        # drive_scenario raised before returning a CollectedRun (so an abandoned RUNNING
        # build can still be killed from run_once's finally). None until a create succeeds.
        self.last_conversation_id: str | None = None

    # -- pre-create infra probe (§9; the ONLY infra source) -------------------

    async def pre_create_probe(self, model: str | None) -> None:
        """Probe the agent-server (and, transitively, the model provider) BEFORE
        creating a conversation. Raise InfraProbeError on a matched signature; the
        caller maps that to INFRA_FAILURE with bounded retry (per infra_signatures).

        We probe the server health endpoint — a connection error / 5xx here is a
        pre-create, runner-side infra condition. (A provider 429/5xx that only shows
        up mid-loop is POST-create and is therefore a PRODUCT outcome, never infra.)
        """
        try:
            status, _body = await self._t.health()
        except _PRECREATE_INFRA_ERRORS as exc:
            raise InfraProbeError(
                "agent_server_unreachable_before_conversation",
                {
                    "component": "agent_server",
                    "error_kind": _classify_conn_error(exc),
                    "error": str(exc),
                },
            ) from exc
        if status in (500, 502, 503, 504):
            raise InfraProbeError(
                "provider_5xx_before_conversation",
                {"component": "agent_server", "http_status": status},
            )
        if status == 429:
            raise InfraProbeError(
                "provider_429_before_conversation",
                {"component": "agent_server", "http_status": status},
            )
        # model is advisory here — the agent-server's own /health is the gate; a
        # bad model surfaces post-create as StatusEvent(ERROR) (a product outcome).

    # -- create + drive -------------------------------------------------------

    async def create_build_conversation(
        self, prompt: str, *, model: str | None = None, autonomous: bool = False
    ) -> str:
        """POST /conversations (surface=build) then POST the user message (which the
        route appends AND kicks). Returns the conversation_id."""
        body: dict[str, Any] = {"surface": "build", "autonomous": autonomous}
        if model:
            body["model_override"] = model
        status, data = await self._t.post_json("/conversations", body)
        if status >= 400 or "conversation_id" not in data:
            raise RuntimeError(f"create_conversation failed: HTTP {status} {data}")
        cid = str(data["conversation_id"])
        # Record the created cid IMMEDIATELY (before the kick) so the runner can tear it
        # down even if the subsequent message POST / drive raises — a created-but-abandoned
        # conversation must never leak as a RUNNING build on the shared server.
        self.last_conversation_id = cid
        # POST the user task — appends the USER message AND kicks the loop.
        mstatus, _ = await self._t.post_json(
            f"/conversations/{cid}/messages", {"content": prompt}
        )
        if mstatus >= 400:
            raise RuntimeError(f"post_message failed: HTTP {mstatus}")
        return cid

    async def approve_plan(self, conversation_id: str) -> None:
        """Approve the pending plan via the REAL WS plan gate (routes/ws.py:95)."""
        await self._t.ws_control(conversation_id, {"type": "approve_plan"})

    async def confirm(self, conversation_id: str) -> None:
        """Confirm a pending RISKY ACTION via the REAL WS confirmation gate
        (routes/ws.py:89 -> runtime.confirm -> ControlOps.confirm -> loop.confirm() + kick).
        This is the confirmation ANALOGUE of approve_plan, NOT a free-text message: a plain
        user `send_message` does NOT clear a WAITING_FOR_CONFIRMATION gate — only the dedicated
        `confirm` control frame executes the pending action and resumes the loop past the gate."""
        await self._t.ws_control(conversation_id, {"type": "confirm"})

    async def resume(self, conversation_id: str) -> dict[str, Any]:
        """Resume a PAUSED (cooperative / actionless) run — the runner ACTS AS THE
        USER who hits Resume. POST /conversations/{cid}/resume (conversations.py:284,
        the same mode-agnostic path the WS `resume` frame uses). A 409 (not resumable)
        is returned as-is so the caller can stop retrying.

        The HTTP code is returned under `http_status` (NOT `status`): the resume body
        itself carries a ``status`` field (e.g. ``{"ok": true, "status": "RUNNING"}``), so
        merging it under the same key would clobber the HTTP int with the body's STATE
        STRING — the caller's ``int(resp["status"])`` then crashed the whole drive with
        ``ValueError: invalid literal for int() ... 'RUNNING'`` the moment a build paused."""
        status, data = await self._t.post_json(
            f"/conversations/{conversation_id}/resume", {}
        )
        return {"http_status": status, **(data if isinstance(data, dict) else {})}

    async def kill(self, conversation_id: str) -> dict[str, Any]:
        """KILL (force-terminate) a conversation the runner is DONE with — the hygiene
        teardown so an abandoned RUNNING / PAUSED / AWAITING build the runner stopped
        watching (inconclusive cutoff, error path, or post-evidence release) does not
        leak and load the shared server. POST /conversations/{cid}/kill
        (conversations.py:275 — halts the agent, tears down its sandbox, revokes its
        capabilities). IDEMPOTENT: a kill on an already-terminal conversation is harmless.

        The HTTP code is returned under `http_status` (mirrors :meth:`resume`); the route
        body is ``{"killed": true, "state": {...}}``. Returns ``{"http_status": int, ...}``."""
        status, data = await self._t.post_json(
            f"/conversations/{conversation_id}/kill", {}
        )
        return {"http_status": status, **(data if isinstance(data, dict) else {})}

    async def send_followup(
        self, conversation_id: str, text: str, *, kind: str = "message"
    ) -> None:
        """Send a follow-up over the REAL WS path. `kind`:
          * "message"      -> {"type":"send_message"}  (after-terminal user follow-up:
                              appended + kick; the PRODUCT decides to re-plan)
          * "steer"        -> {"type":"steer"}         (mid-run redirect of a RUNNING
                              agent — the §15.4 after-first-file-write injection)
          * "request_plan" -> {"type":"request_plan"}  (explicit re-plan gate)
        """
        if kind == "steer":
            frame = {"type": "steer", "steer_text": text}
        elif kind == "request_plan":
            frame = {"type": "request_plan", "content": text}
        else:
            frame = {"type": "send_message", "content": text}
        await self._t.ws_control(conversation_id, frame)

    # -- polling --------------------------------------------------------------

    async def get_state(self, conversation_id: str) -> dict[str, Any]:
        status, data = await self._t.get_json(f"/conversations/{conversation_id}/state")
        if status >= 400:
            raise RuntimeError(f"get_state failed: HTTP {status}")
        return data

    @staticmethod
    def _status_of(state: dict[str, Any]) -> str:
        return str(state.get("execution_status") or state.get("status") or "")

    def _progress_marker(self, conversation_id: str) -> tuple[int, int]:
        """A cheap PROGRESS fingerprint for the conversation: (event_count, max_seq) from
        the durable event log. Either advancing means the build is STILL PRODUCING new
        events — the signal the progress-aware wait resets its inactivity timer on (Bug 15).
        A missing/locked DB reads as no-progress (-1) rather than crashing the poll."""
        uri = f"file:{self._db_path}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            return (0, -1)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(seq), -1) FROM events WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        except sqlite3.Error:
            return (0, -1)
        finally:
            conn.close()
        return (int(row[0]), int(row[1])) if row else (0, -1)

    async def _poll_progress_aware(
        self,
        conversation_id: str,
        *,
        stop_on_gate: bool,
        inactivity_s: float,
        hard_cap_s: float,
    ) -> str:
        """The shared PROGRESS-AWARE terminal wait (Bug 15). Poll GET /state until EITHER:
          (a) a genuine terminal / gate / pause status is reached → return it; OR
          (b) the build goes GENUINELY INACTIVE — no NEW events and no status change for
              `inactivity_s` → return INACTIVE_TIMEOUT (a real wedge: classify normally); OR
          (c) the generous `hard_cap_s` ceiling is hit. If the build was STILL progressing
              within the last inactivity window when the cap hit → PROGRESSING_TIMEOUT
              (inconclusive, NOT a product fail); otherwise → INACTIVE_TIMEOUT.

        A still-actively-progressing build is NEVER cut off by a mere wall-clock elapsing:
        the inactivity timer RESETS whenever the event count / max seq advances OR the status
        transitions. Only genuine silence or the safety ceiling ends the wait.

        IDLE RACE (live-surfaced): POST /messages KICKS the loop ASYNCHRONOUSLY, so a freshly
        -created conversation reads IDLE for a beat before the kick stamps RUNNING. IDLE is
        terminal for adjudication but ambiguous as a DRIVE-STOP — pre-kick "not started yet"
        vs parked "nothing left". We only stop on IDLE once the run has gone active at least
        once (`seen_active`); a pre-kick IDLE is ignored so we don't bail before it plans."""
        start = time.monotonic()
        last_progress = start
        marker = self._progress_marker(conversation_id)
        last_status: str | None = None
        seen_active = False
        while True:
            last = self._status_of(await self.get_state(conversation_id))
            if stop_on_gate and last in GATE_STATES:
                return last
            if last in _WORK_TERMINALS:
                return last
            if last == PAUSED_STATE:
                # a cooperative / actionless PAUSE — the driver decides (resume or stop).
                return last
            if last == "IDLE":
                if seen_active:
                    return last
                # pre-kick / settling — keep waiting for the loop to start.
            else:
                seen_active = True  # RUNNING or any active transitional status

            now = time.monotonic()
            # Progress = new events appeared OR the status transitioned since last poll.
            cur_marker = self._progress_marker(conversation_id)
            if cur_marker != marker or last != last_status:
                marker = cur_marker
                last_status = last
                last_progress = now

            inactive_for = now - last_progress
            if now - start >= hard_cap_s:
                # Safety ceiling. Was it still progressing recently? Then this is an
                # inconclusive model-speed cutoff, NOT a wedge → PROGRESSING_TIMEOUT.
                return (
                    PROGRESSING_TIMEOUT if inactive_for < inactivity_s else INACTIVE_TIMEOUT
                )
            if inactive_for >= inactivity_s:
                return INACTIVE_TIMEOUT
            await asyncio.sleep(self._poll)

    async def poll_until_terminal_or_gate(
        self,
        conversation_id: str,
        *,
        inactivity_s: float,
        hard_cap_s: float,
    ) -> str:
        """Progress-aware wait that ALSO stops on a GATE the runner must act on (e.g.
        AWAITING_PLAN_APPROVAL). Returns the terminal/gate/pause status, or a Bug-15
        timeout sentinel (INACTIVE_TIMEOUT / PROGRESSING_TIMEOUT)."""
        return await self._poll_progress_aware(
            conversation_id,
            stop_on_gate=True,
            inactivity_s=inactivity_s,
            hard_cap_s=hard_cap_s,
        )

    async def poll_until_terminal(
        self,
        conversation_id: str,
        *,
        inactivity_s: float,
        hard_cap_s: float,
    ) -> str:
        """Progress-aware wait to a strictly TERMINAL state (no gate stop). Used after a
        plan is approved / for autonomous runs with no interactive gate."""
        return await self._poll_progress_aware(
            conversation_id,
            stop_on_gate=False,
            inactivity_s=inactivity_s,
            hard_cap_s=hard_cap_s,
        )

    async def wait_until_status_leaves(
        self, conversation_id: str, status: str, *, timeout_s: float
    ) -> str:
        """After acting on a gate (e.g. approving a plan), wait for the status to move
        OFF that gate before driving again — so a not-yet-processed approval isn't
        re-read as the same gate and double-approved. Returns the new status (or the
        unchanged one on timeout)."""
        deadline = time.monotonic() + timeout_s
        last = status
        while time.monotonic() < deadline:
            last = self._status_of(await self.get_state(conversation_id))
            if last != status:
                return last
            await asyncio.sleep(self._poll)
        return last

    async def wait_for_first_file_write(
        self, conversation_id: str, *, timeout_s: float
    ) -> int | None:
        """Poll the DB for the first MUTATING action (the §15.4 steer trigger).
        Returns its seq, or None on timeout / terminal-without-write."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            events = self._read_events(conversation_id)
            for e in events:
                if e.get("kind") != "action":
                    continue
                tc = _payload(e).get("tool_call") or {}
                name = tc.get("tool_name")
                if name and name not in _NON_MUTATING_TOOLS:
                    return int(e.get("seq", -1))
            # bail early if the run already finished without a write
            if events and _last_terminal(events) is not None:
                return None
            await asyncio.sleep(self._poll)
        return None

    # -- collection (post-terminal, race-free) --------------------------------

    def collect_events(self, conversation_id: str) -> list[dict[str, Any]]:
        """Read the FULL event log from disco.db AFTER the run reached terminal —
        the durable, race-free source (the trace_conversation.py pattern). Returns
        DB-row dicts {seq,kind,source,id,created_at,payload(JSON string)} which the
        classifier's normalizer flattens identically to events.jsonl."""
        return self._read_events(conversation_id)

    async def collect_state(self, conversation_id: str) -> dict[str, Any]:
        return await self.get_state(conversation_id)

    async def collect_workspace(
        self, conversation_id: str, file_paths: list[str]
    ) -> dict[str, Any]:
        """Collect the conversation's workspace files AUTHORITATIVELY (Bug 9 fix).

        ROOT CAUSE this replaces: the old path fetched each declared file via the
        single-origin preview proxy (``GET …/preview-app/<path>``). That proxies the
        agent's DEV SERVER, so it returns the file ONLY when the served preview is up,
        registered, and serving that exact route — fragile. A build that genuinely
        SUCCEEDED (index.html written, served, FINISHED) produced an EMPTY manifest
        because the proxy 404'd → the OutputTruthOracle false-FAILed it
        (FALSE_FINISH_NO_OUTPUT). Verified live: ``…/preview-app/index.html``,
        ``…/workspace/index.html`` (image-only allowlist), and ``…/artifacts/index.html``
        (declared-"files" only; an app deliverable is artifact_kind="app") ALL 404 for a
        finished static build — there is no HTTP route that serves arbitrary workspace
        source. The durable, dev-server-independent truth is the host ProjectStore
        SNAPSHOT (``<projects_root>/<cid>/workspace/…``), the SAME source the product's
        own preview-edit / artifact-download routes fall back to once a run is terminal.

        Returns a manifest keyed by workspace-relative path:
            {path: {"present": True, "size": int, "sha256": hex, "content": str}}
        for EVERY file in the snapshot (a faithful workspace reflection, not just the
        declared paths), plus each declared path under its EXACT scenario-declared key
        so the OutputTruthOracle's ``path in files`` presence check matches. A declared
        file that is genuinely absent is OMITTED (never a present:false key — that would
        mask FALSE_FINISH_NO_OUTPUT into ARTIFACT_TRUTH_MISMATCH), so a real
        missing-deliverable build STILL FAILs correctly. ``content`` is the decoded UTF-8
        text (so must_contain substring checks run on the real file); a binary / oversized
        file keeps present+size+sha256 with empty content.

        Fallback: ONLY when there is NO snapshot at all (no projects_root configured, or
        the conversation's snapshot workspace dir never materialized), each declared path
        is fetched via the preview proxy — so a no-storage deployment is never WORSE than
        before. When the snapshot IS authoritative (its workspace dir exists), the proxy
        is NEVER consulted: a declared file absent from the snapshot is genuinely missing
        and is OMITTED, so the proxy can't mask a missing required deliverable with a
        served/stale copy (anti-false-PASS hole #1).
        """
        declared = list(file_paths)
        manifest: dict[str, Any] = {}
        snapshot_dir: Path | None = None

        if self._projects_root is not None:
            deadline = time.monotonic() + self._snapshot_wait_s
            while True:
                snapshot_dir = self._snapshot_workspace_dir(conversation_id)
                manifest = (
                    self._read_snapshot_manifest(conversation_id, declared, snapshot_dir)
                    if snapshot_dir is not None
                    else {}
                )
                missing = [p for p in declared if p not in manifest]
                # Stop when the snapshot exists AND is complete, or the wait budget elapsed.
                # The wait also covers the snapshot DIR not yet existing (snapshot is written
                # after FINISHED is appended inside loop.run()), so a not-yet-flushed run is
                # not mistaken for "no snapshot" → proxy mask.
                if (snapshot_dir is not None and not missing) or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(_SNAPSHOT_POLL_S)

        # The snapshot is AUTHORITATIVE: return it as-is. A declared file absent from the
        # snapshot stays OMITTED — never proxy-substituted (hole #1).
        if snapshot_dir is not None:
            return manifest

        # No snapshot at all → legacy preview-proxy fallback (no-storage deployments only).
        for path in declared:
            if path in manifest:
                continue
            rel = path.lstrip("/")
            status, text, _hdrs = await self._t.get_text(
                f"/conversations/{conversation_id}/preview-app/{rel}"
            )
            if status < 400:
                entry = _file_entry(text.encode("utf-8"))
                entry["source"] = "preview_proxy"
                manifest[path] = entry
        return manifest

    def _read_snapshot_manifest(
        self, conversation_id: str, declared: list[str], ws: Path | None = None
    ) -> dict[str, Any]:
        """Walk the host ProjectStore snapshot workspace for `conversation_id` and
        build the per-file manifest. Symlink-jailed (resolve + is_relative_to) so a
        planted ``leak.html -> /etc/passwd`` can never escape the workspace. Empty dict
        when no snapshot exists yet (caller retries / falls back to the preview proxy)."""
        if ws is None:
            ws = self._snapshot_workspace_dir(conversation_id)
        if ws is None:
            return {}
        manifest: dict[str, Any] = {}
        count = 0
        for root, _dirs, names in os.walk(ws):  # followlinks=False → no dir-symlink escape
            for name in sorted(names):
                fp = Path(root) / name
                try:
                    resolved = fp.resolve()
                    if not resolved.is_relative_to(ws) or not resolved.is_file():
                        continue
                    rel = resolved.relative_to(ws).as_posix()
                    data = resolved.read_bytes()
                except OSError:
                    continue
                manifest[rel] = _file_entry(data)
                count += 1
                if count >= _WS_MANIFEST_MAX_FILES:
                    break
            if count >= _WS_MANIFEST_MAX_FILES:
                break
        # Guarantee each DECLARED path is keyed by its EXACT scenario string (the oracle
        # checks `spec["path"] in files`); the walk keys by the leading-slash-free relpath.
        for path in declared:
            rel = path.lstrip("/")
            if path not in manifest and rel in manifest:
                manifest[path] = manifest[rel]
        return manifest

    def _snapshot_workspace_dir(self, conversation_id: str) -> Path | None:
        """The host ProjectStore ``workspace/`` directory for this conversation, or None
        when no projects_root is configured/valid or the snapshot isn't on disk yet.
        Uses the product's OWN ProjectStore so the runner resolves the SAME root the
        agent-server does (DISCO_DATA_DIR / XDG_DATA_HOME / ~/.local/share/disco/projects)."""
        if self._projects_root is None:
            return None
        try:
            from disco.tools.projects.store import ProjectStore, StorageStatus

            store = ProjectStore(self._projects_root)
            if store.status() != StorageStatus.OK:
                return None
            ws = store.path_for(conversation_id).resolve()
        except Exception:  # noqa: BLE001 — any resolution failure ⇒ no snapshot available
            return None
        return ws if ws.is_dir() else None

    async def collect_preview(self, conversation_id: str) -> dict[str, Any]:
        """Capture preview truth: availability + the served ROOT html + its HTTP
        health status. Shape consumed by OutputTruthOracle:
        {"health": {"status": <code>}, "content": <html>, "available": <bool>}.

        Bug 10 (same ephemeral-proxy fragility as Bug 9): post-FINISH the live preview
        proxy 404s because the model's served preview (a backgrounded ``python -m
        http.server``) is torn down when the run ends — false `FALSE_FINISH_PREVIEW_BROKEN`.

        TRUTH HIERARCHY (no forged 200 — every preview-OK carries GENUINE POSITIVE evidence
        the deliverable actually serves the required content):
          1. The LIVE proxy serves (status < 400 AND non-empty) → that IS the truth, used as-is.
          2. Proxy down + a STATIC served-root (``index.html``) is durable in the snapshot →
             the runner SERVES that snapshot dir itself on an OS-assigned FREE high port (never
             a reserved control port) and HTTP-PROBES it, then tears the server down. The REAL
             probe (status + body) is the evidence — independent of whether the agent ran an
             in-run verify. A non-serving / unreadable / wrong-content snapshot → the probe
             genuinely fails / lacks the needle → the preview FAILs (NOT masked). Absence of an
             in-run verify is NOT treated as a pass; the serve+probe supplies the positive proof.
          3. The build's OWN in-run web-app verification ENDED in failure (`_in_run_verify_failed`)
             → believe the agent's broken-verdict; do NOT claim OK even if the static shell serves.
          4. No static served-root (dynamic-only app, or no deliverable) → honest 404
             (FALSE_FINISH_PREVIEW_BROKEN). Live dynamic-app preview verification is the
             documented follow-up — never a forged pass.
        """
        avail_status, avail = await self._t.get_json(
            f"/conversations/{conversation_id}/preview"
        )
        status, text, _hdrs = await self._t.get_text(
            f"/conversations/{conversation_id}/preview-app/"
        )
        live = {
            "health": {"status": status},
            "content": text,
            "available": bool(avail.get("available")) if avail_status < 400 else False,
        }
        # (1) The live preview proxy actually served → that IS the truth.
        if status < 400 and text:
            return live
        # (3) The agent's own in-run verify ended in failure → believe it; no durable claim.
        if self._projects_root is None or self._in_run_verify_failed(conversation_id):
            return live
        # (4) No static served-root → honest 404 (dynamic-only / no deliverable).
        served_index = self._snapshot_served_index(conversation_id)
        if served_index is None:
            return live
        # (2) Serve the snapshot static content ourselves + PROBE it for GENUINE evidence.
        probe = await asyncio.to_thread(self._serve_probe_snapshot, served_index.parent)
        if probe is None:
            return live  # couldn't serve/probe → no positive evidence → honest 404
        probe_status, probe_body = probe
        return {
            "health": {"status": probe_status},
            "content": probe_body,
            "available": probe_status < 400,
            "source": "snapshot_serve_probe",
        }

    def _serve_probe_snapshot(self, served_dir: Path) -> tuple[int, str] | None:
        """Serve `served_dir` on an OS-assigned FREE loopback port (NEVER a reserved control
        port — port 0 lets the OS pick an ephemeral high port, guarded defensively) and
        HTTP-probe ``GET /`` for GENUINE evidence the static deliverable serves + with what
        content. Returns (status, body) or None if it could not be served/probed. The server
        is ALWAYS torn down (finally). Loopback-only bind (127.0.0.1)."""
        import functools
        import http.server
        import threading
        import urllib.error
        import urllib.request

        class _QuietHandler(http.server.SimpleHTTPRequestHandler):
            # silence per-request stderr noise; signature matches BaseHTTPRequestHandler
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        handler = functools.partial(_QuietHandler, directory=str(served_dir))
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except OSError:
            return None
        # From here `httpd` owns a bound socket — it must be closed on EVERY path, including
        # a failure to create/start the serving thread (else the socket/server leaks).
        thread: threading.Thread | None = None
        try:
            port = httpd.server_address[1]
            if port in _RESERVED_CONTROL_PORTS:  # defensive — port 0 won't pick these
                return None
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    return int(resp.status), body
            except urllib.error.HTTPError as exc:  # a real HTTP error status IS evidence
                try:
                    body = exc.read().decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    body = ""
                return int(exc.code), body
            except (urllib.error.URLError, OSError, ValueError):
                return None
        finally:
            # shutdown() only makes sense once serve_forever is actually running; server_close()
            # is ALWAYS safe and is what frees the socket if the thread never started.
            if thread is not None and thread.is_alive():
                httpd.shutdown()
            httpd.server_close()
            if thread is not None:
                thread.join(timeout=2)

    def _snapshot_served_index(self, conversation_id: str) -> Path | None:
        """The durable served-root ``index.html`` Path for a STATIC build: at the snapshot
        workspace root (preferred), else the shallowest ``index.html`` in the tree —
        MIRRORING the product's ``lifecycle._find_snapshot_index`` skip set
        (``.pmx`` / ``.disco`` / ``node_modules``) so an internal ``index.html`` is never
        picked as the served root. Symlink-jailed (resolved path must stay inside the
        workspace). None ⇒ no static served-root (so the caller does NOT claim a preview)."""
        ws = self._snapshot_workspace_dir(conversation_id)
        if ws is None:
            return None
        candidates: list[Path] = []
        root_index = ws / "index.html"
        if root_index.is_file():
            candidates = [root_index]
        else:
            try:
                candidates = sorted(
                    (
                        p
                        for p in ws.rglob("index.html")
                        if p.is_file() and not (_SERVE_SKIP_DIRS & set(p.relative_to(ws).parts))
                    ),
                    key=lambda p: (len(p.relative_to(ws).parts), str(p)),
                )
            except OSError:
                return None
        for cand in candidates:
            try:
                resolved = cand.resolve()
                if resolved.is_relative_to(ws) and resolved.is_file():
                    return resolved
            except OSError:
                continue
        return None

    def _snapshot_served_root(self, conversation_id: str) -> str | None:
        """The served-root ``index.html`` CONTENT (decoded) — thin wrapper over
        :meth:`_snapshot_served_index` (which handles root-preference, the
        ``.pmx``/``.disco``/``node_modules`` skip set, and the symlink jail). None ⇒ no
        static served-root."""
        p = self._snapshot_served_index(conversation_id)
        if p is None:
            return None
        try:
            return p.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _in_run_verify_failed(self, conversation_id: str) -> bool:
        """True iff the build's LAST in-run ``verify_web_app`` FAILED — where FAILED means
        ANY of: ``structured.passed`` is False; the tool FAILED TO EXECUTE
        (``tool_result.success`` is False, which produces NO verdict — hole #2); or it ran
        but the verdict/error signals failure. Only a verify that genuinely PASSED (ran
        successfully AND ``passed`` truthy) leaves this False, so the durable-preview
        substitution never fires off a verify that errored or failed. No verify at all ⇒
        False (a static deliverable that finished with no serve-check is not a PROVEN
        failure; the served-root presence + content truth still gate the substitution)."""
        last: dict[str, Any] | None = None
        for e in self._read_events(conversation_id):
            if e.get("kind") != "observation":
                continue
            tr = _payload(e).get("tool_result") or {}
            if tr.get("tool_name") == "verify_web_app":
                last = tr
        if last is None:
            return False
        if not last.get("success", True):
            return True  # verifier EXECUTION failure — no verdict produced (hole #2)
        raw_structured = last.get("structured")
        structured: dict[str, Any] = raw_structured if isinstance(raw_structured, dict) else {}
        if structured.get("passed") is False:
            return True
        if str(structured.get("verdict", "")).lower() in {"fail", "failed", "error", "broken"}:
            return True
        return bool(structured.get("error"))

    # -- internal: race-free DB read ------------------------------------------

    def _read_events(self, conversation_id: str) -> list[dict[str, Any]]:
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT seq, kind, source, id, created_at, payload "
                "FROM events WHERE conversation_id = ? ORDER BY seq",
                (conversation_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "seq": r["seq"],
                "kind": r["kind"],
                "source": r["source"],
                "id": r["id"],
                "created_at": r["created_at"],
                "payload": r["payload"],  # JSON string — normalizer parses it
            }
            for r in rows
        ]


# ---- helpers ----------------------------------------------------------------


def _file_entry(data: bytes) -> dict[str, Any]:
    """A single workspace-manifest entry: existence + identity (size, sha256) plus the
    decoded text content. The OutputTruthOracle reads ``content`` for must_contain
    substring checks; a binary / oversized file keeps present+size+sha256 with empty
    content (binary deliverables carry no text assertions)."""
    entry: dict[str, Any] = {
        "present": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if len(data) <= _WS_MANIFEST_MAX_BYTES:
        try:
            entry["content"] = data.decode("utf-8")
        except UnicodeDecodeError:
            entry["content"] = ""
            entry["binary"] = True
    else:
        entry["content"] = ""
        entry["truncated"] = True
    return entry


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    p = row.get("payload")
    if isinstance(p, str):
        try:
            return json.loads(p)
        except (ValueError, TypeError):
            return {}
    return p if isinstance(p, dict) else {}


def _last_terminal(rows: list[dict[str, Any]]) -> str | None:
    result: str | None = None
    for r in rows:
        if r.get("kind") != "status":
            continue
        st = str(_payload(r).get("status"))
        if st in TERMINAL_STATES:
            result = st
        elif st == "RUNNING":
            result = None
    return result


def _classify_conn_error(exc: Exception) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if "name" in text or "resolve" in text or "dns" in text:
        return "dns"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "connection_error"


# ---- real HTTP/WS transport (used by the live runner; not in CI) ------------


class HttpTransport:
    """httpx + websockets transport against a running agent-server. Imported lazily
    so the deterministic test path never needs the live deps loaded."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", *, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s

    def _ws_url(self, conversation_id: str) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[1]
        return f"{scheme}://{host}/ws/conversations/{conversation_id}"

    async def post_json(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(f"{self.base_url}{path}", json=body)
            return r.status_code, _safe_json(r)

    async def get_json(self, path: str) -> tuple[int, dict[str, Any]]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(f"{self.base_url}{path}")
            return r.status_code, _safe_json(r)

    async def get_text(self, path: str) -> tuple[int, str, dict[str, str]]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            r = await client.get(f"{self.base_url}{path}")
            return r.status_code, r.text, dict(r.headers)

    async def ws_control(self, conversation_id: str, frame: dict[str, Any]) -> None:
        import websockets

        url = self._ws_url(conversation_id)
        async with websockets.connect(url, open_timeout=self._timeout) as ws:
            # Drain the initial state frame the server sends on connect, then send
            # the control frame. We do NOT wait for an ack — truth is the event log.
            try:
                await asyncio.wait_for(ws.recv(), timeout=self._timeout)
            except TimeoutError:
                pass
            await ws.send(json.dumps(frame))
            # Give the server a beat to process before the socket closes.
            await asyncio.sleep(0.2)

    async def health(self) -> tuple[int, dict[str, Any]]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(f"{self.base_url}/health")
            return r.status_code, _safe_json(r)


def _safe_json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 — non-JSON body
        return {}
    return data if isinstance(data, dict) else {"_": data}
