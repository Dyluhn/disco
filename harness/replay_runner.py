"""Event-log replay runner (plan Phase 3) — the event log IS a recording.

perpleximanus is event-sourced: a conversation is a totally-ordered log of typed
events. The USER `MessageEvent`s (+ a plan approval) are the INPUTS; everything
the engine emits (`action`/`observation`/`plan`/`report`/`status`/`agent_error`)
are the OUTPUTS. Pin the non-determinism (LLM + search + extraction) with a replay
cassette and the engine becomes a pure function inputs → outputs — so re-running a
recorded conversation and diffing the output sequence catches CODE regressions for
free (the deterministic variant). With a LIVE model instead, it's a behavioural
regression check on a real past task (score with the Phase 2 evals).

This module is the machinery + the pure diff/normalization (unit-tested on real
event objects). The full deterministic replay needs a captured (event-log +
cassette) pair — `_capture_loop_demo.py` produces one; the heavy capture is the
same env-blocked step as the Phase 2 `--replay` e2e.

SURFACE CAVEAT: `build_replay_runtime` serves the LLM + search + extraction from
the cassette, but NOT the sandbox. So a `build`-surface conversation (file writes
in a sandbox) is NOT yet fully deterministic on replay — its tool results would
run live. The `deep_research` surface IS fully replayable (its tools are exactly
search/extract/LLM, all cassette-served), so the capture helper targets it.
Build-surface replay is a follow-up: add a `ReplaySandbox` to the Phase 0 layer
and inject it via the `ConversationRuntime(sandbox_service=)` seam.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from perpleximanus.core.events import (
    BaseEvent,
    ConversationStatus,
    EventAdapter,
    EventKind,
    EventSource,
)

# Fields that regenerate every run — never load-bearing for semantic equality.
# (BaseEvent: id/timestamp/seq/meta; correlation ids: call_id/action_id/
# llm_response_id/provider_call_id.) `schema_version` is stable, kept on purpose.
_VOLATILE = frozenset(
    {"id", "timestamp", "seq", "meta", "call_id", "action_id", "llm_response_id",
     "provider_call_id"}
)

# Volatile ids leak into general-purpose VALUE fields too — e.g. a StatusEvent's
# `detail` carries the PlanEvent's id as a cross-reference. We can't blanket-strip
# `detail` (it also holds semantic reasons like "plan_approved"), so we neutralize
# id-SHAPED values wherever they appear: evt_/conv_/call_/resp_<hex> → "<id>".
_ID_VALUE = re.compile(r"^(evt|conv|call|resp|act|tool)_[0-9a-fA-F]{6,}$")


def _scrub(value: Any) -> Any:
    """Recursively drop volatile keys AND neutralize volatile id-shaped values so two
    runs of the same conversation compare equal on SEMANTICS (kind, source, thought,
    tool name+args, result) — not on the ids/timestamps that differ on every run."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str) and _ID_VALUE.match(value):
        return "<id>"
    return value


def normalize_event(ev: BaseEvent) -> dict[str, Any]:
    """A volatile-free, comparable view of one event."""
    return _scrub(ev.model_dump(mode="json"))


def is_input(ev: BaseEvent) -> bool:
    """Inputs are what a human/external actor fed in: USER messages. The replay
    re-feeds exactly these; everything else is engine OUTPUT to be reproduced."""
    return ev.kind == EventKind.MESSAGE and ev.source == EventSource.USER


def split_io(events: list[BaseEvent]) -> tuple[list[BaseEvent], list[BaseEvent]]:
    """(inputs, outputs) — inputs are re-fed, outputs are diffed."""
    inputs = [e for e in events if is_input(e)]
    outputs = [e for e in events if not is_input(e)]
    return inputs, outputs


