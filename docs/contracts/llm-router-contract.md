# Technical Design Contract — LLM Router Boundary

**Document type:** Detailed Technical Design (Contract Spec)
**Subsystem:** LLM Router & Model Boundary — BoD §15 (with §12.6, §10-principle "prompt is part of the model abstraction")
**Status:** v1.3 — authoritative contract (deterministic selection + reactive error surfacing; intelligent routing dormant)

> **v1.3 changelog (reactive error surfacing).** The v1.2 *pre-call* capability check is **removed**. The router no longer inspects `entry.capabilities` to predict whether the assigned model can handle a request — it **sends the request as assigned**. If the model can't do what was asked, the **provider** says so, and that error is surfaced with its **real content intact** (not swallowed, not flattened to a generic "model call failed"). It is mapped to the typed hierarchy (§6) where it fits, but the real reason is preserved in what reaches the user. The only pre-call check that remains is **config integrity** — that the assignment resolves to a model present in `models` (raises `NoEligibleModel`/`"unknown_assignment"`); that is not a capability judgement. Amended: principle 8 (§1), §4.1 (step 2 drops `capability_mismatch`; step 5 reactive), §10.1 (capability fail-loud → reactive surfacing). The agent loop now catches non-context-window `LLMError` from the driver step and emits a terminal `ErrorEvent` whose `detail` carries the provider's real message (the agent server streams it to the UI) — see agent-loop-contract §4.
>
> **v1.2 changelog (the "lobotomy").** Model selection is now **deterministic and config-driven**: a role resolves to exactly one model by direct lookup — `default_model` for `AGENT_DRIVER`, an explicit `assignments[role]` for every other role — optionally overridden per conversation by the **model pill** (`CallContext.model_override`, driver-only). The intelligent routing layer — the `OverflowPolicy`/`ThresholdOverflowPolicy` five rules, difficulty/capability-based escalation, and local-failure/stuck/low-confidence escalation — is **dormant**: removed from the live path but preserved *commented out* in `policy.py`, `routing.py`, and `config.py` (and `test_router_overflow.py`, skipped), so revival is a diff. Amended below: principles 1 & 3 (§1), the router intro and **RT2** (§4), the resolution algorithm (§4.1), the whole overflow policy (§5, now marked DORMANT), cost governance (§5.2, now a *passive* spend backstop), escalation (§5.3, escalation dormant; same-model retry retained), the config shape (§7, `assignments`), and the routing/overflow test plan (§10.1–10.2). `RoutingDecision.path` gains `"pinned"`/`"manual"`. The dormant design + revival path is in the new **Appendix A**. Capabilities are now **advisory + fail-loud** (they inform assignment and raise on mis-assignment) rather than deciding. Rationale: reduce "model behind the curtain" surprise — the operator assigns models explicitly and sees exactly what runs.
>
> **v1.1 changelog.** §9.1's previously hand-waved sync/async summarizer seam is now pinned: the `Summarizer`/`Condenser`/`complete` path is async end-to-end (aligned with event contract v1.2). No other changes.
**Depends on:** the Event & State contract (`event-state-contract.md`) — specifically `LLMMessage`, the `Summarizer` protocol, and `is_context_window_exceeded()`, all of which this document now *implements*.
**Consumed by:** the agent loop (§12), the memory condenser (§7.3/§5 of the event contract), the citation/grounding verifier (§14), and anything that talks to a model.
**Decision in force:** OpenRouter is wired from the start (not stubbed) and remains an *assignable* provider. As of v1.2 the overflow policy is **dormant** (see changelog + §5 + Appendix A): selection is deterministic config assignment, not policy-driven.

---

## 0. What this document is (and is not)

**Is:** the binding contract for how the system talks to language models — the provider-neutral request/response shapes, the router that selects a model by **deterministic config assignment** (role → assigned model; not by name through the request, and as of v1.2 not by capability/policy either — that is dormant, §5/Appendix A), the per-model/per-mode prompt selection, and the two specific functions the event contract is waiting on (`Summarizer.summarize`, `is_context_window_exceeded`). Implementable from this doc with no further architectural decisions; bindable by other subsystems without guessing.

**Is not:** the agent loop, the prompts' actual prose, or provider SDK internals. It defines the *boundary* — what callers hand in, what they get back, how routing decides — not the loop's logic or the wording of any prompt. Interior choices below the contract line (which HTTP client, retry backoff curves, caching mechanics) are the builder's.

**Conventions** (identical to the event contract):
- Illustrative Python 3.12 + Pydantic v2. Field names, types, signatures are **normative**; bodies illustrative.
- **[CONTRACT]** = a guarantee callers may rely on. **[INTERIOR]** = builder's free choice. **[VERIFY]** = fast-moving fact to confirm at build (model names, prices, provider quirks).
- All model names, context sizes, and prices below are **[VERIFY]** — they are the canonical "re-check at build" fields. The *design* does not depend on them; they live in config (§7).

---

## 1. Foundational principles [CONTRACT]

