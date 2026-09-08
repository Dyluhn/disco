"""Replay human inputs through the runtime using recorded provider responses.

The comparison retains research decisions, source admissions, checkpoint stage
and work counts, review findings, errors, and the complete report. It omits
wall-clock token pulses: instant cassette completions have no live transport.
Generated checkpoint identities and serialized pool sizes depend on run IDs
and timestamps, so they are normalized only in host research trace events.

Legacy plan-gate captures are rejected by the CLI. The checked-in capture
exercises the current gateless Deep Research flow. Replay uses the sandbox
cassette seam for build runs, whose capture must include sandbox interactions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from disco.core.events import (
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
    {
        "id",
        "timestamp",
        "seq",
        "meta",
        "call_id",
        "action_id",
        "llm_response_id",
        "provider_call_id",
    }
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


def _research_trace_tool(ev: BaseEvent) -> str | None:
    """Only host-emitted research narration has these volatile trace fields."""
    if ev.kind != EventKind.ACTION or ev.source != EventSource.AGENT:
        return None
    tool = getattr(ev, "tool_call", None)
    name = getattr(tool, "tool_name", None)
    return name if getattr(ev, "thought", None) == f"Deep Research: {name}" else None


def normalize_event(ev: BaseEvent) -> dict[str, Any]:
    """A volatile-free, comparable view of one event."""
    normalized = _scrub(ev.model_dump(mode="json"))
    tool = _research_trace_tool(ev)
    if tool in {"research_checkpoint_commit", "research_pool"}:
        args = normalized["tool_call"]["arguments"]
        fields = (
            ("conversation_id", "run_id", "checkpoint_id")
            if tool == "research_checkpoint_commit"
            else ("pool_id", "bytes")
        )
        for field in fields:
            args.pop(field, None)
    return normalized


def is_input(ev: BaseEvent) -> bool:
    """Inputs are what a human/external actor fed in: USER messages. The replay
    re-feeds exactly these; everything else is engine OUTPUT to be reproduced."""
    return ev.kind == EventKind.MESSAGE and ev.source == EventSource.USER


def split_io(events: list[BaseEvent]) -> tuple[list[BaseEvent], list[BaseEvent]]:
    """(inputs, outputs) — inputs are re-fed, outputs are diffed."""
    inputs = [e for e in events if is_input(e)]
    outputs = [e for e in events if not is_input(e)]
    return inputs, outputs


def is_legacy_plan_fixture(events: list[BaseEvent]) -> bool:
    """Return true for a pre-gateless capture that still contains PlanEvent."""
    return any(event.kind == EventKind.PLAN for event in events)


def diff_sequences(recorded: list[BaseEvent], replayed: list[BaseEvent]) -> list[str]:
    """Positional semantic diff of two OUTPUT sequences. Returns a list of human-
    readable divergences (empty == identical). Reports the FIRST point of structural
    divergence in detail (a dropped/extra/changed event), since after that the
    sequences are misaligned and further positional diffs are noise."""
    diffs: list[str] = []
    # A cassette completion has no transport stream or wall-clock token pulses.
    # Preserve every research decision, checkpoint stage/count, tool result,
    # review and report; model_activity alone is transport telemetry.
    rn = [normalize_event(e) for e in recorded if _research_trace_tool(e) != "model_activity"]
    pn = [normalize_event(e) for e in replayed if _research_trace_tool(e) != "model_activity"]
    if len(rn) != len(pn):
        diffs.append(f"length: recorded={len(rn)} events, replayed={len(pn)} events")
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
    """Re-feed recorded USER inputs through a fresh engine and return OUTPUTs.

    Current Deep Research is gateless and needs no approval. The bounded plan
    approval branch remains only as compatibility for direct callers replaying
    an older non-CLI recording; ``make replay`` rejects such fixtures and asks
    for a current capture.
    """
    inputs, recorded_outputs = split_io(recorded)
    # Compatibility for old direct callers: only inject approval when the
    # recording proves it proceeded past a plan, never for a plan-only fixture.
    had_approved_plan = any(
        e.kind == EventKind.STATUS
        and getattr(e, "status", None) == ConversationStatus.RUNNING
        and getattr(e, "detail", None) == "plan_approved"
        for e in recorded_outputs
    )

    # ONE runtime: per-conversation surface lives on the instance, so set_surface and
    # kick MUST be the same object (a throwaway would kick with the default surface).
    runtime = build_runtime(store)
    store.create_conversation(cid, owner_id="local")
    runtime.settings._set_surface(cid, surface)
    for msg in inputs:
        await store.append(cid, msg)

    async def _kick_and_wait() -> None:
        runtime.run_controller.kick(cid)
        # `bbe073ae` moved the in-process run tasks off the runtime onto RunRegistry.
        # `task()` is the exact former semantic of `_tasks.get(cid)` — it returns the
        # registered task even when its done-callback has not cleaned it up yet, which
        # `active_task()` would filter out and this await needs.
        task = runtime.run_registry.task(cid)
        if task is not None:
            await task

    from .research_environment import runtime_environment

    with runtime_environment(runtime):
        await _kick_and_wait()
        approvals = 0
        while had_approved_plan and approvals < max_approvals:
            state = await store.get_state(cid)
            if state.execution_status != ConversationStatus.AWAITING_PLAN_APPROVAL:
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
    """Replay the current gateless loop fixture and diff its event sequence."""
    import sys

    from disco.core import SqliteEventStore

    from harness.cassette import Cassette

    root = Path(__file__).resolve().parent / "cassettes"
    events_path = root / "loop_demo.events.jsonl"
    cassette_path = root / "loop_demo.cassette.jsonl"
    if not events_path.exists():
        print(f"missing {events_path} — run 'make capture-loop' first", file=sys.stderr)
        raise SystemExit(2)

    cassette = Cassette.load(cassette_path)
    recorded = load_events(events_path)
    if is_legacy_plan_fixture(recorded):
        print(
            "legacy plan-gate loop_demo fixture detected; rerun `make capture-loop` "
            "to capture the current gateless ReportEvent flow",
            file=sys.stderr,
        )
        raise SystemExit(2)

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
