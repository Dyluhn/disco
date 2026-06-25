"""`PiKernel` — the experimental Pi-SDK Build kernel (REAL, Wave 2 Batch 3).

This is the keystone: the `BuildKernel` seam the runtime resolves to when Settings
selects `pi_experimental` AND the experimental flag is on. It hosts the Node Pi
sidecar (`PiProcess`, B2) driving the UI-selected model over the loopback Disco
inference gateway (EPIC C) with an EPHEMERAL run token — Pi never sees a provider
key. Pi's built-in tools are off; every tool runs through the D2 HTTP tool bridge
(`routes/pi_tools.py`). This module:

  * spawns + drives the sidecar (start / send_user_turn), ISSUING the run token and
    injecting it as `init.config.gateway.apiKey` (+ `bridge` so tools work);
  * drains the sidecar's outbound frames, maps them to Disco events (I1
    `map_pi_event`) and appends them to the SAME event store the UI subscribes to;
  * holds the PLAN gate (E1/E2/E3) + the ask/clarify gates PYTHON-side: the bridge
    long-polls an asyncio future this kernel resolves on approve/reject/answer;
  * emits the §7.11 inspect spans (I2) and writes the sanitized session artifact
    (I3) on finish/cancel; revokes the run token + kills the process tree on stop.

No-recursion rule (mirrors `DiscoKernel`): this delegates ONLY to the runtime's
collaborators / inner resolvers (`_rt._store`, `_rt._config_store`,
`_rt._model_override`, `_rt._pi_token_store`, `_rt._revoke_pi_tokens`) — NEVER to
the runtime's PUBLIC control methods (those route back through the active kernel).

Deliberate deviation from the campaign's "approve_plan calls `_rt._control.
approve_plan`" note: for a Pi-driven conversation there is NO Disco `AgentLoop` —
`_control.approve_plan` would COMPOSE + `kick` a real Disco loop, which would race
the live Pi session. So PiKernel resolves the gate future + appends the status
transition to the store DIRECTLY (no recursion, no Disco loop). The plan TYPE
(`PlanEvent`/`AWAITING_PLAN_APPROVAL`) is still Disco's — only the resume vehicle
differs (a long-poll future, not the loop's plan-mode flag).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from disco.core import (
    ClarifyEvent,
    ClarifyQuestionItem,
    ConversationState,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
    ToolResult,
)
from disco.core.env import disco_env
from disco.core.inspect import inspect_enabled
from disco.core.llm import ModelRole
from disco.core.obs import log_event

from ..build_messages import _context_message, _user_message
from .base import KernelEvent
from .pi_event_mapper import map_pi_event
from .pi_process import KernelInitConfig, PiProcess, default_pi_kernel_entry
from .pi_session_artifact import write_pi_session_artifact

if TYPE_CHECKING:
    from ..runtime import ConversationRuntime

_LOG = logging.getLogger("disco.pi_kernel")

# The model alias Pi sees over the gateway. The gateway PINS the real model by the
# run token, ignoring this value — it is purely cosmetic on the wire (campaign §4.2).
_GATEWAY_MODEL_ALIAS = "disco-selected"

# Run-token lifetime + budget. Short-lived (the store also caps TTL to its own
# ceiling) and capped so a runaway sidecar cannot spend unbounded tokens.
_RUN_TOKEN_TTL_S = 2 * 3600.0
_RUN_TOKEN_BUDGET = 4_000_000

# Tools that MUTATE the workspace — blocked until a plan is approved (E1/E3). The
# allowlist + Action/Observation pairing still live in the bridge; this is the
# plan-gate overlay on top.
_WRITE_TOOLS: frozenset[str] = frozenset(
    {"file_write", "file_replace_lines", "file_insert_lines", "shell_exec"}
)


@dataclass
class _PiSession:
    """The live state for one conversation's Pi sidecar run."""

    kernel_id: str
    token: str
    proc: PiProcess | None = None
    drain_task: asyncio.Task[None] | None = None
    finished: bool = False
    # The raw OUTBOUND frames, kept for the I3 sanitized session artifact.
    frames: list[Mapping[str, Any]] = field(default_factory=list)