1. **Role-based, not name-based** (BoD §15.1) — *deterministic assignment in v1.2.* Callers ask for a *capability profile* (a role + difficulty + requirements), never a model name. The router maps **role → concrete model by direct config lookup** (`assignments`/`default_model`), optionally overridden per conversation by the model pill. Swapping or upgrading a model is a config edit; no caller changes. (v1.1 mapped profile→model *intelligently* via capabilities + policy; that is now dormant — capabilities are advisory, see principle 8.)
2. **Provider-neutral at the boundary.** Callers speak `LLMMessage` / `CompletionRequest` / `CompletionResponse` only. Provider-specific shaping (Anthropic vs OpenAI-style tool calls, OpenRouter passthrough) lives entirely inside provider adapters.
3. **Operator-assigned, not policy-routed** (v1.2, was "local-first, overflow by policy"). The model that runs each role is whatever the operator assigned — `default_model`/`assignments`, or the per-conversation pill. There is no automatic local→overflow escalation. OpenRouter/frontier models are reached only by *explicitly assigning* them to a role. (The dormant policy that escalated on low confidence / repeated errors / hard steps is preserved for revival, §5 + Appendix A.)
4. **Every routing/overflow decision is observable.** The router emits a structured `RoutingDecision` record for each call (which profile, which model, local vs overflow, why) for the metrics/audit layer (BoD §18/§20). Routing is never a silent black box.
5. **The prompt is part of the model abstraction** (BoD §12.6). The router (or a prompt provider it owns) selects the system prompt by **model family × operating mode**. A model swap pulls its matching prompt variant automatically.
6. **Deterministic where it must be.** Tool-call and structured-output generation default to temperature 0 (BoD §10.3). Determinism settings are part of the request, not global state.
7. **Failures are typed and classifiable.** Provider errors map to a small typed hierarchy (§6), including the context-window-exceeded case the condenser depends on. Callers never parse raw provider error strings.
8. **Capabilities are advisory metadata; errors are surfaced reactively** (v1.3, was "advisory + fail-loud"). `ModelEntry.capabilities` and `context_window` are metadata that *inform* the operator's assignment (surfaced in the Settings matrix). The router does **not** inspect them before a call to predict what the model can or can't do — there is no pre-call capability gate. It sends the request as assigned; if the model can't do what was asked, the **provider** returns an error and that error is surfaced to the user with its **real content intact** (mapped to a typed error per §6 where it fits, but never flattened). You assign; the provider — not a predictive check — is the source of truth on what the model can actually do.

---

## 2. Core request/response types [CONTRACT]

These extend the event contract's `LLMMessage` (which stays the wire-level message shape). The router adds the request/response envelope around it.

```python
from __future__ import annotations
from enum import Enum
from typing import Any, Literal, Protocol, AsyncIterator
from pydantic import BaseModel, Field, ConfigDict
# LLMMessage is imported from the event/state contract — NOT redefined here.
# from core.events import LLMMessage

# ---- capability vocabulary ---------------------------------------------------

class ModelRole(str, Enum):
    """The four roles from BoD §15.2, plus the summarizer (§7.3). A caller
    declares the ROLE it needs; the router maps role -> model via config."""
    AGENT_DRIVER = "agent_driver"      # the loop's tool-calling/planning brain
    RAG_ANSWERER = "rag_answerer"      # grounded answer synthesis
    QUERY_REWRITER = "query_rewriter"  # query expansion/decomposition
    SUMMARIZER = "summarizer"          # condensation (cheap, separate model)
    NLI_VERIFIER = "nli_verifier"      # citation entailment (a cross-encoder, see §9)

class Difficulty(str, Enum):
    """A caller's hint about how hard THIS call is. Feeds the overflow policy
    (§5) — e.g., a HARD agent_driver step is an overflow candidate while a
    ROUTINE one stays local."""
    ROUTINE = "routine"
    HARD = "hard"

class Requirement(str, Enum):
    """Hard capability requirements that constrain model selection."""
    VISION = "vision"                  # must accept images
    LONG_CONTEXT = "long_context"      # must handle large inputs (>~64k)
    TOOL_CALLING = "tool_calling"      # must support structured tool calls
    JSON_MODE = "json_mode"            # must support constrained/JSON output

class OperatingMode(str, Enum):
    """Drives prompt-variant selection (§8). Mirrors the surfaces in BoD §8."""
    INTERACTIVE = "interactive"
    PLANNING = "planning"
    LONG_HORIZON = "long_horizon"

class CapabilityProfile(BaseModel):
    """[CONTRACT] What a caller asks for. Never a model name."""
    model_config = ConfigDict(frozen=True)
    role: ModelRole
    difficulty: Difficulty = Difficulty.ROUTINE
    requirements: frozenset[Requirement] = frozenset()
    mode: OperatingMode | None = None   # informs prompt selection if set

# ---- tool specs (the loop hands these in; provider adapters shape them) ------

class ToolSpec(BaseModel):
    """Provider-neutral description of a callable tool, as the model sees it.
    The agent loop builds these from the tool registry (tool contract, separate
    doc); adapters translate to each provider's tool/function schema."""
    model_config = ConfigDict(frozen=True)
    name: str
    description: str
    parameters_schema: dict[str, Any]   # JSON Schema for arguments

# ---- the request -------------------------------------------------------------

class CompletionRequest(BaseModel):
    """[CONTRACT] The provider-neutral request. Callers build this; the router
    routes it; adapters execute it."""
    model_config = ConfigDict(frozen=True)
    profile: CapabilityProfile
    messages: list["LLMMessage"]
    tools: list[ToolSpec] | None = None
    # Generation controls. temperature defaults to 0 for determinism where the
    # role implies structured output; callers may override.
    temperature: float = 0.0
    max_tokens: int | None = None
    # Force structured/JSON output (maps to provider json/grammar modes).
    response_format: Literal["text", "json"] = "text"
    # Opaque per-call correlation id, surfaced back on the response and in
    # ActionEvent.llm_response_id (event contract). VOLATILE.
    request_id: str | None = None
    # If True, caller wants token streaming (router returns an async iterator
    # via stream_complete, §3.2). Default False.
    stream: bool = False

# ---- the response ------------------------------------------------------------

class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)
    input_tokens: int
    output_tokens: int
    # Cost in USD if known (overflow/OpenRouter reports it; local = 0.0).
    cost_usd: float = 0.0

class ProposedToolCall(BaseModel):
    """A tool call the model wants to make. The loop converts this into the
    event contract's ToolCall/ActionEvent. Provider-neutral."""
    model_config = ConfigDict(frozen=True)
    tool_name: str
    arguments: dict[str, Any]
    provider_call_id: str | None = None   # VOLATILE

class CompletionResponse(BaseModel):
    """[CONTRACT] What every completion returns, regardless of provider or
    local/overflow path. The loop reads .text / .tool_calls; the condenser
    reads .text; observability reads .usage and .routing."""
    model_config = ConfigDict(frozen=True)
    text: str = ""                          # assistant text / thought
    tool_calls: list[ProposedToolCall] = Field(default_factory=list)
    usage: TokenUsage
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter", "error"]
    model_used: str                         # concrete model id actually used [VERIFY-valued]
    request_id: str | None = None
    # Populated by the router for observability (§4). [CONTRACT] always present.
    routing: "RoutingDecision"
```

