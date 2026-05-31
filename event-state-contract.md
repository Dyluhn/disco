# Technical Design Contract — Event & State Model

**Document type:** Detailed Technical Design (Contract Spec)
**Subsystem:** Event & State Model (the spine) — BoD §7
**Status:** v1.0 — authoritative contract
**Consumed by:** every other subsystem. This document defines the shared vocabulary the rest of the build cites. Changes here ripple everywhere; treat the schemas and interfaces below as the stable contract and the rationale/notes as guidance.

---

## 0. What this document is (and is not)

**Is:** the binding contract for how state is represented, stored, reconstructed, condensed, and streamed. It specifies the *seams* — the data shapes and interface boundaries that independent subsystems must agree on. An AI coding agent (or a human) should be able to implement this subsystem from this document with no further architectural decisions, and any *other* subsystem should be able to depend on these types without guessing.

**Is not:** an implementation of the agent loop, the tools, or the LLM router. Those are separate subsystems that *consume* this contract. Where this document touches them, it defines only the boundary (what they hand in, what they get back), not their internals. Interior implementation choices below the contract line are left to the builder.

**Conventions:**
- Code is illustrative Python 3.12 + Pydantic v2. Field names, types, and method signatures are **normative**; bodies are illustrative.
- **[CONTRACT]** marks a guarantee other subsystems may rely on.
- **[INTERIOR]** marks something the builder may implement freely as long as the contract holds.
- **[VERIFY]** marks a fast-moving detail (library version, etc.) to confirm at build.
- Aligns with BoD §7 and Principle 3 (append-only log is source of truth).

---

## 1. Foundational invariants [CONTRACT]

These are the load-bearing guarantees. Everything else derives from them. If any is violated, the subsystem is incorrect.

1. **Append-only.** Events are only ever appended. No event is mutated or deleted after it is persisted. (Forgetting is done via tombstones — §5 — never deletion.)
2. **Total order per conversation.** Within a single conversation, events have a strict, gap-free, monotonically increasing sequence number assigned at append time by the store. Order is the order of appends.
3. **Deterministic reconstruction.** Given the ordered event list for a conversation, the conversation `State` (§3) and the LLM `View` (§5) are pure functions of that list. Same events → same state, every time. No hidden state.
4. **Self-describing & versioned.** Every event carries a discriminator (`kind`) and a `schema_version`. Any consumer can deserialize any event without external context, and old events remain readable as schemas evolve.
5. **Identity is stable; volatile fields are marked.** Every event has a stable `id`. Fields that legitimately vary between otherwise-equivalent events (tool-call ids, model-response ids, timestamps, metrics) are explicitly enumerated (§6.3) so equality/comparison logic can ignore them.
6. **Source-attributed.** Every event records its `source` (who/what produced it): `user`, `agent`, `environment`, or `system`. Security and UI both depend on this.

---

## 2. The Event type hierarchy

### 2.1 Base event [CONTRACT]

```python
from __future__ import annotations
from enum import Enum
from datetime import datetime, timezone
from typing import Annotated, Literal, Any
from pydantic import BaseModel, Field, ConfigDict
import uuid

SCHEMA_VERSION = 1  # bump on breaking changes to any event shape

class EventSource(str, Enum):
    USER = "user"
    AGENT = "agent"
    ENVIRONMENT = "environment"   # tool results, injected feedback, hooks
    SYSTEM = "system"             # lifecycle/status, condensation, errors

class EventKind(str, Enum):
    MESSAGE = "message"
    ACTION = "action"
    OBSERVATION = "observation"
    AGENT_ERROR = "agent_error"
    CONDENSATION = "condensation"
    STATUS = "status"
    ERROR = "error"               # conversation-level error (distinct from agent_error)

def _new_id() -> str:
    return f"evt_{uuid.uuid4().hex}"

def _now() -> datetime:
    return datetime.now(timezone.utc)

class BaseEvent(BaseModel):
    """Common envelope for every event. Subtypes add a typed payload.

    [CONTRACT] These fields exist on every event and never change meaning.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=_new_id)
    kind: EventKind                                   # discriminator
    source: EventSource
    timestamp: datetime = Field(default_factory=_now)  # VOLATILE (see §6.3)
    schema_version: int = Field(default=SCHEMA_VERSION)

    # Assigned by the EventStore at append time. None before persistence.
    # [CONTRACT] Monotonic, gap-free, per-conversation. Do NOT set manually.
    seq: int | None = Field(default=None)

    # Free-form, non-semantic metadata (tracing ids, UI hints). VOLATILE.
    # Never load-bearing for reconstruction or equality.
    meta: dict[str, Any] = Field(default_factory=dict)
```

