# Technical Design Contract — LLM Router Boundary

**Document type:** Detailed Technical Design (Contract Spec)
**Subsystem:** LLM Router & Model Boundary — BoD §15 (with §12.6, §10-principle "prompt is part of the model abstraction")
**Status:** v1.0 — authoritative contract
**Depends on:** the Event & State contract (`event-state-contract.md`) — specifically `LLMMessage`, the `Summarizer` protocol, and `is_context_window_exceeded()`, all of which this document now *implements*.
**Consumed by:** the agent loop (§12), the memory condenser (§7.3/§5 of the event contract), the citation/grounding verifier (§14), and anything that talks to a model.
**Decision in force:** OpenRouter is wired from the start (not stubbed). The overflow policy and its threshold are real and tested in v1, not placeholders.

---

## 0. What this document is (and is not)

**Is:** the binding contract for how the system talks to language models — the provider-neutral request/response shapes, the router that selects a model by *capability* (not name), the overflow policy between local and OpenRouter, the per-model/per-mode prompt selection, and the two specific functions the event contract is waiting on (`Summarizer.summarize`, `is_context_window_exceeded`). Implementable from this doc with no further architectural decisions; bindable by other subsystems without guessing.

**Is not:** the agent loop, the prompts' actual prose, or provider SDK internals. It defines the *boundary* — what callers hand in, what they get back, how routing decides — not the loop's logic or the wording of any prompt. Interior choices below the contract line (which HTTP client, retry backoff curves, caching mechanics) are the builder's.

**Conventions** (identical to the event contract):
- Illustrative Python 3.12 + Pydantic v2. Field names, types, signatures are **normative**; bodies illustrative.
- **[CONTRACT]** = a guarantee callers may rely on. **[INTERIOR]** = builder's free choice. **[VERIFY]** = fast-moving fact to confirm at build (model names, prices, provider quirks).
- All model names, context sizes, and prices below are **[VERIFY]** — they are the canonical "re-check at build" fields. The *design* does not depend on them; they live in config (§7).

---

## 1. Foundational principles [CONTRACT]

1. **Capability-based, not name-based** (BoD §15.1). Callers ask for a *capability profile* (a role + difficulty + requirements like vision or long-context), never a model name. The router maps profile → concrete model. Swapping or upgrading a model is a config edit; no caller changes.
2. **Provider-neutral at the boundary.** Callers speak `LLMMessage` / `CompletionRequest` / `CompletionResponse` only. Provider-specific shaping (Anthropic vs OpenAI-style tool calls, OpenRouter passthrough) lives entirely inside provider adapters.
3. **Local-first, overflow by policy.** Local models (Ollama/llama.cpp) are the default execution path. Overflow to OpenRouter happens only when the overflow policy fires (low confidence, repeated errors, hardest steps, high-precision verification). The threshold is tunable config, never hardcoded in callers (BoD §15.3).
4. **Every routing/overflow decision is observable.** The router emits a structured `RoutingDecision` record for each call (which profile, which model, local vs overflow, why) for the metrics/audit layer (BoD §18/§20). Routing is never a silent black box.
5. **The prompt is part of the model abstraction** (BoD §12.6). The router (or a prompt provider it owns) selects the system prompt by **model family × operating mode**. A model swap pulls its matching prompt variant automatically.
6. **Deterministic where it must be.** Tool-call and structured-output generation default to temperature 0 (BoD §10.3). Determinism settings are part of the request, not global state.
7. **Failures are typed and classifiable.** Provider errors map to a small typed hierarchy (§6), including the context-window-exceeded case the condenser depends on. Callers never parse raw provider error strings.

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
- **`openrouter`** — the overflow path. OpenRouter exposes an OpenAI-compatible API, so the adapter is an OpenAI-style client pointed at the OpenRouter base URL with the key from the secrets store (never in prompts/sandbox — event/security contracts). [VERIFY] base URL, auth header, and the model-id strings at build. OpenRouter returns usage/cost; the adapter populates `TokenUsage.cost_usd` from it.

### 3.2 Streaming [CONTRACT]
- If `req.stream` is True, callers use `stream_complete`; the agent server forwards `StreamChunk.delta_text` as `token` frames and reconciles to `final` (the source-of-truth event is still the `ActionEvent`/`MessageEvent` the loop appends — tokens are ephemeral, per event contract §7.2).
- Local and OpenRouter adapters both implement streaming. [INTERIOR] chunk sizing.

---

## 4. The Router [CONTRACT]

The router is the single entry point callers use. It owns: profile→model resolution, the overflow decision, prompt-variant injection, retries, and emitting the `RoutingDecision`.