**[CONTRACT] notes other subsystems rely on:**
- The loop builds a `CompletionRequest(profile=CapabilityProfile(role=AGENT_DRIVER, ...), messages=view.messages, tools=...)` and reads `response.tool_calls` → maps to the event contract's `ToolCall`/`ActionEvent`, carrying `response.request_id` into `ActionEvent.llm_response_id`.
- The condenser's `Summarizer` (event contract) is implemented as a router call with `role=SUMMARIZER` (§9.1).
- `CompletionResponse.routing` and `.usage` are always populated — the metrics layer never has to reconstruct them.

---

## 3. The provider adapter boundary [CONTRACT]

A `ModelProvider` executes a `CompletionRequest` against one backend. This is where all provider-specific shaping lives; everything above it is neutral.

```python
class ModelProvider(Protocol):
    """[CONTRACT] One backend (a local server, or OpenRouter). Translates the
    neutral request to the provider's API and the provider's response back to
    CompletionResponse. Raises the typed errors in §6."""
    name: str                                   # "ollama" | "llamacpp" | "openrouter"

    async def complete(self, req: CompletionRequest, *, model: str) -> CompletionResponse: ...
    async def stream_complete(self, req: CompletionRequest, *, model: str) \
        -> AsyncIterator["StreamChunk"]: ...
    def supports(self, requirement: Requirement, *, model: str) -> bool: ...

class StreamChunk(BaseModel):
    """[CONTRACT] An incremental piece of a streamed completion. The agent
    server maps these to WSServerFrame(type='token') (event contract §7.2).
    The terminal chunk carries the assembled CompletionResponse."""
    model_config = ConfigDict(frozen=True)
    delta_text: str = ""
    done: bool = False
    final: CompletionResponse | None = None     # present iff done is True
```

### 3.1 v1 providers [INTERIOR implementations, CONTRACT boundary]
- **`ollama` / `llamacpp`** — the local path. Talk to the local model server over its HTTP API. [INTERIOR] which of the two (or both) is config; both satisfy `ModelProvider`. Local calls report `cost_usd=0.0`.
- **`openrouter`** — the overflow path. OpenRouter exposes an OpenAI-compatible API, so the adapter is an OpenAI-style client pointed at the OpenRouter base URL with the key from the secrets store (never in current/prompts/sandbox — event/security contracts). [VERIFY] base URL, auth header, and the model-id strings at build. OpenRouter returns usage/cost; the adapter populates `TokenUsage.cost_usd` from it.

### 3.2 Streaming [CONTRACT]
- If `req.stream` is True, callers use `stream_complete`; the agent server forwards `StreamChunk.delta_text` as `token` frames and reconciles to `final` (the source-of-truth event is still the `ActionEvent`/`MessageEvent` the loop appends — tokens are ephemeral, per event contract §7.2).
- Local and OpenRouter adapters both implement streaming. [INTERIOR] chunk sizing.

---

## 4. The Router [CONTRACT]

The router is the single entry point callers use. **v1.2:** it owns role→model resolution (a *direct config lookup* — no policy), prompt-variant injection, provider execution with a bounded *same-model* transient retry, passive cost observation, and emitting the `RoutingDecision`. It no longer owns the overflow decision or model escalation (dormant, §5 + Appendix A).

