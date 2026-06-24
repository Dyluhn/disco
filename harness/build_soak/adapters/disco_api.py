"""disco_api.py — async client that drives the LIVE Disco agent-server (PR S3).

The runner ACTS AS THE USER over the product's real surfaces (REST verbs verified in
routes/conversations.py; the plan gate + steer are WS-ONLY, verified in routes/ws.py
`_handle_frame` — there is no REST approval route):

  create_build_conversation  POST /conversations (surface="build")    conversations.py:49
                             + POST /conversations/{cid}/messages      conversations.py:149
                               (appends the user message AND kicks the loop)
  approve_plan               WS {"type":"approve_plan"}    ws.py:95 -> runtime.approve_plan
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

# Workspace-snapshot manifest bounds (Bug 9 fix): cap per-file captured content and the
# number of files walked so a pathological workspace can't blow up the dossier.
_WS_MANIFEST_MAX_BYTES = 5 * 1024 * 1024  # capture content for files up to 5 MiB
_WS_MANIFEST_MAX_FILES = 2000
_SNAPSHOT_POLL_S = 0.5  # re-read cadence while a just-finished build's snapshot flushes

# Tools that DON'T count as a "file write" for the mid-run steer trigger (the
# planning-safe read/ask set; mirrors ToolScopeOracle.PLANNING_SAFE_TOOLS).
_NON_MUTATING_TOOLS = frozenset(
    {"submit_plan", "file_read", "file_list", "search", "extract", "ask_user", "clarify", "think"}
)


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

    async def resume(self, conversation_id: str) -> dict[str, Any]:
        """Resume a PAUSED (cooperative / actionless) run — the runner ACTS AS THE
        USER who hits Resume. POST /conversations/{cid}/resume (conversations.py:284,
        the same mode-agnostic path the WS `resume` frame uses). A 409 (not resumable)
        is returned as-is so the caller can stop retrying."""
        status, data = await self._t.post_json(
            f"/conversations/{conversation_id}/resume", {}
        )
        return {"status": status, **(data if isinstance(data, dict) else {})}

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

    async def poll_until_terminal_or_gate(
        self, conversation_id: str, *, timeout_s: float
    ) -> str:
        """Poll GET /state until the run reaches a TERMINAL state or a GATE the
        runner must act on (e.g. AWAITING_PLAN_APPROVAL). Returns that status.
        Times out -> "TIMEOUT" (a product/run outcome: STUCK_RUNNING territory).

        IDLE RACE (live-surfaced): POST /messages KICKS the loop ASYNCHRONOUSLY, so a
        freshly-created conversation reads IDLE for a beat before the kick stamps
        RUNNING. IDLE is terminal for adjudication, but as a DRIVE-STOP it is ambiguous
        — pre-kick "not started yet" vs parked "nothing left to do". We only stop on
        IDLE once the run has gone active at least once (`seen_active`); a pre-kick IDLE
        is ignored so we don't bail before the agent even plans."""
        deadline = time.monotonic() + timeout_s
        seen_active = False
        while time.monotonic() < deadline:
            last = self._status_of(await self.get_state(conversation_id))
            if last in GATE_STATES:
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
            await asyncio.sleep(self._poll)
        return "TIMEOUT"

    async def poll_until_terminal(self, conversation_id: str, *, timeout_s: float) -> str:
        """Poll GET /state until a strictly TERMINAL state (no gate stop). Used after
        a plan is approved / for autonomous runs with no interactive gate. Same IDLE
        pre-kick guard as above (stop on IDLE only after the run went active)."""
        deadline = time.monotonic() + timeout_s
        seen_active = False
        while time.monotonic() < deadline:
            last = self._status_of(await self.get_state(conversation_id))
            if last in _WORK_TERMINALS:
                return last
            if last == PAUSED_STATE:
                return last  # let the driver decide (bounded resume) — never silently spin
            if last == "IDLE":
                if seen_active:
                    return last
            else:
                seen_active = True
            await asyncio.sleep(self._poll)
        return "TIMEOUT"

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

        Fallback: when no projects_root is configured (snapshot disabled) OR a declared
        file is missing from the snapshot (e.g. a live, not-yet-snapshotted run), each
        still-missing declared path is fetched via the preview proxy — so a no-storage
        deployment is never WORSE than the old behavior, only better when a snapshot exists.
        """
        declared = list(file_paths)
        manifest: dict[str, Any] = {}

        if self._projects_root is not None:
            deadline = time.monotonic() + self._snapshot_wait_s
            while True:
                manifest = self._read_snapshot_manifest(conversation_id, declared)
                missing = [p for p in declared if p not in manifest]
                if not missing or time.monotonic() >= deadline:
                    break
                # snapshot mid-flush (FINISHED was appended before _maybe_snapshot wrote
                # the workspace) — wait and re-read so a present file isn't called missing.
                await asyncio.sleep(_SNAPSHOT_POLL_S)

        # Fallback for any declared path the snapshot doesn't have: the preview proxy.
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
        self, conversation_id: str, declared: list[str]
    ) -> dict[str, Any]:
        """Walk the host ProjectStore snapshot workspace for `conversation_id` and
        build the per-file manifest. Symlink-jailed (resolve + is_relative_to) so a
        planted ``leak.html -> /etc/passwd`` can never escape the workspace. Empty dict
        when no snapshot exists yet (caller retries / falls back to the preview proxy)."""
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
        {"health": {"status": <code>}, "content": <html>, "available": <bool>}."""
        avail_status, avail = await self._t.get_json(
            f"/conversations/{conversation_id}/preview"
        )
        status, text, _hdrs = await self._t.get_text(
            f"/conversations/{conversation_id}/preview-app/"
        )
        return {
            "health": {"status": status},
            "content": text,
            "available": bool(avail.get("available")) if avail_status < 400 else False,
        }

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
