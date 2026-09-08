"""Capture ONE real gateless Deep Research run into an event-log + cassette
fixture pair for deterministic replay (`make replay`).

Targets the DEEP_RESEARCH surface deliberately: its tools are exactly
search/extract/LLM, all of which `build_replay_runtime` serves from the cassette,
so the replay is fully deterministic (the `build` surface would also need a
sandbox-replay seam — see replay_runner.py's SURFACE CAVEAT).

The capture runs through a terminal ReportEvent. Configure the normal stores
through `DISCO_CONFIG`, `DISCO_SECRETS`, `DISCO_APPROVALS`, and
`DISCO_SECRET_KEY` before running it:

    setsid uv run python -m harness._capture_loop_demo   # = make capture-loop
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from disco.core import LLMMessage, SqliteEventStore
from disco.core.events import ConversationStatus, EventKind, EventSource, MessageEvent
from disco.core.llm import ConfigStore, SecretStore

from .cassette import Cassette
from .research_environment import runtime_environment
from .runtime import build_recording_runtime

_OUT = Path(__file__).resolve().parent / "cassettes"
CID = "loop_demo"
_QUERY = "What is the capital of France and roughly its population?"


async def _kick_and_wait(rt, cid: str) -> None:
    rt.run_controller.kick(cid)
    task = rt.run_registry.task(cid)
    if task is not None:
        await task


async def _main() -> None:
    cas = Cassette()
    store = SqliteEventStore(":memory:")
    rt = build_recording_runtime(
        cas,
        store,
        config_store=ConfigStore(),
        secret_store=SecretStore(),
        model_pick=None,
        enable_thinking=True,  # Match the native Deep Research mode.
    )
    store.create_conversation(CID, owner_id="local")
    rt.settings._set_surface(CID, "deep_research")
    await store.append(
        CID,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=_QUERY)),
    )

    # Gateless Deep Research starts on the user message and must finish with a
    # real report. There is no plan gate or approval input in the current flow.
    with runtime_environment(rt):
        await _kick_and_wait(rt, CID)

    events = await store.get_events(CID)
    state = await store.get_state(CID)
    kinds = [event.kind for event in events]
    if state.execution_status != ConversationStatus.FINISHED:
        raise RuntimeError(f"capture did not finish Deep Research: {state.execution_status!r}")
    if EventKind.PLAN in kinds:
        raise RuntimeError("capture emitted retired PlanEvent protocol")
    reports = [event for event in events if event.kind == EventKind.REPORT]
    if len(reports) != 1:
        raise RuntimeError(f"capture expected one terminal ReportEvent, got {len(reports)}")
    report = reports[0]
    if not getattr(report, "summary", "").strip() or not getattr(report, "sections", ()):
        raise RuntimeError("capture emitted an empty ReportEvent")
    seams = cas.seams()
    if "llm.complete" not in seams:
        raise RuntimeError(f"capture did not record the gateless model calls: {seams!r}")
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
