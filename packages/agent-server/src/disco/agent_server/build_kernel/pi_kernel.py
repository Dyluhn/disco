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
    ActionEvent,
    AgentErrorEvent,
    ClarifyEvent,
    ClarifyQuestionItem,
    ConversationState,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.appkit import BuildBrief
from disco.core.context import ArtifactMemoryStore, ContextLedger
from disco.core.env import disco_env
from disco.core.inspect import inspect_enabled
from disco.core.llm import ModelRole
from disco.core.loop.context_builder import build_context_pack, render_context_pack
from disco.core.obs import log_event
from disco.core.workflows import (
    PromptPack,
    PromptPackRegistry,
    assemble_workflow_prompt,
    render_messages_as_text,
)

from ..build_messages import _build_brief_message, _context_message, _user_message
from .base import KernelEvent
from .pi_event_mapper import map_pi_event
from .pi_process import KernelInitConfig, PiProcess, default_pi_kernel_entry
from .pi_session_artifact import write_pi_session_artifact
from .pi_tool_bridge import BridgeDecision, BridgeOutcome, PiToolBridge

if TYPE_CHECKING:
    from ..runtime import ConversationRuntime

_LOG = logging.getLogger("disco.pi_kernel")

# The loop-handled VIRTUAL / meta tools the Pi tool bridge must NOT route to the
# executor — finish/submit_plan/ask/clarify/notify/remember/serve/delegate are
# intercepted by the loop's MetaToolHandlers + plan gate (turn_control.py:905,
# engine.py:1315), not run as executor tools. This is the AUTHORITATIVE set the
# bridge short-circuits to ``BridgeDecision.VIRTUAL`` so the kernel dispatches them
# to its real plan/ask/clarify gate + finish handlers (NOT a bare executor call).
#
# NOTE (P1/#4): `plan_step` and `update_plan_progress` are deliberately ABSENT —
# they are REAL executor tools (plan-progress bookkeeping the loop runs through the
# executor like any other tool), NOT loop-intercepted virtuals. Listing them here
# would short-circuit them to VIRTUAL and silently skip the executor (diverging
# from Disco, where they execute). They route through the bridge as real tools.
_PI_VIRTUAL_TOOLS: frozenset[str] = frozenset(
    {
        "finish",
        "submit_plan",
        "propose_plan_update",
        "ask_user",
        "clarify",
        "notify_user",
        "remember",
        "serve",
        "delegate_explore",
    }
)

# The VIRTUAL tools whose Python-side long-poll gate handlers exist (submit_plan /
# ask_user / clarify). Normally intercepted in ``routes/pi_tools.py`` BEFORE the
# bridge; listed here so a defensive VIRTUAL dispatch routes them to the real gate
# handler instead of a no-op acknowledgement.
_GATE_VIRTUALS: frozenset[str] = frozenset({"submit_plan", "ask_user", "clarify"})

# The model alias Pi sees over the gateway. The gateway PINS the real model by the
# run token, ignoring this value — it is purely cosmetic on the wire (campaign §4.2).
_GATEWAY_MODEL_ALIAS = "disco-selected"

_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_CONTEXT_PACK_FLAG = "CONTEXT_PACK"
_PI_BOOTSTRAP_SYSTEM_PREFIX = ""
_PI_RESUME_PROMPT = "Continue the build from where you left off."

