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
  collect_workspace          GET /conversations/{cid}/preview-app/<path>   preview.py (snapshot)
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
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

# ---- status vocabulary (mirrors disco.core.ConversationStatus as plain strings) --

TERMINAL_STATES = frozenset({"FINISHED", "ERROR", "STUCK", "IDLE"})
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
    ) -> None:
        self._t = transport
        self._db_path = db_path
        self._poll = poll_interval_s

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
        except (TimeoutError, ConnectionError, OSError) as exc:
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
        """Fetch each scenario-declared workspace file via the single-origin preview
        proxy (serves the live dev server OR the on-host snapshot). Returns
        {path: content} for the OutputTruthOracle. A file that cannot be served at all
        (4xx/5xx — not on the live server and not in the host snapshot) is OMITTED, so
        the oracle distinguishes a genuinely-absent deliverable (FALSE_FINISH_NO_OUTPUT)
        from a served-but-wrong one (ARTIFACT_TRUTH_MISMATCH)."""
        out: dict[str, Any] = {}
        for path in file_paths:
            rel = path.lstrip("/")
            status, text, _hdrs = await self._t.get_text(
                f"/conversations/{conversation_id}/preview-app/{rel}"
            )
            if status < 400:
                out[path] = text
        return out

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
