"""One-time: capture a real research run into development/harness/cassettes/research_demo.jsonl
so the --replay eval + the replay test are deterministic. Heavy (cold fastembed +
LLM); run once. uv run python -m harness._capture_research_demo
"""

from __future__ import annotations

import asyncio

from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, SecretStore

from .cassette import Cassette
from .eval_runner import (
    _encoders,
    capture_research,
    run_research,
    score_research,
)
from .providers import ReplayExtractionProvider, ReplaySearchProvider
from .router import ReplayRouter

QUERY = "What is the vcrpy library and what is it used for?"


async def main() -> None:
    rt = ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore("/tmp/pmx-live-config.json"),
        secret_store=SecretStore("/tmp/pmx-live-secrets.json"),
    )
    router = rt._router_now(pick="driver-local")
    cas = Cassette()
    print("capturing real research run…", flush=True)
    await capture_research(QUERY, cas, router)
    cas.save("development/harness/cassettes/research_demo.jsonl")
    print(f"captured {len(cas)} interactions | seams: {cas.seams()}", flush=True)

    enc = _encoders()
    ans = await run_research(
        QUERY, ReplaySearchProvider(cas), ReplayExtractionProvider(cas), enc, ReplayRouter(cas)
    )
    print("replay score:", score_research(ans, {"min_faithfulness": 0.5, "must_cite_domains": []}))


if __name__ == "__main__":
    asyncio.run(main())