```python
class RoutingDecision(BaseModel):
    """[CONTRACT] Emitted for every call; attached to CompletionResponse and
    logged for observability/audit (BoD §18/§20)."""
    model_config = ConfigDict(frozen=True)
    profile: CapabilityProfile
    chosen_model: str                  # concrete id [VERIFY-valued]
    provider: str                      # "ollama"|"llamacpp"|"openrouter"
    # v1.2: "pinned" = settings assignment, "manual" = per-conversation pill.
    # "local"/"overflow" retained for the DORMANT revival path (Appendix A).
    path: Literal["local", "overflow", "pinned", "manual"]
    reason: str                        # human-readable: "config" in v1.2
    overflow_triggers: list[str] = Field(default_factory=list)  # DORMANT (was: rules fired)
    attempt: int = 1                   # >1 only if a same-model transient retry occurred

class LLMRouter(Protocol):
    """[CONTRACT] The single entry point for all model calls.

    Guarantees:
      RT1 complete() always returns a CompletionResponse with .routing and
          .usage populated, or raises a typed LLMError (§6).
      RT2 (v1.2 amended) The model is selected by DETERMINISTIC config
          assignment: assignments[role] / default_model, optionally overridden
          per conversation by CallContext.model_override (the model pill,
          AGENT_DRIVER only). Selection is NOT capability- or policy-derived.
          Callers still cannot pin a model THROUGH THE REQUEST (no name field on
          CompletionRequest); the pill is an explicit operator surface on the
          out-of-band CallContext, not a caller-chosen name.
      RT3 The matching prompt variant (model family x mode) is injected when
          the caller did not already supply a system message (§8).
      RT4 Every call emits exactly one RoutingDecision (success or terminal
          failure) to the observability sink; path is "pinned" or "manual".
    """
    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...
    async def stream_complete(self, req: CompletionRequest) -> AsyncIterator[StreamChunk]: ...
```

### 4.1 Resolution algorithm [CONTRACT semantics; INTERIOR code] — v1.2 deterministic
For a request, the router, in order:
1. **Resolve the assigned model** by direct lookup: `model_for(role, override) = override ?? assignments[role] ?? default_model`, where `override` is `CallContext.model_override` and applies to `AGENT_DRIVER` only. Path = `"manual"` if an override was used, else `"pinned"`. No policy, no candidate set, no difficulty.
2. **Config-integrity check only** [CONTRACT]: if the resolved key is not in `models`, raise `NoEligibleModel` (trigger `"unknown_assignment"`) and emit one terminal `RoutingDecision` (RT4). This is a misconfiguration check, **not** a capability judgement — v1.3 does **no** pre-call capability inspection (principle 8). The router never silently substitutes a different model.
3. **Passive budget backstop** (§5.2): if a *hard* cap is already exceeded, raise `BudgetExceeded` before spending. This is a spend guard, not a model choice.
4. **Inject the prompt variant** (§8) if needed (RT3).
5. **Execute** via the chosen provider — sending the request *as assigned*, with no capability prediction. On a transient error, retry the *same* assigned model up to the attempt cap (no escalation). Terminal errors (context-window, auth, content-filter, or any provider rejection) **propagate to the caller with their real content intact** (§6) — reactive surfacing, never swallowed or flattened. `attempt` records same-model retries.
6. **Emit** the `RoutingDecision` (`path` `"pinned"`/`"manual"`, `reason` `"config"`); attach to the response.

---

## 5. The overflow policy — **DORMANT as of v1.2**

> **DORMANT (v1.2).** Everything in §5.1 (the five rules) and the escalation half of §5.3 is **commented out** in the implementation (`policy.py` / `routing.py`) and is *not* part of the live router. `OverflowSignal` stays live as **advisory metadata** (the loop still builds it; nothing routes on it). Cost governance (§5.2) survives as a *passive* spend backstop. This section is retained as the design of record for the revival path — see **Appendix A** for the exact revival steps. The text below describes the dormant behavior in the present tense for fidelity; read it as "what would happen if revived."

This was the live, tested decision layer in v1.0/v1.1. It is a pluggable policy so the *rule set* is swappable, but the interface and the default rules were contractual.

```python
class OverflowSignal(BaseModel):
    """Runtime inputs the policy may use beyond the static profile."""
    model_config = ConfigDict(frozen=True)
    difficulty: Difficulty
    consecutive_tool_errors: int = 0    # from the loop (stuck-adjacent)
    last_local_confidence: float | None = None  # if the caller estimates one
    local_attempts_failed: int = 0      # local calls already failed this step
    requires: frozenset[Requirement] = frozenset()

class OverflowPolicy(Protocol):
    """[CONTRACT] Decides local vs overflow and may request escalation.
    Returns (path, triggers)."""
    def decide(self, profile: CapabilityProfile, signal: OverflowSignal,
               *, config: "RouterConfig") -> tuple[Literal["local","overflow"], list[str]]: ...
```

### 5.1 Default policy rules [CONTRACT] (tunable thresholds live in config)
The default `ThresholdOverflowPolicy` routes to **overflow** if ANY fires, else **local**:
1. **Capability gap:** a required capability has no local model that `supports()` it, but an overflow model does → overflow (trigger `"capability_gap"`).
2. **Hard step on a routing-eligible role:** `difficulty == HARD` and the role is overflow-eligible in config (by default `AGENT_DRIVER` planning/synthesis and `NLI_VERIFIER` high-precision) → overflow (`"hard_step"`).
3. **Local failure escalation:** `local_attempts_failed >= config.local_retry_before_overflow` → overflow (`"local_failed"`).
4. **Stuck recovery:** `consecutive_tool_errors >= config.errors_before_overflow` → overflow (`"stuck_recovery"`) — this is the loop handing a struggling step to a stronger model.
5. **Low confidence:** `last_local_confidence is not None and < config.confidence_floor` → overflow (`"low_confidence"`).
Otherwise → local (trigger list empty).

**[CONTRACT]:** roles `RAG_ANSWERER`, `QUERY_REWRITER`, `SUMMARIZER` default to **local-only** (never overflow) unless config opts them in — they are the routine, high-volume, cost-sensitive calls (BoD §15.3). `NLI_VERIFIER` overflow means "use a frontier judge for a sampled subset" (§9.2), not the live path.