```python
class RoutingDecision(BaseModel):
    """[CONTRACT] Emitted for every call; attached to CompletionResponse and
    logged for observability/audit (BoD §18/§20)."""
    model_config = ConfigDict(frozen=True)
    profile: CapabilityProfile
    chosen_model: str                  # concrete id [VERIFY-valued]
    provider: str                      # "ollama"|"llamacpp"|"openrouter"
    path: Literal["local", "overflow"]
    reason: str                        # human-readable: why this route
    overflow_triggers: list[str] = Field(default_factory=list)  # which rules fired
    attempt: int = 1                   # >1 if this was a retry/escalation

class LLMRouter(Protocol):
    """[CONTRACT] The single entry point for all model calls.

    Guarantees:
      RT1 complete() always returns a CompletionResponse with .routing and
          .usage populated, or raises a typed LLMError (§6).
      RT2 The model is selected ONLY from profile + config + the overflow
          policy. Callers cannot pin a model name (no name parameter exists).
      RT3 The matching prompt variant (model family x mode) is injected when
          the caller did not already supply a system message (§8).
      RT4 Every call emits exactly one RoutingDecision (success or terminal
          failure) to the observability sink.
    """
    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...
    async def stream_complete(self, req: CompletionRequest) -> AsyncIterator[StreamChunk]: ...
```

### 4.1 Resolution algorithm [CONTRACT semantics; INTERIOR code]
For a request, the router, in order:
1. **Resolve the role's model set** from config (§7): each role maps to a `primary` (local) model and an `overflow` (OpenRouter) model, plus the candidates' declared capabilities.
2. **Filter by `requirements`** — drop any model that doesn't `supports()` every required capability (e.g., VISION, LONG_CONTEXT). [CONTRACT] if no model satisfies the requirements, raise `NoEligibleModel` (a typed error), never silently pick a non-conforming model.
3. **Apply the overflow policy** (§5) using `difficulty` + runtime signals to decide `local` vs `overflow`.
4. **Inject the prompt variant** (§8) if needed (RT3).
5. **Execute** via the chosen provider; on a retryable/typed failure, optionally **escalate** per policy (§5.3); record attempts.
6. **Emit** the `RoutingDecision`; attach to the response.

---

## 5. The overflow policy [CONTRACT] — OpenRouter from the start

This is the live, tested decision layer (the project chose OpenRouter-from-start, so this is real in v1). It is a pluggable policy so the *rule set* is swappable, but the interface and the default rules are contractual.

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

### 5.2 Cost governance [CONTRACT]
- The router maintains a per-conversation and global **running cost** (sum of `usage.cost_usd`). Config carries soft/hard budget caps.
- On exceeding a **soft cap**, overflow rules 2/5 (the discretionary ones) are suppressed — only hard-necessity overflow (capability gap, stuck recovery) still fires. On a **hard cap**, overflow is disabled and a `BudgetExceeded` warning event is surfaced (the loop may pause for confirmation). [INTERIOR] exact accounting store.
- This is the mechanism behind BoD §15.4 / R9 (cost runaway) and pairs with the plan-preview gate (which cuts spend upstream).

### 5.3 Escalation vs retry [CONTRACT]
- A **retry** re-runs the same model on a transient error (§6) with backoff. [INTERIOR] backoff curve.
- An **escalation** is a routing change (local→overflow, or overflow-to-a-stronger-overflow-model if config defines a ladder) after `local_retry_before_overflow` local failures. Each attempt increments `RoutingDecision.attempt` and is logged. The `max_iterations` ceiling in the loop (event contract) remains the ultimate backstop.

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

class RoleRouting(BaseModel):
    primary: str                        # ModelEntry key for the local default
    overflow: str | None = None         # ModelEntry key for OpenRouter path
    overflow_eligible: bool = False     # may this role overflow at all? (§5.1)
    # optional ladder of stronger overflow models for escalation (§5.3)
    overflow_ladder: list[str] = Field(default_factory=list)

class RouterConfig(BaseModel):
    models: dict[str, ModelEntry]                 # key -> entry
    roles: dict[ModelRole, RoleRouting]
    # overflow policy thresholds (§5.1) — all tunable, none hardcoded in code
    local_retry_before_overflow: int = 1
    errors_before_overflow: int = 2
    confidence_floor: float = 0.5
    # cost governance (§5.2)
    soft_budget_usd_per_conversation: float | None = None
    hard_budget_usd_per_conversation: float | None = None
    soft_budget_usd_global_daily: float | None = None
    hard_budget_usd_global_daily: float | None = None
