"""Composition root: wire record/replay impls into a real `ConversationRuntime`
through its EXISTING injection seams (`router=`, `research_providers=`). A replay
runtime executes a full research/deep-research run deterministically off a
cassette — no network, no LLM. A recording runtime runs the real services and
captures everything it touches.

Replay uses small deterministic encoders in-process.  This keeps the fixture
hermetic and avoids downloading or loading model checkpoints during tests.
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
    """Return deterministic, model-free encoders for cassette replay.

    The capture fixture uses the same simple behavior.  Loading the normal
    fastembed stack here would make replay depend on local model downloads and
    would produce scores that differ from the checked-in fixture.
    """
    from disco.retrieval.ranking import HashingEmbedder

    class ReplayReranker:
        async def rerank(self, query, passages, *, top_k):
            del query
            return passages[:top_k]

    class ReplayNLI:
        def entail(self, premise, hypothesis):
            del premise, hypothesis
            return "entail"

        def score(self, premise, hypothesis):
            del premise, hypothesis
            return 1.0

    return {
        "reranker": ReplayReranker(),
        "embedder": HashingEmbedder(),
        "nli": ReplayNLI(),
    }


def build_replay_runtime(cassette: Cassette, store, *, config_store=None, secret_store=None):
    """A runtime whose providers and encoders are fully deterministic and local."""
    providers = {
        "search": ReplaySearchProvider(cassette),
        "extraction": ReplayExtractionProvider(cassette),
        **_live_encoders(),
    }
    # One config for both seams: the injected router now IS the runtime's config
    # source (`DriverRuntime.config_now`), so it must report the same document the
    # runtime's own config store holds, not a second one.
    cfg_store = config_store or ConfigStore(":memory:")
    return ConversationRuntime(
        store,
        config_store=cfg_store,
        secret_store=secret_store or SecretStore(":memory:"),
        router=ReplayRouter(cassette, config=cfg_store.load()),
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
    live_rt = ConversationRuntime(
        store,
        config_store=config_store,
        secret_store=secret_store,
        research_providers=providers,
        sandbox_service=sandbox,
    )
    # Compose the recording router through the runtime's canonical injection seam.
    # Assigning the retired `ConversationRuntime._injected_router` attribute no
    # longer reaches DriverRuntime and silently produced an empty cassette.
    base_router = live_rt._router_now(pick=model_pick)
    return ConversationRuntime(
        store,
        config_store=config_store,
        secret_store=secret_store,
        router=RecordingRouter(base_router, cassette),
        research_providers=providers,
        sandbox_service=sandbox,
    )
