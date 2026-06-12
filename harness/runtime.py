"""Composition root: wire record/replay impls into a real `ConversationRuntime`
through its EXISTING injection seams (`router=`, `research_providers=`). A replay
runtime executes a full research/deep-research run deterministically off a
cassette — no network, no LLM. A recording runtime runs the real services and
captures everything it touches.

The bundled encoders (rerank/embed/nli via fastembed) are deterministic CPU, so
they run live in both modes and need no cassette.
"""

from __future__ import annotations

from disco.agent_server import ConversationRuntime
from disco.core.llm import ConfigStore, SecretStore

from .cassette import Cassette
from .providers import (
    RecordingExtractionProvider,
    RecordingSearchProvider,
    ReplayExtractionProvider,
    ReplaySearchProvider,
)
from .router import RecordingRouter, ReplayRouter
from .sandbox import RecordingSandboxService, ReplaySandboxService


def _live_encoders() -> dict:
    """The real, deterministic fastembed encoders (reranker/embedder/nli)."""
    from disco.retrieval.live import build_live_retrieval

    deps = build_live_retrieval()  # defaults: bundled encoders + ddgs/local (we override those)
    return {"reranker": deps["reranker"], "embedder": deps["embedder"], "nli": deps["nli"]}


def build_replay_runtime(cassette: Cassette, store, *, config_store=None, secret_store=None):
    """A runtime whose LLM + search + extraction are served from the cassette
    (encoders run live). Fully deterministic; safe to run anywhere."""
    providers = {
        "search": ReplaySearchProvider(cassette),
        "extraction": ReplayExtractionProvider(cassette),
        **_live_encoders(),
    }
    return ConversationRuntime(
        store,
        config_store=config_store or ConfigStore(":memory:"),
        secret_store=secret_store or SecretStore(":memory:"),
        router=ReplayRouter(cassette),
        research_providers=providers,
        # The build surface runs tools through a sandbox; serve it from the cassette
        # too so a recorded BUILD conversation replays deterministically (research
        # runs never touch it, so this is harmless there).
        sandbox_service=ReplaySandboxService(cassette),
    )


def build_recording_runtime(
    cassette: Cassette,
    store,
    *,
    config_store,
    secret_store,
    model_pick="driver-local",
    record_sandbox=False,
):
    """A runtime backed by the REAL services, wrapped to record every LLM + search
    + extraction call into `cassette`. Run a real flow through it, then `save()`.

    `record_sandbox=True` additionally records the build sandbox (exec/read/list)
    by wrapping the host `process` backend — set it when capturing a BUILD demo
    (research captures leave it off; they never touch the sandbox)."""
    from disco.retrieval.live import build_live_retrieval

    real = build_live_retrieval()
    providers = {
        "search": RecordingSearchProvider(real["search"], cassette),
        "extraction": RecordingExtractionProvider(real["extraction"], cassette),
        "reranker": real["reranker"],
        "embedder": real["embedder"],
        "nli": real["nli"],
    }
    sandbox = None
    if record_sandbox:
        from disco.tools.sandbox.process import ProcessSandboxService

        sandbox = RecordingSandboxService(ProcessSandboxService(), cassette)
    rt = ConversationRuntime(
        store,
        config_store=config_store,
        secret_store=secret_store,
        research_providers=providers,
        sandbox_service=sandbox,
    )
    # wrap the per-request router so its completions are recorded
    base_router = rt._router_now(pick=model_pick)
    rt._injected_router = RecordingRouter(base_router, cassette)
    return rt
