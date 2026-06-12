"""Fault / chaos injection at the runtime seams (plan Phase 4).

The ONE harness that is legitimately synthetic. Everything else replays verbatim
captured samples ([[feedback-real-sample-harnesses]]); you cannot capture a real
OOM-killed sandbox or a real 429 on demand. The discipline shifts accordingly:
the error TYPES are the real hierarchy (`LLMTransientError`, `LLMAuthError`,
`SandboxUnavailableError`) raised at the REAL seam, even though the trigger is a
counter. The point is to prove the EXISTING resilience fires — the router's
same-model retry (`routing.py` `complete`), the sandbox session's `_recreate`
(`session.py`), the grounding pipeline's empty-passage degradation — through the
same `ModelProvider` / `SandboxService` boundaries the rest of the harness injects
at. A regression that wires AROUND the resilience (swaps a provider, drops a
cancel check) is caught here, where the unit tests structurally can't see it.

Self-contained (only the public package API) so it imports from `harness/tests`
under `PYTHONPATH=.` without the per-package test fakes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    DefaultLLMRouter,
    InMemoryRoutingSink,
    ModelEntry,
    ModelRole,
    Requirement,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)
from disco.tools.sandbox import (
    ExecResult,
    SandboxSpec,
    SandboxUnavailableError,
)

# ---- LLM provider faults (drives the router's same-model retry) -------------


class FaultyProvider:
    """A `ModelProvider` double that raises a QUEUE of typed errors, then returns a
    canned response. Hand it to `build_faulty_router` and the REAL `DefaultLLMRouter`
    decides whether to retry (transient) or propagate (terminal) — so the assertion
    is on the router's resilience, not on a re-implementation of it.

    `errors`: exceptions to raise in order, one per call, before the provider
    starts succeeding. `[LLMTransientError(), LLMTransientError()]` fails the first
    two calls then succeeds on the third. `[LLMAuthError()]` raises once (the router
    should NOT retry a terminal error, so it never reaches the success path)."""

    def __init__(
        self,
        name: str = "faulty",
        *,
        errors: list[Exception] | None = None,
        text: str = "recovered",
        stream_raise_after_chunk: Exception | None = None,
    ) -> None:
        self.name = name
        self._errors = list(errors or [])
        self._text = text
        # If set, stream_complete yields ONE delta chunk and THEN raises — models a
        # transient that fires mid-stream, after content already reached the UI. Used
        # to prove the router does NOT retry (and duplicate) a partially-emitted stream.
        self._stream_raise_after_chunk = stream_raise_after_chunk
        self.calls = 0

    def _maybe_raise(self) -> None:
        if self.calls <= len(self._errors):
            raise self._errors[self.calls - 1]

    def _response(self, req: CompletionRequest, model: str) -> CompletionResponse:
        return CompletionResponse(
            text=self._text,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,  # the router attaches it (RT1)
        )

    async def complete(self, req: CompletionRequest, *, model: str) -> CompletionResponse:
        self.calls += 1
        self._maybe_raise()
        return self._response(req, model)

    async def stream_complete(
        self, req: CompletionRequest, *, model: str
    ) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        self._maybe_raise()
        final = self._response(req, model)
        if self._stream_raise_after_chunk is not None:
            yield StreamChunk(delta_text=final.text[:3])  # partial body reaches the UI
            raise self._stream_raise_after_chunk
        yield StreamChunk(delta_text=final.text)
        yield StreamChunk(done=True, final=final)

    def supports(self, requirement: Requirement, *, model: str) -> bool:
        return True


def fault_config() -> RouterConfig:
    """A single local-only model. RAG_ANSWERER/SUMMARIZER resolve here with NO
    overflow target, so a transient error retries the SAME model up to the cap and
    then raises — there is no escalation to muddy the assertion."""
    models = {
        "local": ModelEntry(
            model_id="local-driver-q4",
            provider="faulty",
            context_window=65_536,
            capabilities=frozenset({Requirement.TOOL_CALLING, Requirement.JSON_MODE}),
            family="qwen",
        ),
    }
    return RouterConfig(
        models=models,
        default_model="local",
        assignments={ModelRole.RAG_ANSWERER: "local", ModelRole.SUMMARIZER: "local"},
    )


def build_faulty_router(
    provider: FaultyProvider, *, config: RouterConfig | None = None
) -> tuple[DefaultLLMRouter, InMemoryRoutingSink]:
    """Wire a `FaultyProvider` behind the REAL router. Returns (router, sink)."""
    cfg = config or fault_config()
    sink = InMemoryRoutingSink()
    router = DefaultLLMRouter(cfg, {provider.name: provider}, sink=sink)
    return router, sink


# ---- sandbox faults (drives the session's _recreate) ------------------------


class _FaultyInstance:
    """A `SandboxInstance` that DIES (raises the typed death signal) after N execs —
    models an OOM/eviction killing the whole box mid-session. The session must catch
    `SandboxUnavailableError`, recreate, and surface a clean `SandboxError`."""

    def __init__(self, label: str, *, die_after: int | None = None) -> None:
        self.id = label
        self.owner_id = "o"
        self.conversation_id = "c"
        self.spec = SandboxSpec()
        self.execs = 0
        self.destroyed = False
        self._die_after = die_after

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self.execs += 1
        if self._die_after is not None and self.execs > self._die_after:
            raise SandboxUnavailableError(f"box evicted mid-session ({self.id})")
        return ExecResult(exit_code=0, stdout=f"[{self.id}] {cmd}", stderr="")

    async def read_file(self, path: str) -> bytes:
        return b"data"

    async def write_file(self, path: str, data: bytes) -> None:
        return None

    async def list_dir(self, path: str) -> list[str]:
        return []

    def display_url(self) -> str | None:
        return None

    async def destroy(self) -> None:
        self.destroyed = True


class FaultySandboxService:
    """Creates `_FaultyInstance`s. Only the FIRST box is made mortal (`first_dies_after`),
    so the re-created session lands on a healthy one and the next call works — proving
    the session HEALS rather than wedging."""

    name = "faulty"

    def __init__(self, *, first_dies_after: int | None = None) -> None:
        self._first_dies_after = first_dies_after
        self.created: list[_FaultyInstance] = []

    async def create(self, spec, *, owner_id: str, conversation_id: str) -> _FaultyInstance:
        n = len(self.created) + 1
        die = self._first_dies_after if n == 1 else None
        inst = _FaultyInstance(f"inst-{n}", die_after=die)
        self.created.append(inst)
        return inst
