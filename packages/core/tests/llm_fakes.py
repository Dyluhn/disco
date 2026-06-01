"""Faked providers + config builders for the LLM router tests.

The router contract's §10 test plan is headless — "no real models required
(providers are faked)". This module provides that fake plus small config/router
builders so each test stays focused on the behavior under test.
"""

from __future__ import annotations

from perpleximanus.core.llm import (
    CompletionRequest,
    CompletionResponse,
    DefaultLLMRouter,
    InMemoryRoutingSink,
    ModelEntry,
    ModelRole,
    ProposedToolCall,
    Requirement,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)


def _chunks(text: str, size: int = 3) -> list[str]:
    """Split text into contiguous pieces that reassemble to the original."""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


class FakeModelProvider:
    """A `ModelProvider` double. Deterministic; records the requests it saw so
    tests can assert on prompt injection and routing.

    - `raises`: an exception to raise. `raises_times`: raise only for the first
      N calls (then succeed); None = raise every call.
    - `tool_calls`: ProposedToolCalls to return.
    - `supported`: capabilities supported (None = supports everything).
    """

    def __init__(
        self,
        name: str = "fake",
        *,
        text: str = "ok",
        tool_calls: list[ProposedToolCall] | None = None,
        finish_reason: str = "stop",
        cost_usd: float = 0.0,
        raises: Exception | None = None,
        raises_times: int | None = None,
        supported: set[Requirement] | None = None,
    ) -> None:
        self.name = name
        self.text = text
        self.tool_calls = tool_calls or []
        self.finish_reason = finish_reason
        self.cost_usd = cost_usd
        self.raises = raises
        self.raises_times = raises_times
        self.supported = supported
        self.calls = 0
        self.seen_requests: list[CompletionRequest] = []

    def _maybe_raise(self) -> None:
        if self.raises is not None and (
            self.raises_times is None or self.calls <= self.raises_times
        ):
            raise self.raises

    def _make_response(self, req: CompletionRequest, model: str) -> CompletionResponse:
        return CompletionResponse(
            text=self.text,
            tool_calls=self.tool_calls,
            usage=TokenUsage(input_tokens=10, output_tokens=5, cost_usd=self.cost_usd),
            finish_reason=self.finish_reason,  # type: ignore[arg-type]
            model_used=model,
            request_id=req.request_id,
            routing=None,  # the router attaches it (RT1)
        )

    async def complete(self, req: CompletionRequest, *, model: str) -> CompletionResponse:
        self.calls += 1
        self.seen_requests.append(req)
        self._maybe_raise()
        return self._make_response(req, model)

    async def stream_complete(self, req: CompletionRequest, *, model: str):
        self.calls += 1
        self.seen_requests.append(req)
        self._maybe_raise()
        final = self._make_response(req, model)
        for piece in _chunks(final.text):
            yield StreamChunk(delta_text=piece)
        yield StreamChunk(done=True, final=final)

    def supports(self, requirement: Requirement, *, model: str) -> bool:
        return True if self.supported is None else requirement in self.supported


def simple_config() -> RouterConfig:
    """A small two-model config (v1.2 deterministic): a local driver (no vision,
    free) + an assignable frontier model (vision, paid). `default_model` is the
    local driver, so AGENT_DRIVER/RAG/SUMMARIZER all resolve to "local"; "frontier"
    sits in the catalogue as an assignable target for override/cost tests."""
    models = {
        "local": ModelEntry(
            model_id="local-driver-q4",
            provider="ollama",
            context_window=65_536,
            capabilities=frozenset({Requirement.TOOL_CALLING, Requirement.JSON_MODE}),
            family="qwen",
        ),
        "frontier": ModelEntry(
            model_id="frontier-xl",
            provider="openrouter",
            context_window=200_000,
            capabilities=frozenset(
                {Requirement.TOOL_CALLING, Requirement.JSON_MODE, Requirement.VISION}
            ),
            family="anthropic",
            price_in_per_m=3.0,
            price_out_per_m=15.0,
        ),
    }
    assignments = {
        ModelRole.RAG_ANSWERER: "local",
        ModelRole.SUMMARIZER: "local",
    }
    return RouterConfig(models=models, default_model="local", assignments=assignments)


def build_router(
    *,
    config: RouterConfig | None = None,
    local: FakeModelProvider | None = None,
    overflow: FakeModelProvider | None = None,
    **router_kwargs,
) -> tuple[DefaultLLMRouter, InMemoryRoutingSink, dict[str, FakeModelProvider]]:
    """Construct a router over fake providers. Returns (router, sink, providers)."""
    config = config or simple_config()
    providers = {
        "ollama": local or FakeModelProvider("ollama", text="local-says-hi"),
        "openrouter": overflow
        or FakeModelProvider("openrouter", text="frontier-says-hi", cost_usd=0.02),
    }
    sink = router_kwargs.pop("sink", None) or InMemoryRoutingSink()
    router = DefaultLLMRouter(config, providers, sink=sink, **router_kwargs)
    return router, sink, providers