# Run-token lifetime + budget. Short-lived (the store also caps TTL to its own
# ceiling) and capped so a runaway sidecar cannot spend unbounded tokens.
_RUN_TOKEN_TTL_S = 2 * 3600.0
_RUN_TOKEN_BUDGET = 4_000_000


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
        # The per-action risk-confirmation gate future a bridged tool call long-polls
        # when the risk gate HALTed it pending (BridgeDecision.CONFIRM_REQUIRED);
        # resolved by confirm() (execute the held action) / reject() (deny it). The
        # proposed ActionEvent awaiting confirmation is parked alongside it so the
        # continuation re-runs THAT action through the bridge's safe execute/observe.
        self._confirm_gates: dict[str, asyncio.Future[ToolResult]] = {}
        self._pending_actions: dict[str, ActionEvent] = {}

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

    def _context_pack_enabled(self) -> bool:
        """Flag gate for the CXT/WPP live prompt path. Default OFF."""
        return str(disco_env(_CONTEXT_PACK_FLAG) or "").strip().lower() in _TRUTHY

    def _prompt_pack_for(self, conversation_id: str) -> PromptPack | None:
        """Resolve this run's WorkflowPromptPack from the runtime's contract seam.

        The seam is intentionally optional so tests/lightweight hosts can omit it; in
        that case Pi still gets the ContextPack and live user turn without crashing.
        """
        resolver = getattr(self._rt, "_build_contract_for", None)
        if not callable(resolver):
            return None
        try:
            contract = resolver(conversation_id, classify_default=True)
        except TypeError:
            try:
                contract = resolver(conversation_id)
            except Exception:  # noqa: BLE001 — prompt-pack lookup is additive
                _LOG.warning(
                    "Pi kernel prompt-pack contract lookup failed for %s",
                    conversation_id,
                    exc_info=True,
                )
                return None
        except Exception:  # noqa: BLE001 — prompt-pack lookup is additive
            _LOG.warning(
                "Pi kernel prompt-pack contract lookup failed for %s",
                conversation_id,
                exc_info=True,
            )
            return None
        pack_id = getattr(contract, "prompt_pack", None)
        if not isinstance(pack_id, str) or not pack_id.strip():
            return None
        try:
            return PromptPackRegistry().get(pack_id)
        except Exception:  # noqa: BLE001 — a bad pack must not strand Pi bootstrap
            _LOG.warning(
                "Pi kernel prompt-pack load failed for %s: %s",
                conversation_id,
                pack_id,
                exc_info=True,
            )
            return None

    def _existing_sandbox(self, conversation_id: str) -> Any | None:
        """Return an already-live sandbox, if the runtime has one.

        Do not create a loop/executor just to read context memory; Pi bootstrap is the
        sidecar's startup path and context-memory access is best-effort.
        """
        executors = getattr(self._rt, "_executors", None)
        executor = executors.get(conversation_id) if isinstance(executors, Mapping) else None
        if executor is None:
            return None
        return getattr(executor, "sandbox", None) or getattr(executor, "_sandbox", None)

    async def _context_memory_inputs(
        self, conversation_id: str
    ) -> tuple[ContextLedger | None, str | None]:
        sbx = self._existing_sandbox(conversation_id)
        if sbx is None:
            return None, None
        try:
            store = ArtifactMemoryStore(sbx)
            workspace_root = getattr(sbx, "workspace_path", None)
            reconstructed = await store.reconstruct(
                conversation_id,
                str(workspace_root) if workspace_root else None,
            )
            return reconstructed.ledger, await store.read_todo()
        except Exception:  # noqa: BLE001 — durable context is additive, not required
            _LOG.warning(
                "Pi kernel context memory read failed for %s",
                conversation_id,
                exc_info=True,
            )
            return None, None

    async def _context_pack_block(self, conversation_id: str) -> str:
        try:
            events = list(await self._rt._store.get_events(conversation_id))
        except Exception:  # noqa: BLE001 — emit an empty pack rather than fail bootstrap
            _LOG.warning(
                "Pi kernel context-pack event read failed for %s",
                conversation_id,
                exc_info=True,
            )
            events = []
        base_ledger, todo_text = await self._context_memory_inputs(conversation_id)
        return render_context_pack(
            build_context_pack(events, base_ledger=base_ledger, todo_text=todo_text)
        )

    async def _render_prompt_text(self, conversation_id: str, user_text: str) -> str:
        if not self._context_pack_enabled():
            return user_text
        msgs = assemble_workflow_prompt(
            system_prefix=_PI_BOOTSTRAP_SYSTEM_PREFIX,
            prompt_pack=self._prompt_pack_for(conversation_id),
            context_pack_block=await self._context_pack_block(conversation_id),
            recent_turns=[LLMMessage(role="user", content=user_text)],
        )
        return render_messages_as_text(msgs)

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
        build_brief: BuildBrief | None = None,
        steer: bool = False,
    ) -> MessageEvent:
        """Append a user turn and start/continue the Pi run. If an ask/clarify gate is
        pending, this turn is the user's ANSWER → resolve the held bridge call and
        return (Pi continues its in-flight tool). Otherwise it (re)starts the session
        (fresh) or forwards as a follow-up (live). Returns the stored USER message."""
        self._managed.add(conversation_id)
        pending = []
        if context:
            pending.append(_context_message(context))
        if build_brief is not None:
            pending.append(_build_brief_message(build_brief))
        pending.append(_user_message(text, steer=steer))
        stored_events = await self._rt._store.append_many(conversation_id, pending)
        stored = stored_events[-1]

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
                await proc.prompt(await self._render_prompt_text(conversation_id, text))
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

    async def _conclude(
        self, conversation_id: str, session: _PiSession, *, dod_passed: bool | None = None
    ) -> None:
        """The Pi loop returned (`agent_end`) OR finish passed its gate: emit the
        finish/verification spans (I2), write the sanitized artifact (I3), record
        FINISHED, revoke the run token, and tear the sidecar down. Idempotent via
        `session.finished`.

        P1 FINISH GATE: the verification span now carries the REAL Definition-of-Done
        verdict (FinishGate.finish_dod_gate_passed) instead of a hardcoded `deferred`.
        When the finish TOOL already gated (``dod_passed`` set), reuse that verdict to
        avoid a double evaluation; on the bare `agent_end` path (``dod_passed`` None —
        Pi ended its own loop without our finish tool), evaluate the gate here so the
        trace records whether the work actually met its acceptance criteria."""
        if session.finished:
            return
        session.finished = True
        self._span("finish_request", conversation_id, kernel_id=session.kernel_id)
        if dod_passed is None:
            dod_passed = await self._finish_dod_gate_passed(conversation_id)
        self._span(
            "verification_result",
            conversation_id,
            status="passed" if dod_passed else "unmet",
            note="Definition-of-Done gate (FinishGate.finish_dod_gate_passed)",
        )
        self._write_artifact(conversation_id, session)
        with contextlib.suppress(Exception):
            await self._rt._store.append(
                conversation_id,
                StatusEvent(source=EventSource.SYSTEM, status=ConversationStatus.FINISHED),
            )
        await self._rt._maybe_shadow_fold_finished_manifest(conversation_id)
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

    # -- tool bridge (EPIC D — the SAFE in-process safety wrapper) ------------

    def _tool_bridge(self, conversation_id: str) -> PiToolBridge:
        """Build the safety-wrapper bridge over THIS conversation's live loop.

        The loop is resolved through the runtime's inner resolver (``_loop_for``,
        exactly as ``DiscoKernel.pick_alternative`` does) — never through a public
        runtime method (those route through the active kernel → recursion). The
        bridge composes the loop's executor/analyzer/policy/planning-allowlist and
        drives a Pi tool call through the SAME ordered gate stack
        ``AgentLoop._run_drive`` applies (reconcile → halted → replan → planning →
        virtual → hard-deny → risk-confirm → append → execute/observe), NOT a bare
        ``executor.execute()``."""
        loop = self._rt._loop_for(conversation_id)
        return PiToolBridge.from_loop(loop, virtual_tool_names=_PI_VIRTUAL_TOOLS)

    async def route_pi_tool(
        self, conversation_id: str, tool_call: ToolCall
    ) -> ToolResult:
        """Route ONE Pi-emitted tool call through Disco's FULL safety wrapper and map
        the structured outcome back to the ``ToolResult`` Pi's tool expects.

        THE SAFETY CORE of EPIC D. Replaces the unsafe ``runtime.execute_pi_tool``
        path (a bare ``executor.execute()``): a Pi tool call is now gated EXACTLY as
        a Disco-originated one. The ``BridgeDecision`` is wired to the landed
        handlers — VIRTUAL → the real plan/ask/clarify + finish/DoD handlers;
        CONFIRM_REQUIRED → a REAL confirm/reject continuation; HALTED/REFUSED →
        returned WITHOUT executing (no observation side effects)."""
        self._managed.add(conversation_id)
        outcome = await self._tool_bridge(conversation_id).route(
            tool_call, thought=f"[pi] {tool_call.tool_name}"
        )
        return await self._outcome_to_result(conversation_id, tool_call, outcome)

    @staticmethod
    def reconcile_dangling_tool_call(events: list[Event]) -> Event | None:
        """Neutralize a tool call interrupted before its observation (no auto-replay).
        Delegates to the bridge's documented dangling-observation decision — see
        :meth:`PiToolBridge.reconcile_dangling_action`."""
        return PiToolBridge.reconcile_dangling_action(events)

    async def reconcile_dangling_on_resume(self, conversation_id: str) -> Event | None:
        """P2/#6 — WIRE the dangling reconciliation on Pi resume/start.

        Call BEFORE accepting any new Pi tool call after a resume (process restart /
        kill recovery): detect a tool action interrupted between its ActionEvent and
        its observation and, if found, append a synthetic interrupted-observation
        AgentErrorEvent to RESTORE the pairing — WITHOUT re-executing (a partially-
        applied side effect must not be blindly repeated). IDEMPOTENT (a second call
        is a no-op). The bridge ALSO enforces this guard at the top of every
        ``route`` (defense in depth)."""
        loop = self._rt._loop_for(conversation_id)
        events = await loop._events()
        recovered = PiToolBridge.reconcile_dangling_action(events)
        if recovered is not None:
            await loop._emit(recovered)
        return recovered

    async def _outcome_to_result(
        self,
        conversation_id: str,
        tool_call: ToolCall,
        outcome: BridgeOutcome,
    ) -> ToolResult:
        """Map a ``BridgeOutcome`` to the ``ToolResult`` returned over the HTTP bridge.

        EXECUTED → the executor's real result (paired observation already in the log).
        FAILED/REFUSED → a structured failing result carrying the gate/executor error
        (Pi reasons about it and adapts — recoverable, like Disco surfacing a failure).
        HALTED → NOT executed; tell Pi to wait for the control surface.
        CONFIRM_REQUIRED → long-poll the real confirm/reject continuation.
        VIRTUAL → dispatch to the loop-handled finish/gate handlers."""
        name = tool_call.tool_name
        cid = tool_call.call_id
        if outcome.decision is BridgeDecision.EXECUTED:
            assert outcome.observation is not None
            return outcome.observation.tool_result
        if outcome.decision is BridgeDecision.VIRTUAL:
            return await self._handle_virtual(conversation_id, tool_call)
        if outcome.decision is BridgeDecision.CONFIRM_REQUIRED:
            return await self._await_confirmation(conversation_id, tool_call, outcome)
        if outcome.decision is BridgeDecision.HALTED:
            return ToolResult(
                call_id=cid,
                tool_name=name,
                success=False,
                content=(
                    "the conversation is not in a drivable state (awaiting "
                    "confirmation / plan approval / a user answer, paused, or "
                    "terminal); the call was NOT executed — wait for the control "
                    "surface rather than retrying"
                ),
                structured={
                    "kind": "halted",
                    "tool_name": name,
                    "pending_action_id": outcome.pending_action_id,
                },
                error="conversation not drivable",
            )
        # FAILED / REFUSED — surface the gate/executor error as a structured failure.
        err_text = outcome.error.error if outcome.error is not None else "tool failed"
        kind = "refused_by_gate" if outcome.decision is BridgeDecision.REFUSED else "tool_failed"
        return ToolResult(
            call_id=cid,
            tool_name=name,
            success=False,
            content=err_text,
            structured={"kind": kind, "tool_name": name},
            error=err_text,
        )

    async def _handle_virtual(
        self, conversation_id: str, tool_call: ToolCall
    ) -> ToolResult:
        """Dispatch a loop-handled VIRTUAL tool the bridge short-circuited (it was NOT
        executed). ``finish`` runs the REAL Definition-of-Done gate (P1); the gate
        tools route to their long-poll handlers (defense in depth — they are normally
        intercepted before the bridge in ``routes/pi_tools.py``)."""
        name = tool_call.tool_name
        if name == "finish":
            return await self._handle_finish(conversation_id, tool_call)
        if name in _GATE_VIRTUALS:
            return await self.handle_gate_tool(
                conversation_id, name, dict(tool_call.arguments), tool_call.call_id
            )
        # notify_user / remember / serve / delegate_explore / propose_plan_update are
        # NOT in the Pi build-kernel allowlist (they never reach here through the real
        # route — the allowlist refuses them first). Acknowledge without executing so a
        # defensive call is a safe no-op rather than a bare executor dispatch.
        return ToolResult(
            call_id=tool_call.call_id,
            tool_name=name,
            success=True,
            content=f"{name!r} is a loop-handled meta tool; acknowledged (no executor action).",
            structured={"kind": "virtual_ack", "tool_name": name},
        )

    async def _handle_finish(
        self, conversation_id: str, tool_call: ToolCall
    ) -> ToolResult:
        """P1 FINISH GATE — route Pi's ``finish`` through the REAL Definition-of-Done
        gate before marking the run finished (mirror the loop's finish handling,
        engine.py:1372 → FinishGate.finish_dod_gate_passed).

        The landed ``_conclude`` previously recorded FINISHED while explicitly noting
        the DoD/verifier gate was "not wired". Now: evaluate the DoD; if UNMET, the
        gate emits the specific unmet predicates and we return a failing result so Pi
        keeps working (NOT finished); if MET (or no spec), conclude the run."""
        passed = await self._finish_dod_gate_passed(conversation_id)
        if not passed:
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name="finish",
                success=False,
                content=(
                    "finish is BLOCKED: the external Definition-of-Done is not yet "
                    "satisfied. The unmet acceptance criteria were just recorded — "
                    "address them and call finish again. The run was NOT concluded."
                ),
                structured={"kind": "dod_unmet"},
                error="definition of done not met",
            )
        session = self._sessions.get(conversation_id)
        if session is not None:
            await self._conclude(conversation_id, session, dod_passed=True)
        else:
            # No live session (raw/test path) — still terminalize + revoke caps.
            with contextlib.suppress(Exception):
                await self._rt._store.append(
                    conversation_id,
                    StatusEvent(source=EventSource.SYSTEM, status=ConversationStatus.FINISHED),
                )
            await self._rt._maybe_shadow_fold_finished_manifest(conversation_id)
            self._rt._revoke_pi_tokens(conversation_id)
        return ToolResult(
            call_id=tool_call.call_id,
            tool_name="finish",
            success=True,
            content="Build finished — the Definition-of-Done gate passed and the run is complete.",
            structured={"kind": "finished"},
        )

    async def _finish_dod_gate_passed(self, conversation_id: str) -> bool:
        """Run the loop's REAL DoD gate (FinishGate.finish_dod_gate_passed via
        ``AgentLoop._finish_dod_gate_passed``). Returns True iff the finish should be
        allowed (no DoD spec → True, the legacy byte-identical path). Best-effort: a
        gate that cannot evaluate (no workspace / build error) does not trap the run —
        it logs and allows finish, exactly as the loop's own gate does."""
        try:
            loop = self._rt._loop_for(conversation_id)
            return await loop._finish_dod_gate_passed()
        except Exception:  # noqa: BLE001 — a gate error must never wedge the finish
            _LOG.warning(
                "Pi finish: DoD gate could not evaluate for %s; allowing finish "
                "(refusing without evidence would be a silent fail)",
                conversation_id,
                exc_info=True,
            )
            return True

    # -- per-action confirm/reject continuation (P0 graft item 4) -------------

    async def _await_confirmation(
        self,
        conversation_id: str,
        tool_call: ToolCall,
        outcome: BridgeOutcome,
    ) -> ToolResult:
        """The risk gate HALTed this call pending confirmation. Park the held bridge
        HTTP request on a confirm gate future (the proposed ActionEvent +
        WAITING_FOR_CONFIRMATION status are already in the log) and long-poll until
        ``confirm`` (execute it) / ``reject`` (deny it) resolves it — a REAL
        continuation, replacing the documented no-op."""
        assert outcome.action is not None
        self._pending_actions[conversation_id] = outcome.action
        self._span(
            "pause_for_confirmation", conversation_id, action_id=outcome.action.id
        )
        try:
            return await self._park_confirm(conversation_id, tool_call)
        finally:
            self._pending_actions.pop(conversation_id, None)

    async def _park_confirm(
        self, conversation_id: str, tool_call: ToolCall
    ) -> ToolResult:
        """Register + await the confirm gate future for ``conversation_id``. A second
        pending confirm for the same conversation supersedes a stale one."""
        loop = asyncio.get_running_loop()
        existing = self._confirm_gates.get(conversation_id)
        if existing is not None and not existing.done():
            existing.set_result(
                ToolResult(
                    call_id=tool_call.call_id,
                    tool_name=tool_call.tool_name,
                    success=False,
                    content="superseded by a newer pending action",
                    structured={"kind": "confirm_superseded"},
                    error="superseded",
                )
            )
        fut: asyncio.Future[ToolResult] = loop.create_future()
        self._confirm_gates[conversation_id] = fut
        try:
            return await fut
        finally:
            if self._confirm_gates.get(conversation_id) is fut:
                self._confirm_gates.pop(conversation_id, None)

    # -- plan gate (E1/E2/E3) -------------------------------------------------

    def writes_blocked(self, conversation_id: str) -> bool:
        """True when a managed Pi run has NOT had a plan approved.

        INFORMATIONAL ONLY since the EPIC-D graft: write-gating is now ENFORCED by the
        bridge's REAL planning gate (loop.mode == PLANNING blocks writes/shell/finish
        until ``approve_plan`` flips the loop into execution mode — engine.py:945-969),
        NOT by this bookkeeping flag. Kept as a consistent read of the plan-approval
        state (and for the UI/tests); the narrow ``_WRITE_TOOLS`` HTTP gate that USED
        it was removed (it let non-write side effects run pre-approval). A NON-managed
        conversation is never blocked here."""
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
                    qtype = (
                        qtype if qtype in ("short_text", "long_text", "choice") else "short_text"
                    )
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
        bridge request never hangs after the run ends. The confirm gate (a different
        future type) is resolved with a structured failing ToolResult."""
        for registry in (self._plan_gates, self._ask_gates):
            fut = registry.get(conversation_id)
            if fut is not None and not fut.done():
                fut.set_result(message)
        cfut = self._confirm_gates.get(conversation_id)
        if cfut is not None and not cfut.done():
            action = self._pending_actions.get(conversation_id)
            tc = action.tool_call if action is not None else None
            cfut.set_result(
                ToolResult(
                    call_id=tc.call_id if tc else "pi_confirm",
                    tool_name=tc.tool_name if tc else "unknown",
                    success=False,
                    content=f"the pending action was abandoned: {message}",
                    structured={"kind": "confirm_abandoned"},
                    error=message,
                )
            )

    async def approve_plan(self, conversation_id: str) -> None:
        """E2: approve the pending plan — flip the loop into execution mode, transition
        out of `AWAITING_PLAN_APPROVAL`, and resolve the held submit_plan call so Pi
        resumes the SAME session.

        FAIL-CLOSED (P0/a): a no-op UNLESS the conversation currently has a pending
        SUBMITTED plan — i.e. its status is `AWAITING_PLAN_APPROVAL` AND a live
        submit_plan gate future is parked. Without this guard, a spurious approve_plan
        (the ws.py route accepts it unconditionally) would mark writes approved before
        ANY plan was submitted, opening the workspace pre-plan. Writes stay blocked
        until a real PlanEvent + approval.

        The mode flip routes through the loop's REAL `approve_plan` (engine.py:1552),
        which emits the durable `plan_approved` marker and sets `loop.mode =
        execution` so the bridge's planning gate OPENS — it does NOT kick a Disco
        drive loop (no recursion); Pi continues its own session."""
        state = await self._rt._store.get_state(conversation_id)
        fut = self._plan_gates.get(conversation_id)
        has_pending_plan = (
            state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
            and fut is not None
            and not fut.done()
        )
        if not has_pending_plan:
            _LOG.info(
                "approve_plan IGNORED for %s: no pending submitted plan "
                "(status=%s, gate=%s) — fail-closed, writes stay blocked",
                conversation_id,
                state.execution_status,
                "parked" if (fut is not None and not fut.done()) else "none",
            )
            return
        # Flip the loop into execution mode via the loop's own gate (emits the durable
        # `plan_approved` marker + sets loop.mode so the bridge planning gate opens).
        with contextlib.suppress(Exception):
            await self._rt._loop_for(conversation_id).approve_plan()
        self._plan_approved[conversation_id] = True
        self._span("resume_after_approval", conversation_id)
        # fut is guaranteed live by has_pending_plan above.
        assert fut is not None
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

    # -- per-action confirm/reject gate (REAL continuation, P0 graft item 4) --

    async def confirm(self, conversation_id: str) -> None:
        """CONFIRM the pending risk-gated action — a REAL continuation (replaces the
        former documented no-op).

        When a bridged Pi tool call hits the risk gate, the bridge HALTs it pending
        (BridgeDecision.CONFIRM_REQUIRED): the proposed ActionEvent + a
        WAITING_FOR_CONFIRMATION status are in the log and the held HTTP request is
        parked on the confirm gate. ``confirm`` resumes the SAME action through the
        bridge's SAFE execute/observe core (K1 guard + one-observation pairing —
        mirror of ``AgentLoop.confirm`` engine.py:1499) and resolves the held request
        with the executor's result so Pi continues. A no-op if nothing is pending."""
        action = self._pending_actions.get(conversation_id)
        fut = self._confirm_gates.get(conversation_id)
        if action is None or fut is None or fut.done():
            return
        await self._rt._store.append(
            conversation_id, StatusEvent(status=ConversationStatus.RUNNING)
        )
        tc = action.tool_call
        try:
            outcome = await self._tool_bridge(conversation_id).resume_confirmed(action)
            if outcome.decision is BridgeDecision.EXECUTED and outcome.observation is not None:
                result = outcome.observation.tool_result
            else:
                err = outcome.error.error if outcome.error is not None else "tool failed"
                result = ToolResult(
                    call_id=tc.call_id if tc else "pi_confirm",
                    tool_name=tc.tool_name if tc else "unknown",
                    success=False,
                    content=err,
                    structured={"kind": "tool_failed"},
                    error=err,
                )
        except Exception as exc:  # noqa: BLE001 — never wedge the held request
            result = ToolResult(
                call_id=tc.call_id if tc else "pi_confirm",
                tool_name=tc.tool_name if tc else "unknown",
                success=False,
                content=f"confirmed action could not execute: {exc}",
                structured={"kind": "confirm_error"},
                error=str(exc),
            )
        if not fut.done():
            fut.set_result(result)

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        """REJECT the pending risk-gated action — a REAL continuation (replaces the
        former no-op). Pair the already-proposed action with an AgentErrorEvent (so it
        is never left dangling) WITHOUT executing the tool (mirror of
        ``AgentLoop.reject`` engine.py:1515), then resolve the held request with the
        denial so Pi adapts. A no-op if nothing is pending."""
        action = self._pending_actions.get(conversation_id)
        fut = self._confirm_gates.get(conversation_id)
        if action is None or fut is None or fut.done():
            return
        await self._rt._store.append(
            conversation_id, StatusEvent(status=ConversationStatus.RUNNING)
        )
        tc = action.tool_call
        with contextlib.suppress(Exception):
            await self._tool_bridge(conversation_id).reject_pending(action, reason)
        result = ToolResult(
            call_id=tc.call_id if tc else "pi_confirm",
            tool_name=tc.tool_name if tc else "unknown",
            success=False,
            content=f"the action was REJECTED by the user ({reason}) and was NOT executed.",
            structured={"kind": "rejected"},
            error="rejected by user",
        )
        if not fut.done():
            fut.set_result(result)

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
                await session.proc.prompt(
                    await self._render_prompt_text(conversation_id, _PI_RESUME_PROMPT)
                )
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
