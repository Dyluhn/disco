# Technical Design Contract — Agent Loop & Orchestration

**Document type:** Detailed Technical Design (Contract Spec)
**Subsystem:** Agent Loop & Orchestration — BoD §12 (the core)
**Status:** v1.1 — authoritative contract (§8 awaits async condense after Phase 1 audit)

> **v1.1 changelog.** §8 `_materialize_view` now `await`s `condenser.condense(...)` (event contract v1.2 made it async); `should_condense` remains a sync call. No other changes.
**Depends on:**
- Event & State contract (`event-state-contract.md`) — `Event` types, `ConversationState`/`reconstruct()`, `View.of()`, `event_content_eq()`, the `Condenser`/`Summarizer` protocols, `EventStore`, `ConversationStatus`, the wire frames.
- LLM Router contract (`llm-router-contract.md`) — `LLMRouter`, `CompletionRequest`/`CompletionResponse`, `CapabilityProfile`, `ToolSpec`, `ProposedToolCall`, `OverflowSignal`.
**Consumes (boundary only, not defined here):** the **Tool/Sandbox** contract (next document) — referenced via a `ToolExecutor` boundary; the **Security** analyzer/policy (BoD §17) — referenced via `SecurityAnalyzer`/`ConfirmationPolicy` boundaries.
**Source grounding:** the loop mechanics below are taken from direct reading of the OpenHands SDK (`conversation/impl/local_conversation.py`, `conversation/stuck_detector.py`, `security/`), re-implemented — not copied. Cited inline as `[OH]`.

---

## 0. What this document is (and is not)

**Is:** the binding contract for the agent's control loop — the status state machine, the single-step lifecycle, concurrency/steering, the two-phase confirmation gate, stuck detection, completion (stop-hooks), and how the loop drives the event log and the router. Implementable from this doc; other subsystems bind to the boundaries it defines.

**Is not:** the tools themselves, the sandbox, the security analyzer's scoring internals, or the prompt prose. Those are separate subsystems the loop *invokes* across the boundaries named in §0-deps. This document defines exactly what the loop hands them and expects back, and nothing about their interiors.

**Conventions** (identical to prior contracts): illustrative Python 3.12 + Pydantic v2; field names/types/signatures **normative**, bodies illustrative. **[CONTRACT]** = relied-upon guarantee; **[INTERIOR]** = builder's free choice; **[VERIFY]** = confirm at build.

---

## 1. Foundational principles [CONTRACT]

The loop's correctness *is* the product's reliability (BoD §1.1). These principles are non-negotiable.