def diff_sequences(recorded: list[BaseEvent], replayed: list[BaseEvent]) -> list[str]:
    """Positional semantic diff of two OUTPUT sequences. Returns a list of human-
    readable divergences (empty == identical). Reports the FIRST point of structural
    divergence in detail (a dropped/extra/changed event), since after that the
    sequences are misaligned and further positional diffs are noise."""
    diffs: list[str] = []
    rn = [normalize_event(e) for e in recorded]
    pn = [normalize_event(e) for e in replayed]
    if len(rn) != len(pn):
        diffs.append(
            f"length: recorded={len(rn)} events, replayed={len(pn)} events"
        )
    for i, (r, p) in enumerate(zip(rn, pn, strict=False)):
        if r != p:
            rk, pk = r.get("kind"), p.get("kind")
            if rk != pk:
                diffs.append(f"event[{i}]: kind recorded={rk!r} replayed={pk!r}")
            else:
                diffs.append(f"event[{i}] (kind={rk}): payload diverged")
            break  # first divergence misaligns the rest; stop the noise
    return diffs


# ---- the deterministic re-run (needs a cassette + a runtime builder) --------


async def replay_conversation(
    recorded: list[BaseEvent],
    *,
    build_runtime,
    store,
    cid: str = "replay",
    surface: str = "build",
    max_approvals: int = 4,
) -> list[BaseEvent]:
    """Re-feed the recorded USER inputs through a fresh engine (built by
    `build_runtime(store)` — wire a ReplayRouter + replay providers here) and return
    the OUTPUT events it produces. Approves plans when the loop parks at plan
    approval AND the recording shows it proceeded past a plan (bounded, so a buggy
    loop can't approve forever)."""
    inputs, recorded_outputs = split_io(recorded)
    had_plan = any(e.kind == EventKind.PLAN for e in recorded_outputs)

    # ONE runtime: per-conversation surface lives on the instance, so set_surface and
    # kick MUST be the same object (a throwaway would kick with the default surface).
    runtime = build_runtime(store)
    store.create_conversation(cid, owner_id="local")
    runtime.set_surface(cid, surface)
    for msg in inputs:
        await store.append(cid, msg)

    async def _kick_and_wait() -> None:
        runtime.kick(cid)
        task = runtime._tasks.get(cid)
        if task is not None:
            await task

    await _kick_and_wait()
    approvals = 0
    while had_plan and approvals < max_approvals:
        state = await store.get_state(cid)
        if getattr(state, "status", None) != ConversationStatus.AWAITING_PLAN_APPROVAL:
            break
        await runtime.approve_plan(cid)
        approvals += 1
        await _kick_and_wait()

    events = await store.get_events(cid)
    _, outputs = split_io(events)
    return outputs


def load_events(path: str | Path) -> list[BaseEvent]:
    """Load a recorded event log (one JSON event per line)."""
    out: list[BaseEvent] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            out.append(EventAdapter.validate_python(json.loads(line)))
    return out


async def _main() -> None:
    """`make replay` entrypoint: replay the captured loop fixture deterministically
    and diff. Exits non-zero (the regression signal) if the event sequence drifted."""
    import sys

    from perpleximanus.core import SqliteEventStore

    from harness.cassette import Cassette

    root = Path(__file__).resolve().parent / "cassettes"
    events_path = root / "loop_demo.events.jsonl"
    cassette_path = root / "loop_demo.cassette.jsonl"
    if not events_path.exists():
        print(f"missing {events_path} — run 'make capture-loop' first", file=sys.stderr)
        raise SystemExit(2)

    cassette = Cassette.load(cassette_path)
    recorded = load_events(events_path)

    def _builder(store):
        from harness.runtime import build_replay_runtime

        return build_replay_runtime(cassette, store)

    surface = "deep_research"
    outputs = await replay_conversation(
        recorded, build_runtime=_builder, store=SqliteEventStore(":memory:"), surface=surface
    )
    _, recorded_outputs = split_io(recorded)
    diffs = diff_sequences(recorded_outputs, outputs)
    if diffs:
        print("REPLAY DIVERGED (a code regression changed the event sequence):")
        for d in diffs:
            print(f"  - {d}")
        raise SystemExit(1)
    print(f"✓ replay reproduced {len(outputs)} output events identically (no regression)")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
