"""Provider-neutral request/response types — llm-router-contract.md §2 (+ §4
RoutingDecision, §3 StreamChunk).

These extend the event/state contract's `LLMMessage` (the wire-level message
shape, imported, never redefined). Callers speak only these types; all
provider-specific shaping lives inside provider adapters (provider.py).

Field names, types, and signatures are **normative**.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..events import LLMMessage  # the wire-level message shape (event contract)

# ---- capability vocabulary --------------------------------------------------


class ModelRole(str, Enum):
    """The four roles from BoD §15.2, plus the summarizer (§7.3). A caller
    declares the ROLE it needs; the router maps role -> model via config."""

    AGENT_DRIVER = "agent_driver"  # the loop's tool-calling/planning brain
    RAG_ANSWERER = "rag_answerer"  # grounded answer synthesis
    QUERY_REWRITER = "query_rewriter"  # query expansion/decomposition
    SUMMARIZER = "summarizer"  # condensation (cheap, separate model)
    NLI_VERIFIER = "nli_verifier"  # citation entailment (a cross-encoder, §9)


class Difficulty(str, Enum):
    """A caller's hint about how hard THIS call is. Feeds the overflow policy."""

    ROUTINE = "routine"
    HARD = "hard"


class Requirement(str, Enum):
    """Hard capability requirements that constrain model selection."""

    VISION = "vision"  # must accept images
    LONG_CONTEXT = "long_context"  # must handle large inputs (>~64k)
    TOOL_CALLING = "tool_calling"  # must support structured tool calls
    JSON_MODE = "json_mode"  # must support constrained/JSON output
    # W4 (Track-A §10.8): the model is reliable at ANCHORED edits (SEARCH/REPLACE
    # / str_replace against live disk text). Default-OFF — unknown/weak models do
    # NOT get it and fall back to whole-file writes through the syntax-gate.
    # Capable models that benchmark well on diff edits opt in via ModelEntry.
    ANCHORED_EDIT = "anchored_edit"


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
    mode: OperatingMode | None = None  # informs prompt selection if set


# ---- tool specs (the loop hands these in; adapters shape them) --------------


class ToolSpec(BaseModel):
    """Provider-neutral description of a callable tool, as the model sees it.
    The agent loop builds these from the tool registry; adapters translate to
    each provider's tool/function schema."""

    model_config = ConfigDict(frozen=True)
    name: str
    description: str
    parameters_schema: dict[str, Any]  # JSON Schema for arguments


# ---- the request ------------------------------------------------------------


class CompletionRequest(BaseModel):
    """[CONTRACT] The provider-neutral request. Callers build this; the router
    routes it; adapters execute it."""

    model_config = ConfigDict(frozen=True)
    profile: CapabilityProfile
    messages: list[LLMMessage]
    tools: list[ToolSpec] | None = None
    # B9: Assistant prefill. When set, the provider
    # appends a trailing assistant message with this content to the
    # prompt, and the model continues from there. Used for action-space
    # masking (e.g. forcing a thought to start with a specific plan).
    assistant_prefill: str | None = None
    # temperature defaults to 0 for determinism where the role implies
    # structured output; callers may override (BoD §10.3).
    temperature: float = 0.0
    max_tokens: int | None = None
    response_format: Literal["text", "json"] = "text"
    assist: bool = False
    # Per-call control of a reasoning model's "thinking" (Qwen3.6 et al). None →
    # use the provider/model default; False → force thinking OFF for this call.
    # Grounded extraction (RAG_ANSWERER) sets False: it does NOT need to reason, and
    # leaving it on makes the model burn its whole token budget thinking → empty
    # `content` (the "up but not grounding" failure the canary caught). The agent
    # driver leaves it None so it keeps its reasoning.
    enable_thinking: bool | None = None
    # F5 — repair-attempt counter (1 = first try). Set by the engine/loop when
    # this call is a retry of a prior failure (LLMTransientError or requery).
    # The OpenAI provider reads it to FORCE `enable_thinking=False` on a
    # repair attempt ≥ 2 so a failed call doesn't burn its whole budget
    # thinking again. Assist-OFF callers ignore it; the field defaults to 1
    # (first try) so it is byte-identical to today for any caller that
    # does not set it. See openai_provider._payload / _truncate_think_block.
    attempt: int = 1
    # Opaque per-call correlation id, surfaced back on the response and carried
    # into ActionEvent.llm_response_id (event contract). VOLATILE.
    request_id: str | None = None
    # If True, caller wants token streaming (use stream_complete, §3.2).
    stream: bool = False


