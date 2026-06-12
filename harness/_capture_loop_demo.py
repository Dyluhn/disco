"""Capture ONE real deep-research agent-loop conversation into a (event-log +
cassette) fixture pair, so the Phase 3 deterministic replay (`make replay` +
`test_replay_runner.py::test_deterministic_replay_reproduces_recorded_events`)
has real inputs/outputs to reproduce.

Targets the DEEP_RESEARCH surface deliberately: its tools are exactly
search/extract/LLM, all of which `build_replay_runtime` serves from the cassette,
so the replay is fully deterministic (the `build` surface would also need a
sandbox-replay seam — see replay_runner.py's SURFACE CAVEAT).

HEAVY (real local model + multi-step gather/synthesize). The interactive harness
SIGKILLs long model-loading commands, so run this DETACHED or when the model is
already warm:

    PYTHONPATH=. setsid uv run python -m harness._capture_loop_demo   # = make capture-loop
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from disco.core import LLMMessage, SqliteEventStore
from disco.core.events import ConversationStatus, EventSource, MessageEvent
from disco.core.llm import ConfigStore, SecretStore

from .cassette import Cassette
from .runtime import build_recording_runtime

_OUT = Path(__file__).resolve().parent / "cassettes"
CID = "loop_demo"
_QUERY = "What is the capital of France and roughly its population?"


async def _kick_and_wait(rt, cid: str) -> None:
    rt.kick(cid)
    task = rt._tasks.get(cid)
    if task is not None:
        await task


async def _main() -> None:
    cas = Cassette()
    store = SqliteEventStore(":memory:")
    rt = build_recording_runtime(
        cas,
        store,
        config_store=ConfigStore("/tmp/pmx-live-config.json"),
        secret_store=SecretStore("/tmp/pmx-live-secrets.json"),
    )
    store.create_conversation(CID, owner_id="local")
    rt.set_surface(CID, "deep_research")
    await store.append(
        CID,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=_QUERY)),
    )

    # plan-gated: first kick produces the plan, then approve, then it runs.
    await _kick_and_wait(rt, CID)
    for _ in range(4):
        state = await store.get_state(CID)
        if getattr(state, "status", None) != ConversationStatus.AWAITING_PLAN_APPROVAL:
            break
        await rt.approve_plan(CID)
        await _kick_and_wait(rt, CID)

    events = await store.get_events(CID)
    _OUT.mkdir(exist_ok=True)
    log_path = _OUT / "loop_demo.events.jsonl"
    with open(log_path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev.model_dump(mode="json")) + "\n")
    cas.save(_OUT / "loop_demo.cassette.jsonl")
    print(
        f"captured {len(events)} events → {log_path} | "
        f"cassette {len(cas)} interactions, seams {cas.seams()}"
    )


if __name__ == "__main__":
    asyncio.run(_main())