1. **One action per iteration, observed before the next** (BoD Principle 6). Each step produces at most one `ActionEvent`; its result (`ObservationEvent`/`AgentErrorEvent`) is appended and visible before the next step runs. No batching of un-observed actions.
2. **Explicit status state machine, not implicit `while not done`** `[OH]`. The loop's behavior is fully determined by `ConversationStatus` (from the event contract) and the transitions in §2. Status is reconstructed from the event log, never held only in memory.
3. **State is derived; the log is truth.** The loop reads `ConversationState` via `reconstruct()`/`get_state()` and the model-facing `View`; it never keeps a private authoritative copy. Crash + restart = replay + resume, for free (event contract invariant #3).
4. **A hard iteration ceiling is the ultimate backstop** `[OH: max_iteration_per_run]`. `max_iterations` (default 500) terminates any run regardless of other exits. It is the safety net beneath stuck detection.
5. **Concurrency is serialized under a per-conversation lock** `[OH]`. State mutation and stepping take the same FIFO lock, so a pause/steer/confirm can never land mid-action, and concurrent user input is never lost.
6. **Every state-changing or risky action is gated before execution** (BoD §17). The agent self-assesses risk; an independent analyzer scores it; a confirmation policy decides; gating is implemented as the two-phase step (§5), at the loop level — not bolted on in the UI.
7. **The loop is transport-agnostic.** It produces events and consumes messages; whether those arrive via WebSocket, REST, or a test harness is irrelevant to the loop (the event contract's wire layer adapts). The loop is drivable headless.

---

## 2. The status state machine [CONTRACT]

States are the event contract's `ConversationStatus`: `IDLE, RUNNING, PAUSED, STUCK, WAITING_FOR_CONFIRMATION, FINISHED, ERROR`. Every transition is recorded as a `StatusEvent` (event contract §2.3), so the machine is reconstructable.

```
                 ┌────────────── user message / steer ──────────────┐
                 ▼                                                    │
   (start) ──► IDLE ──run()──► RUNNING ──step──► RUNNING (loop)       │
                 ▲                │  │  │  │                          │
                 │                │  │  │  └─ risky action ─► WAITING_FOR_CONFIRMATION
                 │                │  │  │                         │   │
                 │                │  │  │      confirm ───────────┘   │
                 │                │  │  │      reject ──► RUNNING ─────┤
                 │                │  │  └─ stuck detected ─► STUCK ────┤ (user msg resumes)
                 │                │  └─ max_iterations ─► ERROR        │
                 │                └─ agent declares done ─► FINISHED ──┘
                 │                                            │
                 └─────────── pause ◄─ RUNNING ; resume ─► RUNNING
                                       PAUSED
   any state ─ fatal error ─► ERROR
```

**[CONTRACT] transition rules (relied on by the UI and the agent-server):**
- `IDLE → RUNNING`: on `run()` with at least one pending user message.
- `RUNNING → RUNNING`: normal step completion (more work to do).
- `RUNNING → WAITING_FOR_CONFIRMATION`: an action needs confirmation (§5); `State.pending_action_id` is set.
- `WAITING_FOR_CONFIRMATION → RUNNING`: on `confirm` (executes the pending action) or `reject` (records denial, does not execute).
- `RUNNING → STUCK`: stuck detector fires (§6); loop stops. A subsequent user message (`send_message`/`steer`) flips to `IDLE` and re-enters `RUNNING` `[OH: don't-drop + reset-after-user-message]`.
- `RUNNING → FINISHED`: the agent emits a finish signal **and** no stop-hook vetoes it (§7).
- `RUNNING → ERROR`: `max_iterations` reached (an `ErrorEvent(code="max_iterations")`) or an unrecoverable error.
- `RUNNING ↔ PAUSED`: `pause`/`resume`; pause takes effect *between* steps (§4), never mid-action.
- `FINISHED → IDLE`: a new user message reopens the conversation (the loop does **not** terminate the conversation on FINISHED — it idles).

---

## 3. The loop boundaries (what it depends on) [CONTRACT]

The loop is the orchestrator; it invokes four collaborators across clean boundaries. Three are defined in other contracts; one (`ToolExecutor`) is the seam to the next document.

```python
from __future__ import annotations
from typing import Protocol, Literal
# from core.events import (Event, ActionEvent, ObservationEvent, AgentErrorEvent,
#   MessageEvent, StatusEvent, ErrorEvent, CondensationEvent, ConversationState,
#   ConversationStatus, View, EventStore, ToolCall, ToolResult, SecurityRisk,
#   EventSource, Condenser, Summarizer, is_context_window_exceeded)
# from core.llm import (LLMRouter, CompletionRequest, CapabilityProfile, ModelRole,
#   Difficulty, OperatingMode, ToolSpec, ProposedToolCall, OverflowSignal,
#   LLMContextWindowExceeded)

class ToolExecutor(Protocol):
    """[CONTRACT BOUNDARY — defined in the Tool/Sandbox contract, next doc]
    Executes one ToolCall (often in the sandbox) and returns a ToolResult.
    The loop neither knows nor cares whether execution is local or sandboxed."""
    async def execute(self, call: ToolCall) -> ToolResult: ...
    def available_tools(self) -> list[ToolSpec]: ...   # what the model may call

class SecurityAnalyzer(Protocol):
    """[CONTRACT BOUNDARY — defined in the Security design, BoD §17]
    Scores a proposed action's risk BEFORE execution. May override the agent's
    self-assessment."""
    def assess(self, action: ActionEvent) -> SecurityRisk: ...

class ConfirmationPolicy(Protocol):
    """[CONTRACT BOUNDARY — Security design]. Decides whether a given risk
    requires human confirmation."""
    def should_confirm(self, risk: SecurityRisk) -> bool: ...

class Agent(Protocol):
    """[CONTRACT] The 'brain': given the current View (model-facing messages)
    and the available tools, produce the next step — text and/or one tool call,
    with a self-assessed risk. Wraps the LLMRouter; does NOT execute tools.

    [CONTRACT] Returns exactly ONE proposed action (or a finish/no-op). The loop
    enforces one-action-per-iteration; the Agent must not return multiple
    tool calls to be run without observation."""
    async def step(self, view: View, tools: list[ToolSpec], *,
                   mode: OperatingMode, overflow_signal: OverflowSignal) -> "AgentStep": ...

class AgentStep(BaseModel):
    """The product of one Agent.step(). The loop converts this into events."""
    model_config = ConfigDict(frozen=True)
    thought: str = ""
    tool_call: ToolCall | None = None       # None => no action this step
    self_assessed_risk: SecurityRisk = SecurityRisk.UNKNOWN
    finished: bool = False                  # agent declares the goal complete
    llm_response_id: str | None = None      # carried into ActionEvent
```

**[CONTRACT] the Agent boundary:** `Agent.step()` builds a `CompletionRequest(profile=CapabilityProfile(role=AGENT_DRIVER, difficulty=…, requirements=…, mode=…), messages=view.messages, tools=tools)`, calls `LLMRouter.complete()`, and maps the response's first `ProposedToolCall` → a `ToolCall`. It returns **one** `AgentStep`. The `overflow_signal` (consecutive errors, failed attempts) flows from the loop into the router's overflow policy (router contract §5). This is the only place the model is consulted for action selection.

---

## 4. The run loop & single-step lifecycle [CONTRACT semantics; INTERIOR code]

The loop is a bounded iteration over `Agent.step()`, with every status check and state mutation under a per-conversation FIFO lock (principle 5). The **semantics** below are contractual; the exact code is illustrative.

```python
class AgentLoop:
    def __init__(self, conversation_id, store: EventStore, agent: Agent,
                 executor: ToolExecutor, router: LLMRouter,
                 analyzer: SecurityAnalyzer, policy: ConfirmationPolicy,
                 condenser: Condenser, summarizer: Summarizer,
                 *, mode: OperatingMode, max_iterations: int = 500,
                 stop_hooks: list["StopHook"] | None = None,
                 stuck_thresholds: "StuckThresholds" | None = None): ...

    async def run(self) -> ConversationState:
        """Drive the conversation until a terminal-for-now status (FINISHED,
        STUCK, ERROR, PAUSED, or WAITING_FOR_CONFIRMATION). Idempotent to call
        again after a pause/confirmation. [CONTRACT] returns the resulting
        ConversationState."""
        await self._emit(StatusEvent(status=ConversationStatus.RUNNING))
        while True:
            async with self._lock:                          # FIFO, principle 5
                state = await self.store.get_state(self.conversation_id)

                # (a) honor control transitions decided between steps
                if state.execution_status == ConversationStatus.PAUSED:
                    return state                              # stay paused
                if state.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION:
                    return state                              # await confirm/reject
                # a fresh user message (or steer) since FINISHED/STUCK resets us
                if self._has_unprocessed_user_message(state):
                    await self._emit(StatusEvent(status=ConversationStatus.RUNNING))

                # (b) iteration ceiling — the ultimate backstop (principle 4)
                if state.iteration >= self.max_iterations:
                    await self._emit(ErrorEvent(code="max_iterations",
                        detail=f"reached {self.max_iterations}"))
                    return await self.store.get_state(self.conversation_id)

                # (c) stuck detection BEFORE doing more work (§6)
                if self._stuck_detector.is_stuck(await self._recent_events()):
                    await self._emit(StatusEvent(status=ConversationStatus.STUCK))
                    return await self.store.get_state(self.conversation_id)

                # (d) build the model-facing View, condensing if triggered (§8)
                view = await self._materialize_view()        # may append a Condensation

                # (e) ask the agent for ONE action (principle 1)
                step = await self.agent.step(
                    view, self.executor.available_tools(),
                    mode=self.mode, overflow_signal=self._overflow_signal(state))

                # (f) finish path — subject to stop-hook veto (§7)
                if step.finished and step.tool_call is None:
                    if await self._stop_allowed(state):
                        await self._emit(StatusEvent(status=ConversationStatus.FINISHED))
                        return await self.store.get_state(self.conversation_id)
                    else:
                        # hook vetoed; inject feedback as an environment message
                        await self._emit(MessageEvent(source=EventSource.ENVIRONMENT,
                            message=LLMMessage(role="user", content=self._veto_feedback)))
                        continue

                # (g) no-op step (thought only) — record and continue
                if step.tool_call is None:
                    await self._emit(MessageEvent(source=EventSource.AGENT,
                        message=LLMMessage(role="assistant", content=step.thought)))
                    continue

                # (h) build the ActionEvent (carries thought + self-assessed risk)
                action = ActionEvent(thought=step.thought, tool_call=step.tool_call,
                    self_assessed_risk=step.self_assessed_risk,
                    llm_response_id=step.llm_response_id)

                # (i) RISK GATE — assess, then maybe require confirmation (§5)
                risk = self.analyzer.assess(action)
                if self.policy.should_confirm(risk):
                    await self._emit(action)                 # record the PROPOSED action
                    await self._emit(StatusEvent(
                        status=ConversationStatus.WAITING_FOR_CONFIRMATION,
                        detail=action.id))                    # pending_action_id
                    return await self.store.get_state(self.conversation_id)

            # (j) EXECUTE outside the lock (long-running; lock only guards state)
            await self._emit(action)                          # record the action
            await self._execute_and_observe(action)           # appends Obs/Error (§4.1)
            # loop continues
```

### 4.1 Execute-and-observe [CONTRACT]
```python
async def _execute_and_observe(self, action: ActionEvent) -> None:
    """Execute one tool call and append exactly one ObservationEvent (success)
    or AgentErrorEvent (failure), correlated by action.id / call_id.
    [CONTRACT] every executed ActionEvent yields exactly one observation event."""
    try:
        result: ToolResult = await self.executor.execute(action.tool_call)
        if result.success:
            await self._emit(ObservationEvent(tool_result=result, action_id=action.id))
        else:
            await self._emit(AgentErrorEvent(error=result.error or "tool failed",
                                             action_id=action.id))
    except LLMContextWindowExceeded:
        raise            # handled by the view-materialization hard-reset (§8)
    except Exception as e:                                    # tool/exec failure
        await self._emit(AgentErrorEvent(error=str(e), action_id=action.id))
```

**[CONTRACT] guarantees the rest of the system relies on:**
- One executed `ActionEvent` ⇒ exactly one `ObservationEvent` or `AgentErrorEvent` (pairing by `action_id`). Stuck detection and the View depend on this pairing.
- Execution happens **outside** the state lock (step j) — tool calls can be slow; the lock only guards state transitions (principle 5). Re-acquire on the next iteration.
- The proposed `ActionEvent` is recorded **before** execution (steps i/j), so a crash mid-execution leaves a recoverable trace (a dangling action with no observation = re-runnable or surfaced).

---

## 5. Two-phase confirmation (HIL at the loop level) [CONTRACT]

This is the §17 risk gate and the §13.2 plan-preview gate, unified as one mechanism `[OH: confirmation mode]`.

- **Phase 1 (propose):** when `policy.should_confirm(risk)` is true, the loop emits the `ActionEvent` (so the UI can show exactly what is proposed) and a `StatusEvent(WAITING_FOR_CONFIRMATION)` whose detail carries the action id → `State.pending_action_id`. The loop then **returns** (stops). No execution has happened.
- **Phase 2 (resolve):** the user sends `confirm` or `reject` (event contract wire frames §7.3):
  - `confirm` → the loop resumes; it executes **exactly** `State.pending_action_id` via `_execute_and_observe`, then continues. [CONTRACT] confirmation executes the already-proposed action — it does not re-ask the model.
  - `reject` → the loop appends an `AgentErrorEvent`/`MessageEvent` recording denial (so the model sees it next View) and returns to `RUNNING` without executing.
- **Plan-preview gate:** the same machinery gates a *whole plan* up front — the first proposed action of a run (or a dedicated "present plan" action) is confirmation-gated, giving the BoD §13.2 read-narrow-correct moment before compute is spent.

**[CONTRACT]:** the confirmation decision is the `ConfirmationPolicy`'s alone (Security design); the loop only mechanizes propose→wait→execute. Per-surface defaults (Research = `NeverConfirm`, Agent = `ConfirmRisky`) are configured at loop construction (BoD §8).

---

## 6. Stuck detection [CONTRACT] — ported from source, re-implemented

Checked **every iteration before stepping** (step c). On detection: emit `StatusEvent(STUCK)` and stop. Re-implements the SDK's detector `[OH: stuck_detector.py]`.

```python
class StuckThresholds(BaseModel):
    repeat_action_observation: int = 3   # identical action→obs cycles
    repeat_action_error: int = 3         # identical action→error cycles
    agent_monologue: int = 4             # consecutive agent msgs, no user
    alternating: int = 3                 # A-B-A-B cycles
    scan_window: int = 20                # only inspect the last N events

class StuckDetector:
    def is_stuck(self, recent: list[Event]) -> bool:
        """[CONTRACT] Pure function of the recent event window. Returns True if
        any stuck pattern is present. Uses event_content_eq (event contract
        §6.3) so it matches SEMANTIC repetition, ignoring volatile ids."""
        recent = self._after_last_user_message(recent)   # reset on new instruction
        return (self._repeated_action_observation(recent)
                or self._repeated_action_error(recent)
                or self._agent_monologue(recent)
                or self._alternating(recent))
                # pattern 5 (context-window-error loop): see note below
```

**[CONTRACT] behavior:**
- **Window:** inspect only the last `scan_window` events — never materialize the whole log `[OH]`.
- **Reset:** discard events at/before the last `USER` `MessageEvent` — a new instruction means "not stuck" `[OH]`.
- **Equality:** comparisons use `event_content_eq` (event contract), so byte-differences in ids/timestamps don't mask a semantic loop.
- **Five patterns** (BoD §12.5): (1) repeated action→observation, (2) repeated action→error [the "retry the same failing call" failure], (3) agent monologue, (4) alternating A-B-A-B, (5) **context-window-error loop** — *known-hard; even the source stubs it*. We do **not** rely on detecting pattern 5 in the loop; instead we prevent its cause via the hard-reset condensation (§8) and bound it with `max_iterations`. [VERIFY/OPEN] revisit if a robust signal emerges.

On STUCK, the conversation is not dead: a user `send_message`/`steer` resets and resumes (§2). [INTERIOR] the loop may also auto-escalate a stuck step to overflow via the `overflow_signal` (router contract rule 4) *before* declaring STUCK — recommended, configurable.

---

## 7. Concurrency, steering & completion [CONTRACT]

### 7.1 The lock & don't-drop-messages `[OH]`
- A single per-conversation **FIFO async lock** guards every status read and state mutation. Stepping holds it except during tool execution (§4 step j). This makes pause/steer/confirm race-free: they can only take effect *between* steps.
- **Concurrent user input is never lost** (principle 5): a `send_message`/`steer` arriving while the loop is mid-run is persisted (event contract pending-buffer §7.4) and appears as a `USER` `MessageEvent`; the loop's next iteration sees it (the `_has_unprocessed_user_message` check, §4 step a). Critically, the loop does **not** terminate on FINISHED in a way that drops a just-arrived message — FINISHED idles, and a new message reopens it.

### 7.2 Steering [CONTRACT]
`steer` (event contract wire §7.3) is a `USER` `MessageEvent` injected at the next checkpoint. The agent replans on its next `step()` because the new message is in the View — **no session/context is lost** (BoD §13.4 "Steering Wheel"). Steering differs from a normal message only in UI intent; mechanically both are user messages to the log. Steering during `RUNNING` does not require a pause; it lands at the next iteration boundary.

### 7.3 Pause / resume / cancel [CONTRACT]
- `pause` → emit `StatusEvent(PAUSED)`; the loop returns at the next checkpoint (never mid-action). `resume` → emit `StatusEvent(RUNNING)` and call `run()` again.
- `cancel` → graceful stop: emit a terminal `StatusEvent` and stop the loop. Distinct from the **kill switch** (BoD §13.6), which is a network-level capability/token revocation handled outside the loop (it additionally triggers rollback-and-quarantine); the loop's `cancel` is the cooperative stop.

### 7.4 Stop-hooks (programmable completion gate) [CONTRACT]
```python
class StopHook(Protocol):
    """[CONTRACT] Consulted when the agent declares finished. Returning False
    VETOES completion and the loop continues (with injected feedback)."""
    async def allow_stop(self, state: ConversationState, events: list[Event]) -> bool: ...
```
- On `step.finished`, the loop runs all stop-hooks; if **any** returns False, completion is vetoed, feedback is injected as an environment message, and the loop continues `[OH: stop hooks]`.
- This is the home for **acceptance checks** and, later, the **critic** (BoD §12.8) and the **visual-iteration** loop — a hook can render/inspect output and demand revision before allowing FINISHED. v1 ships with zero or a trivial hook; the seam exists. [OPEN: critic integration — BoD §24-D3.]

---

## 8. View materialization & condensation wiring [CONTRACT]

This is where the loop meets the event contract's memory system and the router's summarizer. The loop owns *when* to condense; the `Condenser` owns *how* (event contract §5.2).

```python
async def _materialize_view(self) -> View:
    """Build the model-facing View, condensing if the Condenser requests it.
    [CONTRACT] returns a View; may append at most one CondensationEvent."""
    events = await self.store.get_events(self.conversation_id)
    view = View.of(events)
    req = self.condenser.should_condense(view, token_count=self._estimate_tokens(view))  # sync (pure)
    if req is not None:
        tombstone = await self.condenser.condense(events, view, summarizer=self.summarizer)  # async (event contract v1.2)
        if tombstone is not None:
            await self._emit(tombstone)                      # append-only
            view = View.of(await self.store.get_events(self.conversation_id))
    return view
```

**[CONTRACT] behavior:**
- **Soft trigger:** if the condenser returns a request but produces no tombstone this step (not structurally possible), proceed with the uncondensed View and retry next iteration (event contract §5.2). Non-fatal.
- **Hard trigger (context-window-exceeded):** if `Agent.step()` raises `LLMContextWindowExceeded` (router contract §6 / `is_context_window_exceeded`), the loop issues a **hard** condensation request (forget-and-summarize-all-but-keep_first, with retries) before retrying the step. This is the loop's handling of the one stuck-pattern detection can't catch (§6 note). If the hard reset itself cannot make progress, emit a fatal `ErrorEvent` and go to `ERROR`.
- **Summarization model:** always the cheap `SUMMARIZER`-role model via the router (router contract §9.1) — never the agent model (BoD §7.3).
- **Cadence:** the condenser condenses regularly in moderate amounts (prompt-cache-aware); the loop simply asks every iteration and the condenser's `should_condense` enforces the policy.

---

## 9. Planning & operating modes [CONTRACT]

- **Externalized plan:** the loop does not hold the plan only in context. The agent maintains a visible plan (a task list) as content the user sees and that survives condensation — for the Agent surface this is the BoD §13.2/§13.4 plan; for Research/Deep Research it is the investigation outline. [INTERIOR] whether the plan is a dedicated event subtype, a pinned message, or a persistent-memory artifact (event contract §7.4) — but it MUST be reconstructable and survive condensation (use `keep_first` and/or persistent memory, not the condensable middle).
- **Operating mode** (router contract `OperatingMode`) is fixed per surface at loop construction and passed into `Agent.step()`, which selects the matching prompt variant via the router (router contract §8): Research → INTERACTIVE; Deep Research / long Agent tasks → LONG_HORIZON or PLANNING. The loop machinery is identical across modes (BoD §3.1 "one core, many surfaces"); only the mode, tool scope (via the `ToolExecutor`'s `available_tools`), and confirmation policy differ.

---

## 10. Test plan [CONTRACT — defines correctness]

Headless. The Agent, ToolExecutor, SecurityAnalyzer, ConfirmationPolicy, Condenser, and Summarizer are all **faked** — no real model, no sandbox. This is exactly why the loop lives in `core` (BoD §5.1). The subsystem is done when these pass.

### 10.1 State machine
- Every transition in §2 is exercised with a scripted fake Agent; assert the emitted `StatusEvent` sequence and the reconstructed `ConversationState`.
- `FINISHED → IDLE` on a new user message (the conversation is not dead).
- `max_iterations` forces `ERROR` with `ErrorEvent(code="max_iterations")`.

### 10.2 One-action-per-iteration
- A fake Agent returning a single tool call yields exactly one `ActionEvent` + one observation per iteration; assert no un-observed action precedes the next step.
- A fake Agent that (incorrectly) tries to return multiple calls is reduced to one by the boundary (the loop/Agent contract takes the first; assert the guarantee holds).

### 10.3 Execute-and-observe pairing
- Success → exactly one `ObservationEvent` with matching `action_id`. Failure (executor raises) → exactly one `AgentErrorEvent` with matching `action_id`. Never zero, never two.
- Crash simulation: kill after the `ActionEvent` is emitted but before observation; on restart+replay, the dangling action is detectable (no paired observation).

### 10.4 Two-phase confirmation
- With a policy that confirms on HIGH: a HIGH-risk action emits the proposed `ActionEvent` + `WAITING_FOR_CONFIRMATION` and **does not execute**; `confirm` executes exactly that action; `reject` records denial and resumes without executing.
- With `NeverConfirm` (Research surface): no action is ever gated.
- Plan-preview: the first action of a run is gated under the Agent-surface policy.

### 10.5 Stuck detection
- Each of the four implemented patterns triggers STUCK at its threshold (table-driven), using `event_content_eq` so id/timestamp differences don't mask repetition.
- Reset: a `USER` message inside the window clears the stuck condition.
- Window: events older than `scan_window` don't contribute.
- A `steer`/message after STUCK resumes the loop.

### 10.6 Concurrency & steering
- A `send_message` delivered while `RUNNING` is picked up at the next iteration (not dropped); assert the `USER` `MessageEvent` appears and influences the next `Agent.step()` View.
- `pause` lands between steps (never mid-`_execute_and_observe`); `resume` continues; `cancel` stops terminally.

### 10.7 Stop-hooks
- A hook returning False vetoes FINISHED, injects feedback, and the loop continues; with no hooks, FINISHED is immediate.

### 10.8 Condensation wiring
- Soft request producing no tombstone → loop proceeds uncondensed and retries (non-fatal).
- `LLMContextWindowExceeded` from the fake Agent → loop triggers a hard reset (appends a Condensation) then retries the step; unrecoverable hard reset → `ERROR`.
- Summarization always routes the `SUMMARIZER` role (assert via the fake router), never `AGENT_DRIVER`.

### 10.9 Cross-contract integration (the acceptance gate)
- End-to-end with fakes: user message → `run()` → fake Agent proposes a tool call (built as a `CompletionRequest(AGENT_DRIVER)` to the fake router) → loop gates per policy → executes via fake executor → appends observation → fake Agent declares finished → FINISHED. Assert the full event log replays to the same final `ConversationState`, and that `request_id` flowed into `ActionEvent.llm_response_id`. This proves loop + event contract + router contract compose.

---

## 11. What this contract hands to / expects from each subsystem

- **Tool/Sandbox (next contract):** the loop expects a `ToolExecutor` (`execute(ToolCall)->ToolResult`, `available_tools()->[ToolSpec]`). That contract defines execution, sandboxing, and the concrete tools; the loop only calls this boundary. **This is the next document.**
- **Security (BoD §17):** the loop expects `SecurityAnalyzer.assess(action)->SecurityRisk` and `ConfirmationPolicy.should_confirm(risk)->bool`. The Security design provides them; the loop mechanizes the two-phase gate (§5).
- **Agent/LLM (router contract):** `Agent` wraps `LLMRouter`; the loop passes `mode` + `overflow_signal` and consumes one `AgentStep`.
- **Memory (event contract):** the loop calls `View.of`, `should_condense`/`condense`, and the `Summarizer` (router-backed); it appends `CondensationEvent`s only via the condenser.
- **Event store/wire (event contract):** the loop emits all events via `EventStore.append`; control frames (`confirm`/`reject`/`steer`/`pause`/`resume`/`cancel`) arrive via the wire layer and map to the transitions in §2.
- **UI (BoD §13) / agent-server (BoD §5):** consume the `StatusEvent` stream and `ConversationState` to render the three-tier disclosure, the plan, the confirmation prompts, and the steering control.
- **Critic / visual iteration (BoD §12.8):** plugs in as a `StopHook` (§7.4) — deferred (D3).

---

## 12. Build order (this subsystem) & sequencing

This is a **Phase-1** subsystem and the first that requires both prior contracts. It does **not** block Phase 0.

1. The boundaries (§3): `Agent`, `ToolExecutor`, `SecurityAnalyzer`, `ConfirmationPolicy` protocols + a **fake** of each for tests.
2. The state machine + `run()` skeleton (§2, §4) with the FIFO lock and `max_iterations`, emitting `StatusEvent`s; tests 10.1/10.2.
3. `_execute_and_observe` + pairing guarantees (§4.1); tests 10.3.
4. Two-phase confirmation (§5); tests 10.4.
5. `StuckDetector` (§6) using `event_content_eq`; tests 10.5.
6. Concurrency/steering/pause/cancel + stop-hooks (§7); tests 10.6/10.7.
7. View-materialization + condensation wiring (§8) against the real `Condenser` (event contract) and router-backed `Summarizer`; tests 10.8.
8. The §10.9 cross-contract integration test — the acceptance gate.

**Then** the real `Agent` is wired to the real `LLMRouter` (router contract) and the real `ToolExecutor` is supplied by the Tool/Sandbox contract — at which point Phase 1 is a working agent.

---

### Appendix — open items specific to this contract
- **[OPEN] Plan representation.** Dedicated event subtype vs. pinned message vs. persistent-memory artifact (§9). Any is contract-valid if it survives condensation and is reconstructable; decide in Phase 1.
- **[OPEN] Auto-escalate-before-STUCK.** Whether a struggling step escalates to overflow (router rule 4) before declaring STUCK is recommended and configurable; tune empirically (BoD §24-D6 neighbors).
- **[OPEN] Critic as stop-hook (§7.4).** Deferred to after reading the OpenHands `critic/` source (BoD §24-D3).
- **[VERIFY] Token estimation** for `should_condense` (§8) — a cheap heuristic vs. a tokenizer call; INTERIOR, but pick one in Phase 1.
- **Next contract to write:** the **Tool/Sandbox boundary** — the loop's `ToolExecutor` dependency, and the seam to the secure execution environment (BoD §10/§11). After that, retrieval/grounding and the security analyzer detail.