### 5.2 Cost governance [CONTRACT] — **passive in v1.2**
- The router still maintains a per-conversation and global **running cost** (sum of `usage.cost_usd`); this stays live for observability. Config carries soft/hard budget caps.
- **v1.2 behavior:** cost no longer influences *which model is chosen* (selection is `assignments`-driven). The **hard cap** survives as a *passive spend backstop*: if accrued spend has reached a hard cap, the router raises `BudgetExceeded` before spending again — a refusal to spend, not a routing choice. Local (price-0) work never accrues, so this only ever bites a deliberately-assigned paid model. The **soft cap** is observe-only (there is no discretionary overflow left to suppress).
- *(Dormant)* v1.1 used the soft cap to suppress discretionary overflow (rules 2/5) and the hard cap to disable overflow while still serving locally; that interplay lives in Appendix A.
- This still backs BoD §15.4 / R9 (cost runaway) and pairs with the plan-preview gate (which cuts spend upstream).

### 5.3 Escalation vs retry [CONTRACT] — retry kept, escalation DORMANT
- A **retry** re-runs the *same* assigned model on a transient error (§6) with backoff, up to the attempt cap. **This is kept in v1.2** — it is resilience, not model-switching — and is the reason a single transient blip doesn't propagate out of the loop. Each retry increments `RoutingDecision.attempt`. [INTERIOR] backoff curve.
- *(Dormant)* An **escalation** — a routing change (local→overflow, or up an overflow ladder) after `local_retry_before_overflow` local failures — is **removed from the live path** and preserved commented in `routing.py` (Appendix A). The `max_iterations` ceiling in the loop (event contract) remains the ultimate backstop.

---

## 6. Typed errors & context-window classification [CONTRACT]

Callers never parse raw provider strings (principle 7). Providers raise this hierarchy; the router catches, retries/escalates, and (for terminal failures) raises to the caller.

```python
class LLMError(Exception):
    """Base. Carries the provider and model for diagnostics."""
    provider: str
    model: str

class LLMTransientError(LLMError):
    """Retryable: timeouts, 429 rate limits, 5xx, transient network."""
    retry_after_s: float | None = None

class LLMContextWindowExceeded(LLMError):
    """The input exceeded the model's context window. THIS is the case the
    condenser's hard-reset path depends on (event contract §5.3)."""

class LLMAuthError(LLMError):           # bad/missing key — terminal
    ...
class LLMContentFiltered(LLMError):     # provider refused on policy grounds
    ...
class NoEligibleModel(LLMError):        # no model satisfies requirements (§4.1)
    ...
class BudgetExceeded(LLMError):         # hard cost cap hit (§5.2)
    ...
```

### 6.1 The function the event contract is waiting on [CONTRACT]
The event/state contract declared `is_context_window_exceeded(err) -> bool` and left it for this subsystem. It is implemented here as classification owned by the provider adapters:

```python
def is_context_window_exceeded(err: Exception) -> bool:
    """[CONTRACT] True iff err indicates context-window overflow. The condenser
    calls this to raise a HARD condensation request (event contract §5.2/§5.3).

    Implementation: each ModelProvider adapter classifies its own provider's
    error shapes and raises LLMContextWindowExceeded; this function is then
    simply `isinstance(err, LLMContextWindowExceeded)`. Per-provider error-shape
    detection is enumerated inside the adapters and is the [VERIFY]/maintenance
    surface BoD §7.3 warns about (provider error formats drift)."""
    return isinstance(err, LLMContextWindowExceeded)
```

**[CONTRACT]:** the burden of recognizing each provider's context-window error lives in that provider's adapter (where the raw error is seen), not scattered across callers. Adding a provider = adding its error classification there.

---

## 7. Configuration shape [CONTRACT]

All `[VERIFY]` specifics (model names, context sizes, prices, thresholds) live here — one config surface (BoD §19), editable without touching callers. This is the single file you revise when models or prices change (R10).

```python
class ModelEntry(BaseModel):
    model_id: str                       # provider's id string [VERIFY]
    provider: str                       # "ollama"|"llamacpp"|"openrouter"
    context_window: int                 # [VERIFY]
    capabilities: frozenset[Requirement]
    # local quantization note for provenance/ops (BoD §15.2); informational.
    quantization: str | None = None     # e.g. "Q4_K_M"
    # per-million-token prices for cost accounting; 0 for local. [VERIFY]
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0

class RouterConfig(BaseModel):                    # v1.2 deterministic
    models: dict[str, ModelEntry]                 # key -> entry (the assignable catalogue)
    default_model: str                            # AGENT_DRIVER + fallback for any unassigned role
    assignments: dict[ModelRole, str] = Field(default_factory=dict)  # explicit per-role (source of truth)
    # cost governance (§5.2) — PASSIVE spend backstop; does not influence selection
    soft_budget_usd_per_conversation: float | None = None
    hard_budget_usd_per_conversation: float | None = None
    soft_budget_usd_global_daily: float | None = None
    hard_budget_usd_global_daily: float | None = None

    def model_for(self, role: ModelRole, *, override: str | None = None) -> str:
        # precedence: per-conversation pill (driver) > settings assignment > default
        return override if override is not None else self.assignments.get(role, self.default_model)

# DORMANT (v1.2, Appendix A): RoleRouting (primary/overflow/eligibility/ladder) and
# the threshold fields (local_retry_before_overflow, errors_before_overflow,
# confidence_floor) are retained commented in config.py for the revival path.
```