**Why `frozen=True`:** events are immutable by invariant #1. Freezing them at the type level makes accidental mutation a runtime error, not a silent bug. The only field "filled in later" is `seq`, which is set by the store by emitting a copy (`model_copy(update={"seq": n})`), not by mutating in place. [CONTRACT: consumers must treat events as immutable.]

### 2.2 LLM-convertible marker [CONTRACT]

Some events become messages sent to the model; others (status, condensation bookkeeping) do not. This distinction is a **contract the View and the loop both depend on**.

```python
class LLMConvertible:
    """Marker mixin. Events that subclass this can be rendered into an LLM
    message via to_llm_message(). The View (§5) only ever materializes
    LLMConvertible events. [CONTRACT]
    """
    def to_llm_message(self) -> "LLMMessage":
        raise NotImplementedError
```

`MessageEvent`, `ActionEvent`, `ObservationEvent`, and `AgentErrorEvent` are `LLMConvertible`. `StatusEvent`, `ErrorEvent`, and the `CondensationEvent` tombstone are **not** (the tombstone is bookkeeping; its *summary* is what reaches the model, via the View).

### 2.3 The concrete events [CONTRACT]

```python
# ---- payload value objects ---------------------------------------------------

class LLMMessage(BaseModel):
    """Provider-neutral message shape. The LLM router (separate subsystem)
    maps this to/from provider formats. [CONTRACT at the router boundary.]"""
    model_config = ConfigDict(frozen=True)
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    # Optional structured content (tool calls/results) carried opaquely;
    # the router owns provider-specific shaping.
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None   # VOLATILE

class ToolCall(BaseModel):
    """A request to execute one tool. Produced by the agent, consumed by the
    tool subsystem (§ tool contract, separate doc)."""
    model_config = ConfigDict(frozen=True)
    tool_name: str
    arguments: dict[str, Any]
    call_id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex}")  # VOLATILE

class ToolResult(BaseModel):
    """The outcome of executing a ToolCall. Produced by the tool subsystem."""
    model_config = ConfigDict(frozen=True)
    call_id: str                      # VOLATILE (correlates to ToolCall)
    tool_name: str
    success: bool
    content: str                      # human/LLM-readable result text
    structured: dict[str, Any] | None = None  # optional machine payload
    error: str | None = None          # populated iff success is False

# ---- security (mirrors §17 / security contract) ------------------------------

class SecurityRisk(str, Enum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

# ---- concrete event types ----------------------------------------------------

class MessageEvent(BaseEvent, LLMConvertible):
    kind: Literal[EventKind.MESSAGE] = EventKind.MESSAGE
    message: LLMMessage
    def to_llm_message(self) -> LLMMessage:
        return self.message

class ActionEvent(BaseEvent, LLMConvertible):
    """The agent chose to take one tool action. One action per event
    (Principle 6). Carries the agent's reasoning and self-assessed risk."""
    kind: Literal[EventKind.ACTION] = EventKind.ACTION
    source: EventSource = EventSource.AGENT
    thought: str                      # the agent's reasoning for this action
    tool_call: ToolCall
    # Agent's self-assessed risk; the independent analyzer may override
    # downstream (security contract). Part of the event for audit.
    self_assessed_risk: SecurityRisk = SecurityRisk.UNKNOWN
    # VOLATILE: correlates to the model completion that produced this action.
    llm_response_id: str | None = None
    def to_llm_message(self) -> LLMMessage:
        return LLMMessage(
            role="assistant",
            content=self.thought,
            tool_calls=[{"id": self.tool_call.call_id,
                         "name": self.tool_call.tool_name,
                         "arguments": self.tool_call.arguments}],
        )

class ObservationEvent(BaseEvent, LLMConvertible):
    """The result of an ActionEvent's tool call (success path)."""
    kind: Literal[EventKind.OBSERVATION] = EventKind.OBSERVATION
    source: EventSource = EventSource.ENVIRONMENT
    tool_result: ToolResult
    # Correlates this observation to its action. NOT volatile for
    # reconstruction (needed to pair action/observation) but IS ignored
    # by stuck-equality (§6.3) since the action content is what matters.
    action_id: str
    def to_llm_message(self) -> LLMMessage:
        return LLMMessage(role="tool",
                          content=self.tool_result.content,
                          tool_call_id=self.tool_result.call_id)

class AgentErrorEvent(BaseEvent, LLMConvertible):
    """An error observation — tool failed, action invalid, execution raised.
    Distinct from ErrorEvent (which is conversation-fatal)."""
    kind: Literal[EventKind.AGENT_ERROR] = EventKind.AGENT_ERROR
    source: EventSource = EventSource.ENVIRONMENT
    error: str
    action_id: str | None = None      # the action that failed, if any
    def to_llm_message(self) -> LLMMessage:
        return LLMMessage(role="tool", content=f"ERROR: {self.error}",
                          tool_call_id=None)

class CondensationEvent(BaseEvent):
    """A TOMBSTONE. Marks a span of prior events as forgotten and records the
    summary that replaces them. NOT LLMConvertible — the View applies it.
    See §5. [CONTRACT: this is how forgetting is represented.]"""
    kind: Literal[EventKind.CONDENSATION] = EventKind.CONDENSATION
    source: EventSource = EventSource.SYSTEM
    # The seq range [start_seq, end_seq] (inclusive) that this condensation
    # forgets. The View drops events whose seq falls in any active range.
    forgotten_start_seq: int
    forgotten_end_seq: int
    # The summary event id(s) this condensation inserts in place of the span.
    # The summary text is carried as a MessageEvent appended alongside, OR
    # inline here; this contract uses inline for atomicity.
    summary: str
    summary_role: Literal["system", "user"] = "user"
    reason: Literal["request", "tokens", "events", "hard_reset"] = "tokens"

class ConversationStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STUCK = "STUCK"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    FINISHED = "FINISHED"
    ERROR = "ERROR"

class StatusEvent(BaseEvent):
    """A lifecycle/status transition. NOT LLMConvertible. Drives the UI and
    the loop's state machine reconstruction (§3)."""
    kind: Literal[EventKind.STATUS] = EventKind.STATUS
    source: EventSource = EventSource.SYSTEM
    status: ConversationStatus
    detail: str | None = None

class ErrorEvent(BaseEvent):
    """A conversation-level (fatal-ish) error, e.g. MaxIterationsReached.
    NOT LLMConvertible."""
    kind: Literal[EventKind.ERROR] = EventKind.ERROR
    source: EventSource = EventSource.SYSTEM
    code: str
    detail: str

# ---- the discriminated union the store/serde use ----------------------------

Event = Annotated[
    MessageEvent | ActionEvent | ObservationEvent | AgentErrorEvent
    | CondensationEvent | StatusEvent | ErrorEvent,
    Field(discriminator="kind"),
]
```

