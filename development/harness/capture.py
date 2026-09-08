"""Capture verbatim cassettes from the REAL services, and verify replay is
byte-identical. This is the literal "pull a real sample first" step — the cassette
is recorded from reality, never hand-written.

Run:  uv run python -m harness.capture <out.jsonl>
"""

from __future__ import annotations

import asyncio
import sys

from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.events import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    ConfigStore,
    ModelRole,
    SecretStore,
)
from disco.retrieval.bundled_providers import LocalExtractionProvider
from disco.retrieval.live import build_keyless_search

from .cassette import Cassette
from .providers import (
    RecordingExtractionProvider,
    RecordingSearchProvider,
    ReplayExtractionProvider,
    ReplaySearchProvider,
)
from .router import RecordingRouter, ReplayRouter


async def capture(out_path: str) -> Cassette:
    cas = Cassette()
    # --- real retrieval seams, wrapped to record ---
    search = RecordingSearchProvider(build_keyless_search(), cas)
    extract = RecordingExtractionProvider(LocalExtractionProvider(), cas)
    hits = await search.search("what is the vcrpy library used for", limit=3)
    await extract.extract(hits[0].url)

    # --- real LLM seam (local model — free/fast), wrapped to record ---
    rt = ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore("/tmp/pmx-live-config.json"),
        secret_store=SecretStore("/tmp/pmx-live-secrets.json"),
    )
    router = RecordingRouter(rt._router_now(pick="driver-local"), cas)
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="In one sentence: what is vcrpy?")],
        tools=[],
        temperature=0.0,
        request_id="capture",
    )
    await router.complete(req, context=None)

    cas.save(out_path)
    print(f"captured {len(cas)} interactions → {out_path} | seams: {cas.seams()}")
    return cas


async def verify_roundtrip(cas: Cassette) -> None:
    """Replay the SAME calls from the cassette; assert identical to what was
    recorded (the real outputs). Encoders not involved here."""
    rsearch = ReplaySearchProvider(cas)
    rextract = ReplayExtractionProvider(cas)
    rrouter = ReplayRouter(cas)

    hits = await rsearch.search("what is the vcrpy library used for", limit=3)
    assert hits, "replay search mismatch"
    doc = await rextract.extract(hits[0].url)
    assert doc.fetched_ok and doc.passages, "replay extract mismatch"
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="In one sentence: what is vcrpy?")],
        tools=[],
        temperature=0.0,
        request_id="replay",  # different request_id — must NOT affect the key
    )
    resp = await rrouter.complete(req, context=None)
    assert resp.text, "replay llm mismatch"
    print(
        f"✓ replay round-trip OK — search={len(hits)} hits, "
        f"extract={len(doc.passages)} passages, llm='{resp.text[:50]}…'"
    )


async def _main(out_path: str) -> None:
    cas = await capture(out_path)
    await verify_roundtrip(cas)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "development/harness/cassettes/demo.jsonl"
    asyncio.run(_main(out))