**[CONTRACT] starting assignments** (values are [VERIFY] at build; the *shape* is fixed). Every role maps to exactly one model:
- `default_model` = a local 70B/35B-class tool-calling model (Q4_K_M+) — this is `AGENT_DRIVER` and the fallback for any unassigned role.
- `assignments[RAG_ANSWERER]` = local 8–14B.
- `assignments[QUERY_REWRITER]` = local 7–8B.
- `assignments[SUMMARIZER]` = a cheap/small local model.
- `assignments[NLI_VERIFIER]` = the local cross-encoder (§9).
- A frontier/OpenRouter model is included in `models` as an *assignable* entry; nothing routes to it unless the operator assigns it to a role (Settings matrix) or selects it for a conversation (model pill). The UI surfaces for these assignments are: the **Settings model-assignment matrix** (default + per-role selectors, showing each model's capabilities + cost) and the **main-screen model pill** (per-conversation driver override). Backend hooks exist now (`assignments` / `CallContext.model_override` via `RouterAgent(model_override=...)`); the React surfaces are specified separately.

---

## 8. Prompt-variant selection [CONTRACT] (the prompt is part of the abstraction)

Per BoD §12.6 / principle 5, the router owns selecting the **system prompt** by **model family × operating mode**. It does not own the prompt *prose* (that's content, versioned in the repo), only the selection contract.

```python
class PromptProvider(Protocol):
    """[CONTRACT] Returns the system prompt for a given model family and mode.
    Owned alongside the router. Prompt text lives in versioned template files;
    this only resolves WHICH one."""
    def system_prompt(self, *, model_family: str, mode: OperatingMode | None,
                      role: ModelRole) -> str: ...
```

**[CONTRACT] behavior (RT3):**
- The router derives `model_family` from the chosen `ModelEntry` (e.g., "anthropic" / "llama" / "qwen" / "gpt"). [VERIFY] the family tags at build.
- If the caller's `messages` do **not** already start with a system message, the router prepends `PromptProvider.system_prompt(...)` for the resolved family + the request's `mode` (defaulting per role: AGENT_DRIVER→LONG_HORIZON or PLANNING, RAG_ANSWERER→INTERACTIVE).
- If the caller supplied its own system message, the router does not override it (callers may opt out).
- [INTERIOR] the template engine and file layout (mirrors the OpenHands `current/prompts/` structure: a base per mode, plus `model_specific/<family>` overlays).

---

## 9. Implementing the event contract's `Summarizer`, and the NLI verifier

### 9.1 Summarizer [CONTRACT]
The event contract's `Summarizer.summarize(messages) -> str` is implemented as a router call:

```python
class RouterSummarizer:
    """[CONTRACT] Satisfies the event contract's Summarizer protocol by routing
    a SUMMARIZER-role completion. Uses the cheap local model (never the agent
    model), per BoD §7.3."""
    def __init__(self, router: LLMRouter): self._router = router
    async def summarize(self, messages: list["LLMMessage"]) -> str:
        req = CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.SUMMARIZER),
            messages=[*messages,
                      LLMMessage(role="user", content="Summarize the above concisely, preserving facts, decisions, and open threads.")],
            temperature=0.0,
        )
        return (await self._router.complete(req)).text
```
**[CONTRACT] the async seam (resolved; was the Phase 1 audit's open ambiguity).** `RouterSummarizer.summarize` is `async` and the event contract (v1.2) makes both `Summarizer.summarize` and `Condenser.condense` `async` to match. The end-to-end path is fully async: the agent loop's `_materialize_view` (agent-loop contract §8) `await`s `condenser.condense(...)`, which `await`s `summarizer.summarize(...)`, which `await`s `router.complete(...)`. There is no sync→async boundary anywhere on this path, so it composes cleanly inside the loop's running event loop — no `asyncio.run()`-inside-a-loop, no thread offload, no deadlock. (Earlier drafts left this as "[INTERIOR] wiring"; it is now pinned because a sync `summarize`/`condense` could not drive the async `complete` from within the loop.)

### 9.2 NLI verifier [CONTRACT boundary, distinct mechanism]
The `NLI_VERIFIER` role is special: per BoD §14/§15.2 it is **a ~300M cross-encoder, not an LLM**. It does not go through `complete()` (it is not a chat model). It is exposed as its own narrow interface, registered as a role for *config and routing-eligibility symmetry* only:

```python
class NLIVerifier(Protocol):
    """[CONTRACT] 3-way entailment for citation grounding (BoD §14). Premise =
    retrieved passage; hypothesis = a claim. Local cross-encoder by default."""
    def entail(self, premise: str, hypothesis: str) -> Literal["entail","neutral","contradict"]: ...
    def score(self, premise: str, hypothesis: str) -> float: ...  # entailment prob
```
**[CONTRACT]:** the grounding subsystem (§14, separate doc) consumes `NLIVerifier`, not the router's `complete()`. The router's only relationship to it is that "send a sampled subset to a frontier LLM judge for eval" (§5.1) is an observability/eval path, not the live verification path.

---

## 10. Test plan [CONTRACT — defines correctness]

Headless; no real models required (providers are faked). The subsystem is done when these pass.

### 10.1 Routing (v1.2 deterministic)
- **No name leak through the request:** `CompletionRequest` has no model-name field; `complete()`'s signature is `{self, req, context}`. The per-conversation pill is the explicit `CallContext.model_override`, not a request field.
- **Deterministic assignment:** a role resolves to its assigned model; **difficulty changes nothing** (identical role + different difficulty → same model). Each role resolves to `assignments[role]`/`default_model`.
- **Model pill (manual override):** `CallContext.model_override` selects the driver model → `path == "manual"`, and it overrides the settings default; it affects `AGENT_DRIVER` only (other roles keep their settings assignment).
- **Reactive surfacing (no pre-call capability check):** a VISION request against a non-vision *assignment* is **sent** (the provider, not a predictive check, decides) — `complete()` does not raise `NoEligibleModel` before the call. If the provider rejects it, that error propagates with its **real content intact** (asserted: `str(provider_error)` reaches the caller). The only pre-call raise is config integrity — an assignment to a model absent from `models` raises `NoEligibleModel` (trigger `"unknown_assignment"`).
- **Loop surfaces it to the UI:** the agent loop catches a non-context-window `LLMError` from the driver step and emits a terminal `ErrorEvent` whose `detail` carries the provider's real message (+ typed class); status → `ERROR`. (Acceptance test in `test_loop_integration.py`.)
- **RoutingDecision always emitted:** every `complete()` (success or terminal failure) produces exactly one `RoutingDecision` to the sink; success `path` is `"pinned"`/`"manual"`, `reason` `"config"` (RT4).

### 10.2 Overflow policy — **DORMANT (skipped)**
- The five-rule policy / local-only roles / stuck recovery / cost-driven suppression / escalation tests live in `test_router_overflow.py`, **skipped at module level** with a dormant note. They are the revival harness: un-skip + restore the dormant code (Appendix A) and they should pass again.
- What v1.2 *does* test for cost: the **passive hard-cap backstop** (over the cap + a paid pill-selected model → `BudgetExceeded`) and that **per-conversation cost still accrues** (observability), both in `test_router_extra.py`.

### 10.3 Errors & context window
- A faked provider raising its context-window error shape is classified as `LLMContextWindowExceeded`, and `is_context_window_exceeded()` returns True for it (the condenser's dependency — cross-tested against the event contract).
- Transient errors retry the *same* assigned model (per config) then raise (v1.2 — no escalation); auth errors are terminal (no retry); content-filter is terminal and typed.

### 10.4 Prompt selection
- When no system message is present, the correct family×mode prompt is prepended; when one is present, it is not overridden.
- Family derivation maps each configured model to the expected family tag.

### 10.5 Summarizer / streaming
- `RouterSummarizer.summarize()` routes a SUMMARIZER-role call and returns the faked model's text (cross-tested against the event contract's condenser, which must accept it).
- `stream_complete` yields deltas then a terminal chunk whose `final` equals the non-streamed `complete()` result for the same input.

### 10.6 Cross-contract integration
- A mini end-to-end (still no real model): build `view.messages` (event contract) → `CompletionRequest(AGENT_DRIVER)` → faked provider returns a tool call → assert it maps cleanly to the event contract's `ToolCall`/`ActionEvent`, with `request_id` carried into `llm_response_id`. This proves the two contracts compose.

---

## 11. What this contract hands to each downstream subsystem

- **Agent loop (§12):** `LLMRouter.complete()/stream_complete()`, `CompletionRequest`/`CompletionResponse`, `CapabilityProfile`, `ToolSpec`, `ProposedToolCall`, and (v1.2) `CallContext.model_override` for the per-conversation pill (`RouterAgent(model_override=...)`). The loop declares a role and reads tool calls back. The runtime signals it still threads (consecutive_tool_errors, etc.) are now **advisory metadata** carried on `OverflowSignal`/`CallContext`; nothing routes on them (the policy is dormant). The loop seam was unchanged by the lobotomy — it asks for a role and gets the assigned model.
- **Memory/condenser (event contract §5):** `RouterSummarizer` (satisfies `Summarizer`) and `is_context_window_exceeded()` — both promised there, delivered here.
- **Grounding/citation (§14):** `NLIVerifier` (local cross-encoder), distinct from `complete()`.
- **Agent server (§5/§7):** maps `StreamChunk` → `WSServerFrame(type="token")`.
- **Observability (§18) / cost (§21, R9):** consumes `RoutingDecision` + `TokenUsage` from every response; budget caps live in `RouterConfig`.
- **Security (§17):** the OpenRouter key comes from the secrets store, never current/prompts/sandbox; this contract assumes that boundary and does not duplicate it.
- **Config/ops (§19):** `RouterConfig` is the one surface to edit when models/prices/thresholds change (R10).

---

## 12. Build order (this subsystem)

1. Core types (§2) + the event contract's `LLMMessage` import wired.
2. `ModelProvider` protocol + the **OpenRouter adapter** and **one local adapter (Ollama)** (§3), with the typed error hierarchy + per-adapter context-window classification (§6).
3. `RouterConfig` loading (§7) with the starting role assignments ([VERIFY] values).
4. `LLMRouter` with deterministic resolution (§4.1) + `RoutingDecision` emission. *(v1.2: the `ThresholdOverflowPolicy` step is dormant — Appendix A.)*
5. `PromptProvider` + family×mode selection (§8) — base templates can be thin at first.
6. `RouterSummarizer` (§9.1) — unblocks the condenser; `NLIVerifier` stub/registration (§9.2) — full impl with the grounding subsystem.
7. The full §10 test suite (faked providers); the §10.6 cross-contract test is the acceptance gate.

**Sequencing with the build:** this is a Phase-1 subsystem (the loop needs it). It does **not** block Phase 0 (the walking skeleton's §10 checklist in the event contract doesn't call models). Pin it now; build it when Phase 1 starts.

---

### Appendix B — open items specific to this contract
- **[VERIFY] all model ids, context windows, prices, family tags, OpenRouter base URL/auth** at build. They live only in `RouterConfig` and the adapters, so updating them touches nothing else (R10).
- **[DORMANT] confidence estimation.** `last_local_confidence` fed dormant rule 5; moot until intelligent routing is revived (Appendix A).
- **[DORMANT] overflow ladder.** Multi-model escalation is part of the dormant policy (Appendix A); not in the live deterministic path.
- **[INTERIOR] caching.** Prompt/response caching (and prompt-cache-aware behavior the condenser cares about) is a provider-adapter interior; the contract only requires correct `usage` accounting.
- **Next contract to write:** the **agent loop**, which now has both of its dependencies pinned (state + router). After that: the **tool/sandbox boundary**.

---

## Appendix A — Dormant intelligent routing & the revival path (v1.2)

The v1.2 lobotomy **removed intelligent routing from the live path but did not delete it.** Everything below is preserved *commented out* in the code, clearly fenced with `# === INTELLIGENT ROUTING (DORMANT) … # === END DORMANT ===` banners, so reviving it is a diff rather than an archaeology dig.

### What is dormant (and where the commented code lives)
- **The overflow policy** — `OverflowPolicy` protocol + `ThresholdOverflowPolicy` (the five rules: `capability_gap`, `hard_step`, `local_failed`, `stuck_recovery`, `low_confidence`) + `DISCRETIONARY_TRIGGERS` + `_LOCAL_ONLY_ROLES`. In `policy.py` (commented; `OverflowSignal` stays live above the banner as advisory metadata).
- **Difficulty/capability-based escalation** as a routing decision — the v1.1 `_resolve` body that built an `OverflowSignal`, called the policy, defensively forced overflow on a capability gap, and applied cost governance to suppress/force overflow. In `routing.py` (commented, bottom of file).
- **Local-failure / stuck-recovery / low-confidence escalation** — the `complete()` retry loop's branch that promoted `local→overflow` after N local failures (honoring the hard cap). In `routing.py` (commented). The *same-model* transient retry is **kept live**.
- **Capability-based filtering as a router decision** — the "drop non-conforming models, pick the qualifying one" step. Replaced by the advisory **fail-loud** check (principle 8): the router raises on a mis-assignment instead of choosing a substitute.
- **Config scaffolding** — `RoleRouting` (primary/overflow/eligibility/ladder) and the threshold fields (`local_retry_before_overflow`, `errors_before_overflow`, `confidence_floor`). `RoleRouting` is retained as a live (but unused) class for revival; the threshold fields are commented on `RouterConfig`. In `config.py`.
- **The test harness** — `test_router_overflow.py` is skipped at module level with a dormant note; it is the regression suite for the revived policy.

### Why these stay live (the seams that did NOT change)
- `OverflowSignal` (the loop builds it; now advisory) — so `loop/agent.py` is unchanged.
- `CostTracker` + per-conversation/global accounting — observability, plus the passive hard-cap backstop (§5.2).
- `RoutingDecision` is still emitted on every call (RT4); routing stays observable. `path` just reads `"pinned"`/`"manual"` and `reason` `"config"`.
- `is_context_window_exceeded` + the typed error hierarchy — the loop's hard-reset depends on it.

### Revival steps (deterministic → intelligent)
1. **config.py** — uncomment the threshold fields + `roles: dict[ModelRole, RoleRouting]` on `RouterConfig`; populate `roles` in `default_config()` (the old shape is in git / the dormant block).
2. **policy.py** — uncomment `OverflowPolicy` + `ThresholdOverflowPolicy` + `DISCRETIONARY_TRIGGERS` + `_LOCAL_ONLY_ROLES` and their imports.
3. **routing.py** — re-add the `policy` constructor param + the `OverflowSignal`/`DISCRETIONARY_TRIGGERS`/`policy` imports and `_cap_state`; swap the deterministic `_resolve`/`complete` bodies for the dormant intelligent ones at the bottom of the file.
4. **`__init__.py`** — re-export `OverflowPolicy` + `ThresholdOverflowPolicy`.
5. **tests** — un-skip `test_router_overflow.py`; reconcile `test_router_routing.py`/`test_router_extra.py` (the v1.2 determinism + fail-loud tests assume no escalation).
6. Decide the **coexistence model**: most likely the deterministic assignment becomes the *default* and the policy an *opt-in per role* (e.g. an `assignments` entry of a sentinel that means "let the policy decide"), so the operator keeps explicit control while re-enabling escalation only where wanted. This was the open design question the lobotomy parked.

### Seams that assumed routing intelligence (reported, all handled)
- `loop/agent.py` (`RouterAgent`) built `CapabilityProfile(difficulty=…)` and a `CallContext` from the `OverflowSignal` expecting the router to route on them. **Handled:** those are now advisory; the seam is unchanged and gains only `model_override`.
- The router's hard-cap path used to *fall back to local* (discretionary overflow) or *raise* (necessity). **Handled:** with no overflow, the hard cap is a pure passive `BudgetExceeded` spend guard; note that `BudgetExceeded` (like other non-context-window `LLMError`s) propagates out of `run()` — the loop catches only `LLMContextWindowExceeded`. This is intentional fail-loud behavior for a budget breach, but is the one place a caller might want to add handling.
- `RoutingDecision.path == "overflow"` and `overflow_triggers` were consumed by observability. **Handled:** kept in the `Literal` and the schema; v1.2 simply emits `"pinned"`/`"manual"` with empty triggers, so dashboards keep parsing.