**[CONTRACT] notes that other subsystems rely on:**
- The agent loop produces `ActionEvent`; the tool subsystem produces `ObservationEvent`/`AgentErrorEvent` correlated by `action_id`/`call_id`.
- The security subsystem reads `ActionEvent.self_assessed_risk` and may emit a paired risk record (its own contract); the confirmation gate emits `StatusEvent(WAITING_FOR_CONFIRMATION)`.
- The UI consumes the raw event stream (§7) and renders by `kind` + `source`.
- The memory subsystem only ever appends `CondensationEvent` and reads the `LLMConvertible` subset through the View (§5).

---

## 3. State reconstruction [CONTRACT]

`State` is a **pure function of the ordered event list**. It is never stored as the source of truth — it is derived, cached, and re-derivable. This is invariant #3 made concrete.

```python
class ConversationState(BaseModel):
    """Derived, in-memory projection of the event log. NOT the source of truth.

    [CONTRACT] reconstruct(events) is pure: identical event lists yield
    identical state. The loop reads execution_status from here; the store
    rebuilds this on load by replaying events.
    """
    conversation_id: str
    execution_status: ConversationStatus = ConversationStatus.IDLE
    iteration: int = 0                       # count of ActionEvents in this run
    max_iterations: int = 500                # hard ceiling (BoD §12.1)
    last_seq: int = 0                        # highest seq observed
    # The id of an action awaiting confirmation, if status is
    # WAITING_FOR_CONFIRMATION. Enables the two-phase confirm step (BoD §12.4).
    pending_action_id: str | None = None
    # Feature-scoped scratch state keyed by string (subsystems may stash here).
    # [CONTRACT] keys are namespaced by subsystem, e.g. "memory.last_condense_seq".
    extras: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def reconstruct(cls, conversation_id: str, events: list[Event],
                    max_iterations: int = 500) -> "ConversationState":
        st = cls(conversation_id=conversation_id, max_iterations=max_iterations)
        run_iteration = 0
        for e in events:
            st.last_seq = e.seq or st.last_seq
            if isinstance(e, StatusEvent):
                st.execution_status = e.status
                if e.status != ConversationStatus.WAITING_FOR_CONFIRMATION:
                    st.pending_action_id = None
            elif isinstance(e, ActionEvent):
                run_iteration += 1
                # a fresh user message resets the run counter (see below)
            elif isinstance(e, MessageEvent) and e.source == EventSource.USER:
                run_iteration = 0
            elif isinstance(e, ErrorEvent):
                st.execution_status = ConversationStatus.ERROR
        st.iteration = run_iteration
        return st
```