class PiKernel:
    """Real `BuildKernel` hosting the Pi sidecar. Holds a back-ref to the runtime
    (like `DiscoKernel`/`ControlOps`) plus a per-conversation session registry and
    the plan/ask gate futures the bridge long-polls."""

    name: ClassVar[str] = "pi_experimental"

    def __init__(
        self,
        runtime: ConversationRuntime | Any,
        *,
        node_bin: str = "node",
        entry: str | None = None,
        cwd: str | None = None,
        artifact_base: str | None = None,
    ) -> None:
        self._rt = runtime
        # Spawn config — overridable for tests (a fake Python sidecar); defaults to
        # the real built Node sidecar resolved repo-relative.
        self._node_bin = node_bin
        self._entry = entry
        self._cwd = cwd
        self._artifact_base_override = artifact_base
        # Per-conversation live sessions: (PiProcess, drain_task, run token, kernel_id).
        self._sessions: dict[str, _PiSession] = {}
        # Conversations this kernel is DRIVING (used to scope the write-before-plan
        # gate so the raw D2 bridge — no managed session — is unaffected).
        self._managed: set[str] = set()
        # Whether a plan has been APPROVED for a conversation (writes gated on this).
        self._plan_approved: dict[str, bool] = {}
        # The plan-approval gate future the bridge's submit_plan call long-polls;
        # resolved by approve_plan / reject_plan / request_plan.
        self._plan_gates: dict[str, asyncio.Future[str]] = {}
        # The ask/clarify gate future the bridge's ask_user/clarify call long-polls;
        # resolved by the user's next turn (send_user_turn) or pick_alternative.
        self._ask_gates: dict[str, asyncio.Future[str]] = {}

    # -- inspect spans (I2) ---------------------------------------------------

    def _span(self, name: str, conversation_id: str, **fields: Any) -> None:
        """Emit one §7.11 inspect span into the shared inspect sink (the `disco.span`
        logger the runtime's `_SpanHandler` captures, keyed by `cid`). Gated on
        `DISCO_INSPECT=1` so it is zero-cost when off."""
        if not inspect_enabled():
            return
        with contextlib.suppress(Exception):
            log_event(name, cid=conversation_id, **fields)

    # -- spawn config ---------------------------------------------------------

    def _spawn_entry(self) -> str:
        return self._entry or str(default_pi_kernel_entry())

    def _spawn_cwd(self) -> str:
        if self._cwd is not None:
            return self._cwd
        return str(Path(self._spawn_entry()).resolve().parents[1])

    def _gateway_base_url(self) -> str:
        """The loopback inference-gateway base URL (`…/internal/pi-kernel/v1`); the
        Pi provider appends `/chat/completions`. Always 127.0.0.1 — the sidecar runs
        on this host and the provider asserts a loopback host."""
        port = disco_env("PORT", "8000") or "8000"
        return f"http://127.0.0.1:{port}/internal/pi-kernel/v1"

    def _bridge_base_url(self) -> str:
        """The loopback tool-bridge base URL; `tools.ts` appends
        `/internal/pi-kernel/{kernelId}/tools/{name}`."""
        port = disco_env("PORT", "8000") or "8000"
        return f"http://127.0.0.1:{port}"

    def _resolve_model_key(self, conversation_id: str) -> str:
        """The SELECTED model's catalogue key for the run token: the conversation's
        explicit pick if it is a known model, else the AGENT_DRIVER assignment. The
        gateway resolves this key → provider/model and decrypts the key (Pi never
        sees it)."""
        cfg = self._rt._config_store.load()
        override = self._rt._model_override.get(conversation_id)
        if override and override in cfg.models:
            return override
        return cfg.model_for(ModelRole.AGENT_DRIVER)

    def _build_init(self, conversation_id: str, kernel_id: str, token: str) -> KernelInitConfig:
        """The sidecar `init.config`: the loopback gateway (carrying ONLY the
        ephemeral run token as `apiKey`) + the tool bridge (reusing the same token).
        Cast to `KernelInitConfig` — `bridge` rides the wire (protocol.ts) even
        though the B2 mirror TypedDict predates that field."""
        init: dict[str, Any] = {
            "conversationId": conversation_id,
            "gateway": {
                "baseUrl": self._gateway_base_url(),
                "model": _GATEWAY_MODEL_ALIAS,
                "apiKey": token,
                "api": "openai-completions",
            },
            "bridge": {
                "baseUrl": self._bridge_base_url(),
                "kernelId": kernel_id,
            },
        }
        return cast("KernelInitConfig", init)

    # -- lifecycle ------------------------------------------------------------

    def start(self, conversation_id: str) -> None:
        """Schedule the conversation's Pi run (idempotent: a no-op while a live
        session exists). Spawning is async, so this schedules a background bootstrap
        task (mirrors `ConversationRuntime.kick`, which schedules the Disco loop)."""
        session = self._sessions.get(conversation_id)
        if session is not None and not session.finished:
            return
        self._managed.add(conversation_id)
        asyncio.ensure_future(self._bootstrap(conversation_id, prompt_text=None))

    async def send_user_turn(
        self,
        conversation_id: str,
        text: str,
        *,
        context: str | None = None,
        steer: bool = False,
    ) -> MessageEvent:
        """Append a user turn and start/continue the Pi run. If an ask/clarify gate is
        pending, this turn is the user's ANSWER → resolve the held bridge call and
        return (Pi continues its in-flight tool). Otherwise it (re)starts the session
        (fresh) or forwards as a follow-up (live). Returns the stored USER message."""
        self._managed.add(conversation_id)
        if context:
            await self._rt._store.append(conversation_id, _context_message(context))
        stored = await self._rt._store.append(
            conversation_id, _user_message(text, steer=steer)
        )

        # An ask_user / clarify gate is held PYTHON-side over the bridge HTTP — the
        # user's reply IS the resume signal. Resolve it; Pi's tool call returns.
        gate = self._ask_gates.get(conversation_id)
        if gate is not None and not gate.done():
            await self._rt._store.append(
                conversation_id, StatusEvent(status=ConversationStatus.RUNNING)
            )
            gate.set_result(text)
            return cast("MessageEvent", stored)

        session = self._sessions.get(conversation_id)
        if session is not None and not session.finished and session.proc is not None:
            # A live session: deliver as a follow-up so it lands after the current
            # turn (a fresh `prompt` would be rejected mid-turn by the sidecar).
            with contextlib.suppress(Exception):
                await session.proc.followup(text)
            return cast("MessageEvent", stored)

        await self._bootstrap(conversation_id, prompt_text=text)
        return cast("MessageEvent", stored)

    async def _bootstrap(self, conversation_id: str, *, prompt_text: str | None) -> None:
        """Resolve the model, ISSUE the run token, spawn + handshake the sidecar, start
        the drain task, and send the first prompt. On any failure: revoke the token,
        tear down, and append an error event (never leave a half-spawned session)."""
        try:
            model_key = self._resolve_model_key(conversation_id)
            self._span("model_selected", conversation_id, model_key=model_key)

            store = self._rt._pi_token_store
            if store is None:
                raise RuntimeError("Pi inference gateway token store is not wired")
            kernel_id = f"pi_{uuid.uuid4().hex}"
            token = store.issue(
                kernel_id=kernel_id,
                conversation_id=conversation_id,
                model_key=model_key,
                ttl_s=_RUN_TOKEN_TTL_S,
                budget_tokens=_RUN_TOKEN_BUDGET,
            )
            self._span("kernel_start", conversation_id, kernel_id=kernel_id)

            proc = PiProcess(
                node_bin=self._node_bin,
                entry=self._spawn_entry(),
                env=dict(os.environ),
                cwd=self._spawn_cwd(),
            )
            session = _PiSession(kernel_id=kernel_id, token=token, proc=proc)
            self._sessions[conversation_id] = session

            await proc.start(self._build_init(conversation_id, kernel_id, token))
            session.drain_task = asyncio.ensure_future(self._drain(conversation_id, session))

            text = prompt_text if prompt_text is not None else await self._latest_user_text(
                conversation_id
            )
            if text:
                await proc.prompt(text)
        except Exception as exc:  # noqa: BLE001 — surface as an event, never crash the caller
            _LOG.warning("Pi kernel bootstrap failed for %s: %s", conversation_id, exc)
            await self._fail_session(conversation_id, exc)

    async def _drain(self, conversation_id: str, session: _PiSession) -> None:
        """Drain the sidecar's outbound frames: capture each (for the I3 artifact),
        emit a `pi_event` span, map it to Disco events (I1), and append those to the
        store the UI subscribes to. An `agent_end` frame drives the finish path."""
        concluded = False
        proc = session.proc
        assert proc is not None
        try:
            async for frame in proc.events():
                session.frames.append(frame)
                self._span("pi_event", conversation_id, frame_type=frame.get("type"))
                for ev in map_pi_event(frame, conversation_id=conversation_id):
                    await self._rt._store.append(conversation_id, ev)
                if self._frame_kind(frame) == "agent_end":
                    concluded = True
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a drain failure must not crash the loop
            _LOG.exception("Pi kernel drain failed for %s", conversation_id)
        finally:
            if concluded:
                await self._conclude(conversation_id, session)

    @staticmethod
    def _frame_kind(frame: Mapping[str, Any]) -> str | None:
        """The frame's effective kind: the inner `agent_event` envelope's `kind`,
        else the raw protocol `type` (mirrors the I1 mapper's unwrap)."""
        if frame.get("type") == "agent_event":
            inner = frame.get("event")
            if isinstance(inner, Mapping):
                k = inner.get("kind")
                return k if isinstance(k, str) else None
        t = frame.get("type")
        return t if isinstance(t, str) else None

    async def _latest_user_text(self, conversation_id: str) -> str:
        """The most recent USER message text in the log (the prompt to (re)issue when
        `start`/resume carries no explicit text). Empty when there is none."""
        with contextlib.suppress(Exception):
            events = await self._rt._store.get_events(conversation_id)
            for ev in reversed(events):
                if isinstance(ev, MessageEvent) and ev.source == EventSource.USER:
                    return ev.message.content or ""
        return ""

    async def _conclude(self, conversation_id: str, session: _PiSession) -> None:
        """The Pi loop returned (`agent_end`): emit the finish/verification spans (I2),
        write the sanitized artifact (I3), record FINISHED, revoke the run token, and
        tear the sidecar down. Idempotent via `session.finished`."""
        if session.finished:
            return
        session.finished = True
        self._span("finish_request", conversation_id, kernel_id=session.kernel_id)
        # NOTE(seam): the Definition-of-Done / verifier gate lives in the Disco
        # `AgentLoop` engine and is NOT reproduced for the Pi loop in this batch —
        # running Pi's `finish` tool through the bridge into the loop DoD gate is a
        # larger integration (a later PR). The span is emitted with a `deferred`
        # status so the trace shape (§7.11) is complete and the gap is explicit.
        self._span(
            "verification_result",
            conversation_id,
            status="deferred",
            note="DoD/verifier gate not yet wired for PiKernel",
        )
        self._write_artifact(conversation_id, session)
        with contextlib.suppress(Exception):
            await self._rt._store.append(
                conversation_id,
                StatusEvent(source=EventSource.SYSTEM, status=ConversationStatus.FINISHED),
            )
        self._rt._revoke_pi_tokens(conversation_id)
        self._resolve_pending(conversation_id, "run finished")
        proc = session.proc
        self._sessions.pop(conversation_id, None)
        if proc is not None:
            with contextlib.suppress(Exception):
                await proc.aclose()

    async def _fail_session(self, conversation_id: str, exc: Exception) -> None:
        """Tear down a session that failed to bootstrap: append an error event, revoke
        the token, kill any spawned process."""
        from disco.core import AgentErrorEvent

        with contextlib.suppress(Exception):
            await self._rt._store.append(
                conversation_id,
                AgentErrorEvent(error=f"Pi build kernel failed to start: {exc}"),
            )
        await self._teardown(conversation_id, reason="bootstrap failed")

    def _write_artifact(self, conversation_id: str, session: _PiSession) -> None:
        """Persist the sanitized session frame stream (I3); best-effort + secret-free."""
        base = self._artifact_base()
        if base is None:
            return
        write_pi_session_artifact(
            base_dir=base,
            conversation_id=conversation_id,
            kernel_id=session.kernel_id,
            frames=list(session.frames),
            redact_secrets=[session.token],
        )

    def _artifact_base(self) -> str | None:
        """The evidence-folder base for session artifacts. Test override wins; else a
        DB-sidecar dir (`{DISCO_DB}.pi_sessions`), or None for an in-memory/unset DB
        (nowhere durable to write — skip)."""
        if self._artifact_base_override is not None:
            return self._artifact_base_override
        db = disco_env("DB", "") or ""
        if not db or db == ":memory:":
            return None
        return f"{db}.pi_sessions"

    # -- plan gate (E1/E2/E3) -------------------------------------------------

    def writes_blocked(self, conversation_id: str) -> bool:
        """True when a managed Pi run has NOT had a plan approved — the bridge refuses
        write tools (file_write/replace/insert/shell_exec) until then (E1/E3: submit a
        plan before writing; a rejected plan re-blocks until re-approval). A NON-managed
        conversation (the raw D2 bridge, no Pi session) is never blocked here."""
        return (
            conversation_id in self._managed
            and not self._plan_approved.get(conversation_id, False)
        )

    async def handle_gate_tool(
        self,
        conversation_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        call_id: str,
    ) -> ToolResult:
        """Dispatch a bridged gate tool (submit_plan / ask_user / clarify) to its
        long-poll handler. Each appends the existing Disco gate event, parks at the
        gate, and AWAITS its future — Pi's tool call naturally pauses until a control
        op resolves it, then the verdict/answer returns as the tool result."""
        self._managed.add(conversation_id)
        if tool_name == "submit_plan":
            return await self._handle_submit_plan(conversation_id, arguments, call_id)
        if tool_name == "ask_user":
            return await self._handle_ask_user(conversation_id, arguments, call_id)
        if tool_name == "clarify":
            return await self._handle_clarify(conversation_id, arguments, call_id)
        return ToolResult(
            call_id=call_id,
            tool_name=tool_name,
            success=False,
            content=f"unknown gate tool {tool_name!r}",
            structured={"kind": "unknown_gate_tool", "tool_name": tool_name},
            error=f"unknown gate tool {tool_name!r}",
        )

    async def _handle_submit_plan(
        self, conversation_id: str, arguments: Mapping[str, Any], call_id: str
    ) -> ToolResult:
        """E1: append a `PlanEvent`, set `AWAITING_PLAN_APPROVAL`, and long-poll the
        approval gate. Returns the verdict text (approve/reject) as the tool result so
        Pi's submit_plan resolves and the session continues in the SAME process."""
        plan = self._build_plan_event(arguments)
        self._plan_approved[conversation_id] = False
        await self._rt._store.append(conversation_id, plan)
        await self._rt._store.append(
            conversation_id, StatusEvent(status=ConversationStatus.AWAITING_PLAN_APPROVAL)
        )
        self._span(
            "pause_for_plan", conversation_id, plan_id=plan.id, steps=len(plan.steps)
        )
        verdict = await self._park(self._plan_gates, conversation_id)
        return ToolResult(
            call_id=call_id,
            tool_name="submit_plan",
            success=True,
            content=verdict,
            structured={"kind": "plan_verdict"},
        )

    @staticmethod
    def _build_plan_event(arguments: Mapping[str, Any]) -> PlanEvent:
        """Coerce the model's `{steps, summary?, context?}` into a `PlanEvent`. Steps
        may be strings or `{title, detail?}` dicts; tolerant of either shape."""
        raw_steps = arguments.get("steps")
        steps: list[PlanStep] = []
        if isinstance(raw_steps, list):
            for item in raw_steps:
                if isinstance(item, Mapping):
                    title = str(item.get("title") or item.get("step") or "").strip()
                    detail = item.get("detail")
                    detail = str(detail) if detail else None
                else:
                    title = str(item).strip()
                    detail = None
                if title:
                    steps.append(PlanStep(title=title, detail=detail))
        if not steps:
            steps = [PlanStep(title="(no steps provided)")]
        summary = str(arguments.get("summary") or "Proposed build plan").strip()
        context = str(arguments.get("context") or "")
        return PlanEvent(summary=summary, steps=steps, context=context)

    async def _handle_ask_user(
        self, conversation_id: str, arguments: Mapping[str, Any], call_id: str
    ) -> ToolResult:
        """E3 (ask): append the agent's free-form question as an assistant message, set
        `AWAITING_USER_QUESTION`, and long-poll until the user's next turn answers."""
        question = str(
            arguments.get("question") or arguments.get("prompt") or "The agent needs input."
        )
        await self._rt._store.append(
            conversation_id,
            MessageEvent(
                source=EventSource.AGENT,
                message=LLMMessage(role="assistant", content=question),
                meta={"pi_gate": "ask_user"},
            ),
        )
        await self._rt._store.append(
            conversation_id, StatusEvent(status=ConversationStatus.AWAITING_USER_QUESTION)
        )
        self._span("pause_for_plan", conversation_id, gate="ask_user")
        answer = await self._park(self._ask_gates, conversation_id)
        return ToolResult(
            call_id=call_id,
            tool_name="ask_user",
            success=True,
            content=answer,
            structured={"kind": "ask_answer"},
        )

    async def _handle_clarify(
        self, conversation_id: str, arguments: Mapping[str, Any], call_id: str
    ) -> ToolResult:
        """E3 (clarify): append a `ClarifyEvent` of typed questions, set
        `AWAITING_USER_QUESTION`, and long-poll until the user's next turn answers."""
        raw_items = arguments.get("items") or arguments.get("questions") or []
        items: list[ClarifyQuestionItem] = []
        if isinstance(raw_items, list):
            for i, it in enumerate(raw_items):
                if isinstance(it, Mapping):
                    q = str(it.get("question") or it.get("text") or "").strip()
                    if not q:
                        continue
                    qtype = it.get("type")
                    qtype = qtype if qtype in ("short_text", "long_text", "choice") else "short_text"
                    opts = it.get("options")
                    options = [str(o) for o in opts] if isinstance(opts, list) else []
                    items.append(
                        ClarifyQuestionItem(
                            id=str(it.get("id") or f"q{i}"),
                            question=q,
                            type=cast("Any", qtype),
                            options=options,
                        )
                    )
                else:
                    items.append(ClarifyQuestionItem(id=f"q{i}", question=str(it)))
        if not items:
            items = [ClarifyQuestionItem(id="q0", question="Please clarify your request.")]
        clarify = ClarifyEvent(
            question=str(arguments.get("question") or "Please clarify"), items=items
        )
        await self._rt._store.append(conversation_id, clarify)
        await self._rt._store.append(
            conversation_id, StatusEvent(status=ConversationStatus.AWAITING_USER_QUESTION)
        )
        self._span("pause_for_plan", conversation_id, gate="clarify")
        answer = await self._park(self._ask_gates, conversation_id)
        return ToolResult(
            call_id=call_id,
            tool_name="clarify",
            success=True,
            content=answer,
            structured={"kind": "clarify_answer"},
        )

    async def _park(
        self, registry: dict[str, asyncio.Future[str]], conversation_id: str
    ) -> str:
        """Register + await a gate future for `conversation_id`, cleaning the registry
        on resolution. A second gate for the same conversation supersedes a stale one
        (resolve it so its held request returns)."""
        loop = asyncio.get_running_loop()
        existing = registry.get(conversation_id)
        if existing is not None and not existing.done():
            existing.set_result("superseded by a newer gate")
        fut: asyncio.Future[str] = loop.create_future()
        registry[conversation_id] = fut
        try:
            return await fut
        finally:
            if registry.get(conversation_id) is fut:
                registry.pop(conversation_id, None)

    def _resolve_pending(self, conversation_id: str, message: str) -> None:
        """Resolve any held plan/ask gate futures (on teardown/finish) so a long-poll
        bridge request never hangs after the run ends."""
        for registry in (self._plan_gates, self._ask_gates):
            fut = registry.get(conversation_id)
            if fut is not None and not fut.done():
                fut.set_result(message)

    async def approve_plan(self, conversation_id: str) -> None:
        """E2: approve the pending plan — mark writes unblocked, transition out of
        `AWAITING_PLAN_APPROVAL`, and resolve the held submit_plan call so Pi resumes
        the SAME session. No recursion (see module docstring): the status transition is
        appended directly, NOT via `_control.approve_plan` (which would kick a Disco
        loop)."""
        self._plan_approved[conversation_id] = True
        await self._rt._store.append(
            conversation_id, StatusEvent(status=ConversationStatus.RUNNING)
        )
        self._span("resume_after_approval", conversation_id)
        fut = self._plan_gates.get(conversation_id)
        if fut is not None and not fut.done():
            fut.set_result(
                "Plan approved — proceed with execution. Write tools are now permitted."
            )

    async def reject_plan(self, conversation_id: str, reason: str = "") -> None:
        """E3: reject the pending plan — writes stay blocked, the held submit_plan call
        returns an instruction to REPLAN (not write), and Pi must submit a revised plan
        and have it re-approved before any write tool is permitted again."""
        self._plan_approved[conversation_id] = False
        await self._rt._store.append(
            conversation_id, StatusEvent(status=ConversationStatus.RUNNING)
        )
        detail = reason.strip() or "revise the plan"
        fut = self._plan_gates.get(conversation_id)
        if fut is not None and not fut.done():
            fut.set_result(
                f"Plan rejected: {detail}. Submit a REVISED plan via submit_plan and wait "
                "for approval — do NOT write files yet."
            )

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        """(Re-)enter plan mode with the user's instruction. If a plan gate is held,
        resolve it as a replan request; otherwise Pi drives its own planning, so the
        instruction lands as a follow-up turn."""
        self._plan_approved[conversation_id] = False
        fut = self._plan_gates.get(conversation_id)
        if fut is not None and not fut.done():
            instr = text.strip() or "revise the plan"
            fut.set_result(
                f"Replan requested: {instr}. Submit a revised plan via submit_plan; do NOT "
                "write files yet."
            )
            return
        # No pending plan gate — forward the instruction to the live session.
        session = self._sessions.get(conversation_id)
        if text.strip() and session is not None and session.proc is not None:
            with contextlib.suppress(Exception):
                await session.proc.followup(text)

    # -- action gate (Pi has no Disco BlastRadiusConfirm) ---------------------

    async def confirm(self, conversation_id: str) -> None:
        """Pi does not use Disco's per-action BlastRadiusConfirm gate (its plan gate is
        the up-front approval). A confirm is a no-op (documented); never recurse into
        `_control`."""
        return None

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        """No per-action gate for Pi — a no-op (see `confirm`)."""
        return None

    async def pick_alternative(self, conversation_id: str, option_id: str) -> None:
        """Resolve a pending ask gate with the picked option id, if any (the
        alternatives path is otherwise out of scope for the Pi plan gate)."""
        fut = self._ask_gates.get(conversation_id)
        if fut is not None and not fut.done():
            await self._rt._store.append(
                conversation_id, StatusEvent(status=ConversationStatus.RUNNING)
            )
            fut.set_result(option_id)

    # -- stop / resume --------------------------------------------------------

    async def pause(self, conversation_id: str) -> None:
        """Cooperatively abort the in-flight Pi turn WITHOUT killing the process (the
        sidecar stays resumable). A no-op when no live session exists."""
        session = self._sessions.get(conversation_id)
        if session is not None and session.proc is not None and not session.finished:
            with contextlib.suppress(Exception):
                await session.proc.cancel()

    async def cancel(self, conversation_id: str) -> None:
        """Cooperative stop: write the artifact, revoke the run token, and tear the
        sidecar TREE down. Idempotent."""
        await self._teardown(conversation_id, reason="cancelled")

    async def resume(self, conversation_id: str) -> None:
        """Continue a stopped/incomplete run. A live session gets a 'continue'
        follow-up; otherwise a fresh session is bootstrapped from the latest user
        turn."""
        session = self._sessions.get(conversation_id)
        if session is not None and session.proc is not None and not session.finished:
            with contextlib.suppress(Exception):
                await session.proc.prompt("Continue the build from where you left off.")
            return
        await self._bootstrap(conversation_id, prompt_text=None)

    async def kill(self, conversation_id: str) -> None:
        """The hard kill: tear down the process tree + revoke caps (same teardown as
        cancel; the run token revoke IS the cap revoke for the gateway). Idempotent."""
        await self._teardown(conversation_id, reason="killed")

    async def _teardown(self, conversation_id: str, *, reason: str) -> None:
        """Tear down a conversation's Pi session: write the artifact, revoke the run
        token, cancel the drain, kill the process tree, and resolve any held gates.
        Idempotent + best-effort (a teardown failure must never propagate)."""
        session = self._sessions.pop(conversation_id, None)
        # Revoke unconditionally (None-safe + idempotent) — even if no in-memory
        # session remains, the token must die when the run ends.
        self._rt._revoke_pi_tokens(conversation_id)
        self._managed.discard(conversation_id)
        if session is None:
            self._resolve_pending(conversation_id, f"run {reason}")
            return
        session.finished = True
        self._write_artifact(conversation_id, session)
        drain = session.drain_task
        if drain is not None and not drain.done():
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain
        if session.proc is not None:
            with contextlib.suppress(Exception):
                await session.proc.aclose()
        self._resolve_pending(conversation_id, f"run {reason}")

    # -- events / state -------------------------------------------------------

    async def subscribe(
        self, conversation_id: str, *, after_seq: int | None = None
    ) -> AsyncIterator[KernelEvent]:
        """The conversation's event stream (history-then-live), straight from the store
        — IDENTICAL to `DiscoKernel`, so the WS/UI work unchanged."""
        return await self._rt._store.subscribe(conversation_id, after_seq=after_seq)

    async def get_state(self, conversation_id: str) -> ConversationState:
        """The reconstructed conversation state from the store (status + pending gate
        ids) — IDENTICAL to `DiscoKernel`."""
        return await self._rt._store.get_state(conversation_id)