```

**[CONTRACT] starting role assignments** (values are [VERIFY] at build; the *shape* is fixed). Per BoD §15.2:
- `AGENT_DRIVER`: primary = a local 70B/35B-class tool-calling model (Q4_K_M+); overflow_eligible = True; overflow = a frontier model; ladder optional.
- `RAG_ANSWERER`: primary = local 8–14B; overflow_eligible = False.
- `QUERY_REWRITER`: primary = local 7–8B; overflow_eligible = False.
- `SUMMARIZER`: primary = a cheap/small local model; overflow_eligible = False.
- `NLI_VERIFIER`: primary = the local cross-encoder (§9); overflow used only for sampled frontier-judge eval (§9.2).

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
- [INTERIOR] the template engine and file layout (mirrors the OpenHands `prompts/` structure: a base per mode, plus `model_specific/<family>` overlays).

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
(The condenser is async-aware; if the event contract's `Summarizer` is sync at the call site, the loop awaits the summary before invoking `condense` — an [INTERIOR] wiring detail, but the contract is: summarization is a SUMMARIZER-role router call.)

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

### 10.1 Routing
- **No name leak:** there is no API by which a caller pins a model; `complete()` selects only from profile+config+policy (assert via interface + a test that two profiles with different difficulty can route to different models with identical messages).
- **Requirement filtering:** a profile requiring VISION never selects a non-vision model; if none qualifies, `NoEligibleModel` is raised (not a silent fallback).
- **RoutingDecision always emitted:** every `complete()` (success or terminal failure) produces exactly one `RoutingDecision` to the sink, with correct `path`/`triggers` (RT4).

### 10.2 Overflow policy
- Table-driven over the five default rules (§5.1): each trigger, in isolation, routes to overflow; none → local.
- **Local-only roles:** `RAG_ANSWERER`/`QUERY_REWRITER`/`SUMMARIZER` never overflow regardless of difficulty unless config opts in.
- **Stuck recovery:** `consecutive_tool_errors >= threshold` routes the agent_driver to overflow.
- **Cost caps:** soft cap suppresses discretionary overflow (rules 2,5) but not necessity overflow (rules 1,4); hard cap raises `BudgetExceeded`.
- **Escalation:** N local failures escalate to overflow; `attempt` increments and is logged.

### 10.3 Errors & context window
- A faked provider raising its context-window error shape is classified as `LLMContextWindowExceeded`, and `is_context_window_exceeded()` returns True for it (the condenser's dependency — cross-tested against the event contract).
- Transient errors retry (per config) then escalate; auth errors are terminal (no retry); content-filter is terminal and typed.

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

- **Agent loop (§12):** `LLMRouter.complete()/stream_complete()`, `CompletionRequest`/`CompletionResponse`, `CapabilityProfile`, `ToolSpec`, `ProposedToolCall`. The loop never names a model; it declares a profile and reads tool calls back. Overflow signals (consecutive_tool_errors, etc.) flow from the loop into the policy.
- **Memory/condenser (event contract §5):** `RouterSummarizer` (satisfies `Summarizer`) and `is_context_window_exceeded()` — both promised there, delivered here.
- **Grounding/citation (§14):** `NLIVerifier` (local cross-encoder), distinct from `complete()`.
- **Agent server (§5/§7):** maps `StreamChunk` → `WSServerFrame(type="token")`.
- **Observability (§18) / cost (§21, R9):** consumes `RoutingDecision` + `TokenUsage` from every response; budget caps live in `RouterConfig`.
- **Security (§17):** the OpenRouter key comes from the secrets store, never prompts/sandbox; this contract assumes that boundary and does not duplicate it.
- **Config/ops (§19):** `RouterConfig` is the one surface to edit when models/prices/thresholds change (R10).

---

## 12. Build order (this subsystem)

1. Core types (§2) + the event contract's `LLMMessage` import wired.
2. `ModelProvider` protocol + the **OpenRouter adapter** and **one local adapter (Ollama)** (§3), with the typed error hierarchy + per-adapter context-window classification (§6).
3. `RouterConfig` loading (§7) with the starting role assignments ([VERIFY] values).
4. `LLMRouter` with resolution (§4.1) + the default `ThresholdOverflowPolicy` (§5) + `RoutingDecision` emission.
5. `PromptProvider` + family×mode selection (§8) — base templates can be thin at first.
6. `RouterSummarizer` (§9.1) — unblocks the condenser; `NLIVerifier` stub/registration (§9.2) — full impl with the grounding subsystem.
7. The full §10 test suite (faked providers); the §10.6 cross-contract test is the acceptance gate.

**Sequencing with the build:** this is a Phase-1 subsystem (the loop needs it). It does **not** block Phase 0 (the walking skeleton's §10 checklist in the event contract doesn't call models). Pin it now; build it when Phase 1 starts.

---

### Appendix — open items specific to this contract
- **[VERIFY] all model ids, context windows, prices, family tags, OpenRouter base URL/auth** at build. They live only in `RouterConfig` and the adapters, so updating them touches nothing else (R10).
- **[OPEN] confidence estimation.** `last_local_confidence` is optional; whether/how the loop estimates local confidence (logprob-based, self-rating, or omitted) is deferred — rule 5 simply doesn't fire if it's None. Decide during Phase 1 loop work.
- **[OPEN] overflow ladder.** Whether overflow escalates through multiple frontier models or a single one is config; start with single, add a ladder only if needed.
- **[INTERIOR] caching.** Prompt/response caching (and prompt-cache-aware behavior the condenser cares about) is a provider-adapter interior; the contract only requires correct `usage` accounting.
- **Next contract to write:** the **agent loop**, which now has both of its dependencies pinned (state + router). After that: the **tool/sandbox boundary**.