**[CONTRACT] reconstruction rules other subsystems may rely on:**
- `execution_status` is whatever the **last** `StatusEvent` set, unless a later `ErrorEvent` forced `ERROR`.
- A `USER` `MessageEvent` resets the per-run `iteration` counter to 0 (a new instruction starts a fresh "run" for ceiling and stuck purposes).
- `pending_action_id` is set when the status is `WAITING_FOR_CONFIRMATION` and cleared on any other status (this is what the loop's resume step reads to know which action to execute on approval).
- State is **[INTERIOR]-cacheable**: the store may memoize the projection and update it incrementally on append, as long as it equals a full `reconstruct()`.

---

## 4. Serialization contract [CONTRACT]

Serialization is a hard boundary: events are persisted, streamed over the wire, and (eventually) read by future code. The rules:

1. **Format:** JSON. Each event serializes to a single JSON object via Pydantic `model_dump(mode="json")`. The `kind` field is the discriminator for deserialization via the `Event` union.
2. **Stability:** field names are part of the contract. Renaming a field is a breaking change requiring a `schema_version` bump and a migration shim (§4.1). Adding an **optional** field (with a default) is backward-compatible and does **not** require a bump.
3. **Datetimes:** ISO 8601 UTC strings. Always timezone-aware.
4. **Enums:** serialized by value (the string), never by name/index.
5. **No code in payloads:** `arguments`, `structured`, `meta`, and `extras` are JSON-serializable data only — never pickled objects, never callables.
6. **Determinism:** serialization is stable/ordered (Pydantic preserves field order); two equal events serialize byte-identically (modulo volatile fields). This matters for the dedup/idempotency guarantees in §6.

### 4.1 Schema evolution [CONTRACT]

```python
def migrate_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a persisted event dict to the current SCHEMA_VERSION before
    validation. Pure, idempotent, append-only migrations.

    [CONTRACT] Every reader runs raw dicts through migrate_event() before
    Event validation. Old events stay readable forever (invariant #4).
    """
    v = raw.get("schema_version", 1)
    # Example shape (no migrations yet at v1):
    # if v < 2: raw = _v1_to_v2(raw); v = 2
    return raw
```

Rule: **migrations are forward-only and never lose information.** A v1 event read in a v5 world is upgraded on read; it is never rewritten in the store (append-only, invariant #1).

---

## 5. Memory: View + Condensation [CONTRACT + INTERIOR]

This is the §7.3 design made executable. The **contract** is the View's behavior and the tombstone semantics; the **condensation strategy** below the line is a swappable interior (a `Condenser` interface), with one default implementation specified.

### 5.1 The View [CONTRACT]

```python
class View(BaseModel):
    """The materialized 'what the LLM sees right now', computed from the raw
    event log by applying condensation tombstones. The agent loop and the
    LLM router consume View.messages — never the raw log directly.

    [CONTRACT] View.of(events) is pure. Given the same events it yields the
    same messages, every time.
    """
    messages: list[LLMMessage]
    # seqs of events currently visible (post-condensation), for diagnostics.
    visible_seqs: list[int]
    total_events: int
    forgotten_count: int

    @classmethod
    def of(cls, events: list[Event]) -> "View":
        # 1. Collect active forgotten ranges from condensation tombstones.
        forgotten: list[tuple[int, int]] = [
            (e.forgotten_start_seq, e.forgotten_end_seq)
            for e in events if isinstance(e, CondensationEvent)
        ]
        def is_forgotten(seq: int | None) -> bool:
            return seq is not None and any(a <= seq <= b for a, b in forgotten)

        # 2. Walk events in order. Emit summaries where tombstones sit; drop
        #    forgotten LLMConvertible events; keep the rest.
        msgs: list[LLMMessage] = []
        visible: list[int] = []
        emitted_summary_for: set[int] = set()
        for e in events:
            if isinstance(e, CondensationEvent):
                key = e.forgotten_start_seq
                if key not in emitted_summary_for:
                    msgs.append(LLMMessage(role=e.summary_role, content=e.summary))
                    emitted_summary_for.add(key)
                continue
            if not isinstance(e, LLMConvertible):
                continue                      # status/error: not shown to LLM
            if is_forgotten(e.seq):
                continue                      # forgotten by a tombstone
            msgs.append(e.to_llm_message())
            if e.seq is not None:
                visible.append(e.seq)

        forgotten_n = sum(b - a + 1 for a, b in forgotten)
        return cls(messages=msgs, visible_seqs=visible,
                   total_events=len(events), forgotten_count=forgotten_n)
```

**[CONTRACT] guarantees:**
- The View never shows a forgotten event; it shows the summary in its place, exactly once per tombstone span.
- Non-`LLMConvertible` events (status, error, the tombstone itself) are never in `messages`.
- The View is read-only and derived; computing it has no side effects and never appends.

### 5.2 The Condenser interface [CONTRACT] + default [INTERIOR]

```python
class CondensationRequest(BaseModel):
    soft: bool                # soft = maintain bound; hard = must condense now
    reason: Literal["tokens", "events", "request", "hard_reset"]

class Condenser(Protocol):
    """Decides whether/how to condense. Returns a CondensationEvent to append,
    or None (no-op). [CONTRACT] the loop calls should_condense()/condense();
    the strategy itself is INTERIOR and swappable."""
    def should_condense(self, view: View, *, token_count: int | None) -> CondensationRequest | None: ...
    def condense(self, events: list[Event], view: View, *, summarizer: "Summarizer") -> CondensationEvent | None: ...

class Summarizer(Protocol):
    """Produces summary text for a span of messages. [CONTRACT at the LLM
    boundary] — implemented by the LLM router using a CHEAP model, separate
    from the agent model (BoD §7.3, §15)."""
    def summarize(self, messages: list[LLMMessage]) -> str: ...
```

**Default `LLMSummarizingCondenser` [INTERIOR] — behavior specified, implementation free:**
- Config: `max_size=240` (event count), `max_tokens: int | None`, `keep_first=2`, `minimum_progress=0.1`, `hard_reset_max_retries=5`, `hard_reset_context_scaling=0.8`.
- **Trigger (`should_condense`):** if visible event count > `max_size` or `token_count` > `max_tokens` → soft request (`reason="events"`/`"tokens"`). The loop may also pass an explicit user/agent request (`reason="request"`) or a hard context-window-exceeded signal (`reason="hard_reset"`, soft=False).
- **Strategy (`condense`):** target the **first half** of the visible (non-`keep_first`) events; summarize them via `summarizer`; emit a `CondensationEvent` whose `[forgotten_start_seq, forgotten_end_seq]` covers that span and whose `summary` is the result. Leave the back half untouched.
- **minimum_progress guard:** if the span to forget is < `minimum_progress` of the view → return `None` (no-op) for soft requests; this avoids cache-destroying churn.
- **Soft vs hard:** for a soft request where summarization isn't structurally possible this step, return `None` and let the loop retry next step. For a hard request (no next step possible), perform a full forget-and-summarize of everything except `keep_first`, retrying up to `hard_reset_max_retries` with per-retry input-size scaling by `hard_reset_context_scaling`. If even that fails, raise `NoCondensationAvailable` (a fatal `ErrorEvent`).
- **Cache cadence:** prefer frequent moderate condensations over rare huge ones (each invalidates prompt cache).

**[CONTRACT] the loop relies on:** `condense()` returns at most one `CondensationEvent` to append; appending it changes what `View.of()` subsequently yields; no event is ever deleted.

### 5.3 Context-window-exceeded classification [CONTRACT boundary]

```python
def is_context_window_exceeded(err: Exception) -> bool:
    """Provider-specific. Returns True if an LLM error indicates the context
    window was exceeded (vs. other failures). [CONTRACT] the loop uses this to
    raise a HARD condensation request. Implemented in the LLM router subsystem;
    enumerated per provider. Budget for ongoing maintenance (BoD §7.3)."""
    ...
```

This is the one stuck-pattern (context-window-error loop) even the reference left stubbed; couple it to the hard reset above and the `max_iterations` ceiling as the backstop.

---

## 6. The EventStore interface [CONTRACT]

This is the persistence seam. Every subsystem that reads or writes history goes through this interface. The filesystem/SQLite implementation is v1; the interface is what keeps Postgres/object-storage a swap, not a rewrite (BoD §4.1, §7.2).

```python
from typing import Protocol, AsyncIterator

class EventFilter(BaseModel):
    kinds: list[EventKind] | None = None
    sources: list[EventSource] | None = None
    after_seq: int | None = None          # exclusive lower bound
    before_seq: int | None = None         # exclusive upper bound
    since: datetime | None = None
    until: datetime | None = None

class Page(BaseModel):
    events: list[Event]
    next_cursor: int | None               # pass as after_seq to continue; None = end

class EventStore(Protocol):
    """[CONTRACT] The persistence boundary for events.

    Guarantees implementations MUST uphold:
      G1 append() assigns a monotonic, gap-free, per-conversation seq and
         returns the event with seq populated. Appends to one conversation
         are serialized (no two events share a seq).
      G2 append() is atomic and durable before it returns.
      G3 get_events() returns events in ascending seq order, gap-free.
      G4 idempotency: appending an event whose (conversation_id, id) already
         exists is a no-op returning the existing event (enables safe retries).
      G5 reads never block writes pathologically (impl detail, but required).
    """
    async def append(self, conversation_id: str, event: Event) -> Event: ...
    async def append_many(self, conversation_id: str, events: list[Event]) -> list[Event]: ...
    async def get_events(self, conversation_id: str,
                         filter: EventFilter | None = None) -> list[Event]: ...
    async def paginate(self, conversation_id: str, *, after_seq: int | None = None,
                       limit: int = 100, filter: EventFilter | None = None) -> Page: ...
    async def get_state(self, conversation_id: str) -> ConversationState: ...
    async def subscribe(self, conversation_id: str,
                        after_seq: int | None = None) -> AsyncIterator[Event]: ...
    async def conversation_exists(self, conversation_id: str) -> bool: ...
    async def list_conversations(self, *, owner_id: str,
                                 limit: int = 50, cursor: str | None = None) -> list[str]: ...
```

### 6.1 Ownership [CONTRACT — the multi-user door]
Every conversation carries an `owner_id` from day one (BoD §4.1). In v1 it is a constant. `list_conversations()` filters by it. **No query in the system ever returns cross-owner data.** This single discipline is what makes multi-user a later module-enable rather than a migration.

### 6.2 The v1 implementation [INTERIOR]
- **SQLite** table `events(conversation_id TEXT, seq INTEGER, id TEXT, kind TEXT, payload JSON, created_at TEXT, PRIMARY KEY(conversation_id, seq), UNIQUE(conversation_id, id))`. `seq` assigned via `MAX(seq)+1` within a transaction over the conversation (G1). The `UNIQUE(conversation_id, id)` constraint gives G4 for free.
- A `conversations(conversation_id, owner_id, space_id, title, created_at, status)` table for listing/metadata (status here is a denormalized cache of the derived state, updated by a callback — never the source of truth).
- **Filesystem option** [INTERIOR]: one append-only JSONL file per conversation (`{conversation_id}.jsonl`), seq = line number; a sidecar index for fast pagination. Equivalent guarantees; chosen if you prefer files over SQLite for portability.
- `subscribe()` [INTERIOR]: an in-process async pub/sub (e.g., per-conversation `asyncio.Queue` fan-out) that emits on append; on connect it first drains history after `after_seq`, then live events (this is the §7 reconnect/replay path).

### 6.3 Equality & volatile fields [CONTRACT]
For stuck detection (BoD §12.5) and dedup, define content-equality that **ignores volatile fields**. Volatile fields, enumerated and binding:

> `id`, `seq`, `timestamp`, `meta`, and the correlation ids: `ToolCall.call_id`, `ObservationEvent.action_id`, `AgentErrorEvent.action_id`, `ActionEvent.llm_response_id`, `LLMMessage.tool_call_id`.

```python
def event_content_eq(a: Event, b: Event) -> bool:
    """[CONTRACT] True if two events are semantically equal ignoring volatile
    fields. Used by stuck detection and idempotency reasoning. Same-type only."""
    if type(a) is not type(b) or a.source != b.source:
        return False
    if isinstance(a, ActionEvent) and isinstance(b, ActionEvent):
        return (a.thought == b.thought
                and a.tool_call.tool_name == b.tool_call.tool_name
                and a.tool_call.arguments == b.tool_call.arguments)
    if isinstance(a, ObservationEvent) and isinstance(b, ObservationEvent):
        return (a.tool_result.tool_name == b.tool_result.tool_name
                and a.tool_result.content == b.tool_result.content
                and a.tool_result.success == b.tool_result.success)
    if isinstance(a, AgentErrorEvent) and isinstance(b, AgentErrorEvent):
        return a.error == b.error
    if isinstance(a, MessageEvent) and isinstance(b, MessageEvent):
        return a.message.role == b.message.role and a.message.content == b.message.content
    return a.model_dump(exclude={"id", "seq", "timestamp", "meta"}) == \
           b.model_dump(exclude={"id", "seq", "timestamp", "meta"})
```

This function is consumed by the agent-loop subsystem; it is defined **here** because volatility is a property of the event schema, not of the loop.

---

## 7. The streaming & wire contract [CONTRACT]

This is the seam between agent-server and the frontend. It is the second-most-referenced contract after the events themselves (the UI subsystem binds to it directly). Transport is **WebSocket-primary** (BoD §4.6).

### 7.1 Channels
- **WebSocket** `/ws/conversations/{conversation_id}` — bidirectional, the live channel.
- **REST** for history/replay and convenience send (below).

### 7.2 Server → client frames [CONTRACT]
Every server→client WS frame is one JSON object with a `type`:

```python
class WSServerFrame(BaseModel):
    type: Literal["event", "token", "state", "error", "pong"]
    # type == "event": a newly-appended Event (full object, §2). PRIMARY signal.
    event: Event | None = None
    # type == "token": an incremental token for the in-progress assistant
    # message/thought, for typewriter rendering. Tokens are NOT persisted as
    # events; the final ActionEvent/MessageEvent is the source of truth.
    token: str | None = None
    token_for_event_id: str | None = None
    # type == "state": a ConversationState snapshot (sent on connect + on change).
    state: ConversationState | None = None
    # type == "error": a transport/protocol error (not an agent error).
    error: dict[str, Any] | None = None
```

**[CONTRACT] the UI relies on:**
- On connect, the server sends one `state` frame, then replays missed `event` frames after the client's `last_seq` (full reconnect/rehydrate), then streams live.
- `event` frames are the source of truth; `token` frames are an ephemeral rendering nicety the UI may show but must reconcile to the final `event`.
- Frames for a conversation arrive in `seq` order for `event` frames.

### 7.3 Client → server frames [CONTRACT]
```python
class WSClientFrame(BaseModel):
    type: Literal["send_message", "confirm", "reject", "steer", "pause",
                  "resume", "cancel", "ping"]
    # send_message: a new user message (becomes a MessageEvent).
    content: str | None = None
    # confirm/reject: respond to WAITING_FOR_CONFIRMATION. action_id echoes
    # State.pending_action_id.
    action_id: str | None = None
    # steer: redirect a running agent without losing context (BoD §13.4).
    # Becomes a MessageEvent(source=user) injected at the next loop checkpoint.
    steer_text: str | None = None
    last_seq: int | None = None       # sent on (re)connect to request replay
```

**[CONTRACT] mapping to the loop (the agent-loop subsystem consumes this):**
- `send_message` / `steer` → append `MessageEvent(source=USER)`; if the loop is mid-run, it is picked up at the next iteration's checkpoint (the "don't drop concurrent messages" guarantee, BoD §12.3). `steer` and `send_message` differ only in UI intent; both are user messages to the log.
- `confirm` → the loop's resume executes `State.pending_action_id` (two-phase confirm, BoD §12.4). `reject` → append an `AgentErrorEvent`/`MessageEvent` indicating denial and return to `RUNNING` without executing.
- `pause`/`resume`/`cancel` → drive status transitions; `cancel` is the graceful stop, distinct from the kill switch (which is a separate, network-level control, BoD §13.6).

### 7.4 The pending-message buffer [CONTRACT]
If a `send_message`/`steer`/`confirm` arrives with no live socket (or before the loop is ready), it is **persisted server-side and applied on connect/readiness** — nothing is lost during connection setup (BoD §7.5). Implementation [INTERIOR]: a small per-conversation durable buffer drained in order before live processing.

### 7.5 REST surface [CONTRACT]
- `POST /conversations` → create (with `owner_id`, optional `space_id`); returns `conversation_id` + `conversation_url`.
- `POST /conversations/{id}/messages` → convenience send (thin proxy; equivalent to a `send_message` WS frame, BoD §5.2).
- `GET /conversations/{id}/events?after_seq=&limit=` → paginated history (the replay/`Page` query, §6).
- `GET /conversations/{id}/state` → current derived `ConversationState`.
- `GET /conversations?owner_id=&cursor=` → library listing (owner-scoped).

### 7.6 Side-effects as callbacks [CONTRACT]
Cross-cutting reactions (title generation, vector-indexing of results, notifications, audit) subscribe to the event stream via `EventStore.subscribe()` and run **outside** the request path (BoD §7.5). They may append their own events (e.g., a title is stored on the conversation record; an indexing callback writes to the vector store) but must never block the loop. [CONTRACT] a callback failure is logged and retried; it never fails the originating append.

---

## 8. Test plan [CONTRACT — these tests define correctness]

These tests are part of the contract: the subsystem is "done" when they pass. They are written to be implementable headless (no server, no LLM) — the `core` package is testable in isolation (BoD §5.1, §20).

### 8.1 Invariant tests
- **Append-only:** mutating a persisted event raises (frozen model); the store exposes no update/delete.
- **Monotonic seq (G1):** N concurrent appends to one conversation yield seqs `1..N` with no gaps or duplicates (run under asyncio concurrency to prove serialization).
- **Idempotency (G4):** appending the same event `id` twice returns the existing event; log length unchanged.
- **Atomicity/durability (G2):** an append that completes is present after a simulated process restart (reopen store, replay).

### 8.2 Reconstruction tests
- **Purity:** `reconstruct(events)` twice on the same list yields equal state; shuffling input then re-sorting by seq yields the same state.
- **Status:** last `StatusEvent` wins; a trailing `ErrorEvent` forces `ERROR`.
- **Iteration reset:** a `USER` `MessageEvent` resets `iteration`; `pending_action_id` set on `WAITING_FOR_CONFIRMATION`, cleared otherwise.

### 8.3 View / condensation tests
- **View purity:** `View.of(events)` is deterministic; forgotten events never appear; each tombstone emits its summary exactly once.
- **Non-convertibles excluded:** `StatusEvent`/`ErrorEvent`/`CondensationEvent` never appear in `View.messages`.
- **First-half strategy:** after a soft condensation, the back half is byte-identical in the View; the front half is replaced by one summary.
- **keep_first / minimum_progress:** the first `keep_first` events are never forgotten; a condensation below `minimum_progress` is a no-op.
- **Hard reset:** on a hard request, everything but `keep_first` is forgotten; retries scale input size; exhaustion raises a fatal `ErrorEvent`.
- **Summarizer injected:** tests pass a fake `Summarizer` (no real LLM) and assert the tombstone's `summary` equals its output.

### 8.4 Equality tests
- `event_content_eq` ignores every enumerated volatile field and is sensitive to every semantic field (table-driven: mutate each field, assert eq/neq). This is the test the stuck detector's correctness rests on.

### 8.5 Serialization tests
- **Round-trip:** every event type survives `model_dump(mode="json")` → `migrate_event` → validate, equal to the original.
- **Discriminator:** a heterogeneous JSON list deserializes to the correct concrete types via the `Event` union.
- **Forward-compat:** an event dict missing a field added after v1 (with default) still validates; an unknown future `kind` fails loudly, not silently.

### 8.6 Streaming/wire tests
- **Reconnect/replay:** a client connecting with `last_seq=k` receives one `state` frame, then exactly events `seq > k` in order, then live events.
- **Pending buffer:** a `send_message` delivered with no socket is applied on connect, in order, before live processing.
- **Concurrent message not dropped:** a `send_message` arriving while `RUNNING`/`FINISHED` yields a `USER` `MessageEvent` the next reconstruction reflects (BoD §12.3 at the data layer).
- **Frame validation:** malformed client frames are rejected with a transport `error` frame, never crash the socket.

---

## 9. What this contract hands to each downstream subsystem

An index so each subsequent subsystem knows exactly what it may rely on from here.

- **Agent loop (§12):** the `Event` types, `ConversationState` + `reconstruct()`, `View.of()`, `event_content_eq()` (stuck detection), the `Condenser`/`Summarizer` protocols, the `ConversationStatus` machine. The loop appends `ActionEvent`/`StatusEvent`/`ErrorEvent` and reads `View.messages`.
- **Tool subsystem (§10):** `ToolCall` (consumes), `ToolResult`/`ObservationEvent`/`AgentErrorEvent` (produces), correlated by `call_id`/`action_id`.
- **Sandbox (§11):** no direct event types; tool executors run there; results return as `ToolResult`.
- **Retrieval/grounding (§9/§14):** search/extract tools produce `ObservationEvent`s; indexing runs as a §7.6 callback.
- **Security (§17):** reads `ActionEvent.self_assessed_risk`; the gate uses `StatusEvent(WAITING_FOR_CONFIRMATION)` + `State.pending_action_id`; the audit trail is a `get_events()` query.
- **LLM router (§15):** implements `Summarizer.summarize()` (cheap model) and `is_context_window_exceeded()`; maps `LLMMessage` ↔ provider formats. **This is the next contract to pin** — the loop and condenser both bind to it.
- **UI (§13):** binds to the §7 wire contract — `WSServerFrame`/`WSClientFrame`, the REST surface — and renders raw events by `kind`/`source`.
- **App-server (§5):** uses `EventStore.list_conversations(owner_id=…)`, the create/proxy endpoints, and the ownership guarantee (§6.1).

---

## 10. Build checklist (Phase 0 slice of this subsystem)

For the walking skeleton (BoD Phase 0), implement in this order:
1. `BaseEvent` + concrete event types + the `Event` union (§2).
2. `migrate_event` (no-op at v1) + serialization round-trip tests (§4, §8.5).
3. SQLite `EventStore`: `append`/`get_events`/`paginate`/`get_state` + invariant tests G1/G4 (§6, §8.1).
4. `ConversationState.reconstruct` + tests (§3, §8.2).
5. `View.of` + a no-op condenser (real one deferred to Phase 1) + view tests (§5, §8.3).
6. `event_content_eq` + tests (§6.3, §8.4) — needed by Phase 1's stuck detector, cheap to land now.
7. WebSocket endpoint with `event`/`state` frames + reconnect/replay + pending buffer + tests (§7, §8.6).
8. REST history/state/create endpoints (§7.5).

Deferred to Phase 1 (built on this skeleton): the real `LLMSummarizingCondenser`, the context-window classifier, and the loop that drives status transitions.

---

### Appendix — open items specific to this contract
- **[OPEN] Token-frame granularity.** Per-token vs. per-chunk streaming is an [INTERIOR] UI/perf choice; the contract only requires reconciliation to the final event. Tune in Phase 2.
- **[OPEN] Subscribe fan-out at scale.** In-process pub/sub is fine single-node; a cross-node bus is the multi-node door (BoD §5.2), not needed for v1.
- **[VERIFY] Pydantic v2 + FastAPI versions** at build; the discriminated-union and `model_dump(mode="json")` behavior assumed here is stable in current v2 but pin it.
- **Next contract to write:** the **LLM router boundary** (§9 index) — the seam the loop and condenser both depend on, and the natural second document.