# ---- the response -----------------------------------------------------------


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)
    input_tokens: int
    output_tokens: int
    # Cost in USD if known (OpenRouter reports it; local = 0.0).
    cost_usd: float = 0.0
    # Cluster 8: cached prompt tokens (read from cache, ~10x cheaper). Surfaced
    # so KV-cache hit-rate is OBSERVABLE — without it, a silently-broken prefix
    # is invisible except on the bill. 0 when the provider doesn't report it.
    cached_tokens: int = 0


class ProposedToolCall(BaseModel):
    """A tool call the model wants to make. The loop converts this into the
    event contract's ToolCall/ActionEvent. Provider-neutral."""

    model_config = ConfigDict(frozen=True)
    tool_name: str
    arguments: dict[str, Any]
    provider_call_id: str | None = None  # VOLATILE


class RoutingDecision(BaseModel):
    """[CONTRACT] Emitted for every call; attached to CompletionResponse and
    logged for observability/audit (BoD §18/§20)."""

    model_config = ConfigDict(frozen=True)
    profile: CapabilityProfile
    chosen_model: str  # concrete id [VERIFY-valued]
    provider: str  # "ollama"|"llamacpp"|"openrouter"
    # v1.2: deterministic routing uses "pinned" (settings assignment) / "manual"
    # (per-conversation model-pill override). "local"/"overflow" are retained for
    # the DORMANT intelligent-routing revival path (see config.py / policy.py).
    path: Literal["local", "overflow", "pinned", "manual"]
    reason: str  # human-readable: why this route (e.g. "config")
    overflow_triggers: list[str] = Field(default_factory=list)  # DORMANT: rules fired
    attempt: int = 1  # >1 if this was a transient same-model retry


class CompletionResponse(BaseModel):
    """[CONTRACT] What every completion returns, regardless of provider or
    local/overflow path. The loop reads .text / .tool_calls; the condenser
    reads .text; observability reads .usage and .routing.

    NOTE on `routing`: the contract describes it as "always present, populated
    by the router". Providers cannot know the routing decision (it is the
    router's), so the field is typed Optional to make the provider boundary
    implementable; the **router guarantees** it is non-None on every response it
    returns to a caller (RT1). Providers return it as None.
    """

    model_config = ConfigDict(frozen=True)
    text: str = ""  # assistant text / thought
    tool_calls: list[ProposedToolCall] = Field(default_factory=list)
    usage: TokenUsage
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter", "error"]
    model_used: str  # concrete model id actually used [VERIFY-valued]
    request_id: str | None = None
    routing: RoutingDecision | None = None


class StreamChunk(BaseModel):
    """[CONTRACT] An incremental piece of a streamed completion. The agent
    server maps these to WSServerFrame(type='token') (event contract §7.2). The
    terminal chunk carries the assembled CompletionResponse."""

    model_config = ConfigDict(frozen=True)
    delta_text: str = ""
    # Tool-call argument streaming (the file body for a `file_write` arrives HERE,
    # not in delta_text). `tool_args_delta` is a raw JSON fragment of the tool call's
    # `arguments` string as the model emits it; consumers accumulate per `tool_index`
    # and may incrementally extract a field (e.g. `content`) for watch-it-write UX.
    tool_name: str = ""
    tool_args_delta: str = ""
    tool_index: int = 0
    done: bool = False
    final: CompletionResponse | None = None  # present iff done is True
