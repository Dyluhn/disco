"""Contract tests — the TS frontend types and the Python wire/event models are
hand-mirrored; this proves they don't drift. A new frame or event added on one
side without the other fails here (exactly the bug I hit syncing WSServerFrame's
`file_stream` by hand).

Known intentional asymmetries are allow-listed WITH a reason — so the test catches
NEW drift while documenting the deliberate one-sided cases.

Run: uv run pytest development/harness/tests/test_contract.py
"""

from __future__ import annotations

import asyncio
import json
import re
import typing
from pathlib import Path
from typing import Any

from disco.core.events import EventKind, RunFailure, RunFailureClass
from disco.core.wire import WSClientFrame, WSServerFrame
from disco.retrieval.deep_research._progress_events import (
    RESEARCH_PROGRESS_ACTIONS,
    emit_hold,
)

_TS = (Path(__file__).resolve().parents[3] / "current" / "frontend" / "src" / "types" / "agent.ts").read_text()

# The deep-research progress ACTIONS ride ActionEvent.tool_call.tool_name, and
# the frontend dispatches on that name in its trace parts rather than in the
# event-type mirror. Scanning exactly that directory (plus the type mirror)
# keeps the check meaningful: it is where a new action must be handled, not a
# broad string search over the whole app.
_FRONTEND_SRC = Path(__file__).resolve().parents[3] / "current" / "frontend" / "src"
#: Payloads frozen from a real producer run and consumed byte-identically by the
#: frontend's vitest suites. See `test_hold_payload_shape_matches_ts_fixture`.
_TS_FIXTURES = _FRONTEND_SRC / "lib" / "__fixtures__"
_TS_ACTION_SOURCES = "\n".join(
    [_TS, *(path.read_text() for path in sorted(
        (_FRONTEND_SRC / "lib" / "deepResearchTraceParts").glob("*.ts")
    ))]
)

# Deliberate one-sided cases (documented, not bugs).
#
# 2026-08-02, Epic 12-A: ALL THREE allowlists are now EMPTY. Amendment A3 had
# these same asymmetries fixed byte-level:
#   * `token` (was _SERVER_PY_ONLY) — the backend declares it, so the TS wire
#     union now declares it too. Mirroring a declaration costs nothing and the
#     comment here ("separate FE path") described the research stream, not this
#     socket's contract.
#   * `connection` (was _SERVER_TS_ONLY) — this note was RIGHT: it is
#     synthesized by the WS client and never arrives from Python. It now lives
#     in its own `WSClientSynthesizedFrame` union rather than being an exception
#     inside the wire mirror, and `test_synthesized_frames_stay_client_only`
#     below keeps that honest.
#   * `knowledge` / `datasource` (were _EVENT_PY_ONLY) — "not rendered in the
#     UI" is a rendering decision, not a reason to have no type. Both now have
#     TS interfaces, as does `runtime_constraint`, which was never allow-listed
#     and so had been failing this suite at HEAD.
#
# Keep them empty. An asymmetry that is genuinely intentional belongs in a
# named union with a test, the way `connection` now is.
_SERVER_PY_ONLY: set[str] = set()
_SERVER_TS_ONLY: set[str] = set()
_CLIENT_PY_ONLY: set[str] = set()
_EVENT_PY_ONLY: set[str] = set()


def _py_literals(model, field: str) -> set[str]:
    return set(typing.get_args(model.model_fields[field].annotation))


def _ts_union_types(union_name: str, _seen: frozenset[str] = frozenset()) -> set[str]:
    """The `type: "..."` discriminants of `export type X = ...`.

    Line-based: the `;` separating properties inside each member breaks a naive
    non-greedy regex (it stops at the first member).

    Follows references to other locally-declared unions. Epic 12-A split
    `WSServerFrame` into `WSWireServerFrame | WSClientSynthesizedFrame`, and a
    parser that understood only INLINE members silently returned the empty set
    for it — reporting every backend frame as drift. Resolving the alias keeps
    this test meaningful across that refactor instead of fragile to it.
    """
    assert union_name not in _seen, f"circular type alias through {union_name}"
    out: set[str] = set()
    refs: set[str] = set()
    capturing = False
    for ln in _TS.splitlines():
        if ln.startswith(f"export type {union_name} ="):
            rhs = ln.split("=", 1)[1].strip()
            out.update(re.findall(r'type:\s*"([^"]+)"', rhs))
            # A pure alias-of-unions RHS, e.g. `A | B;` — follow both.
            if re.fullmatch(r"[A-Za-z_]\w*(\s*\|\s*[A-Za-z_]\w*)*;?", rhs):
                refs.update(re.split(r"\s*\|\s*", rhs.rstrip(";")))
            if rhs.endswith(";"):
                break
            capturing = True
            continue
        if capturing:
            s = ln.strip()
            if not s or s.startswith(("export ", "/**")):
                break
            if s.startswith("//"):
                continue
            out.update(re.findall(r'type:\s*"([^"]+)"', ln))
            refs.update(re.findall(r"^\|\s*([A-Za-z_]\w*)\s*;?$", s))
    for ref in refs:
        out |= _ts_union_types(ref, _seen | {union_name})
    assert out, f"extracted NO discriminants for {union_name} — the parser is broken"
    return out


def test_ws_server_frame_types_match():
    py = _py_literals(WSServerFrame, "type")
    ts = _ts_union_types("WSWireServerFrame")
    new = py - ts - _SERVER_PY_ONLY
    assert py - ts == _SERVER_PY_ONLY, f"NEW WSServerFrame drift: py-only={new}"
    assert ts - py == _SERVER_TS_ONLY, f"NEW TS-only WSServerFrame drift: {ts - py}"


def test_synthesized_frames_stay_client_only():
    """Client-synthesized frames must not overlap what the backend declares.

    `WSClientSynthesizedFrame` is the sanctioned home for a frame the client
    invents (today: `connection`, emitted on socket recovery/degradation). It
    must not become a place to park a real wire frame and dodge the mirror.
    """
    py = _py_literals(WSServerFrame, "type")
    synthesized = _ts_union_types("WSClientSynthesizedFrame")
    assert not (synthesized & py), (
        f"client-synthesized frame(s) also declared by the backend: {synthesized & py}"
    )
    assert _ts_union_types("WSServerFrame") == _ts_union_types(
        "WSWireServerFrame"
    ) | synthesized, "WSServerFrame is not exactly wire | synthesized"


def test_ws_client_frame_types_match():
    py = _py_literals(WSClientFrame, "type")
    ts = _ts_union_types("WSClientFrame")
    new = py - ts - _CLIENT_PY_ONLY
    assert py - ts == _CLIENT_PY_ONLY, f"NEW WSClientFrame drift: py-only={new}"
    assert not (ts - py), f"TS sends a frame Python doesn't accept: {ts - py}"


def test_event_kinds_have_ts_mirrors():
    py = {e.value for e in EventKind}
    ts = set(re.findall(r'kind:\s*"([^"]+)"', _TS))  # one per AgentEvent interface
    missing = py - ts - _EVENT_PY_ONLY
    assert not missing, f"event kinds in Python with no TS mirror (invisible to the UI): {missing}"


def test_run_failure_classes_have_ts_mirrors():
    """`ErrorEvent.failure.failure_class` is a CLOSED set on both sides.

    The whole point of the field is that a UI can switch on it — render an
    outage differently from a research result — so a class the backend starts
    emitting with no TS name is a silently unhandled branch, exactly the drift
    the event-kind mirror above catches for events. Python is the one
    declaration (`disco.core.events.RunFailureClass`).
    """
    py = set(typing.get_args(RunFailureClass))
    block = re.search(
        r"export type RunFailureClass =(.*?);", _TS, re.DOTALL
    )
    assert block is not None, "types/agent.ts declares no RunFailureClass"
    ts = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    assert py == ts, f"RunFailureClass drift: py-only={py - ts}, ts-only={ts - py}"


def test_run_failure_fields_have_ts_mirrors():
    """The four parts must all be nameable by the renderer, not just the class."""
    py = set(RunFailure.model_fields)
    block = re.search(r"export interface RunFailure \{(.*?)\n\}", _TS, re.DOTALL)
    assert block is not None, "types/agent.ts declares no RunFailure interface"
    ts = set(re.findall(r"^\s*([a-z_]+)\??:", block.group(1), re.MULTILINE))
    assert py == ts, f"RunFailure field drift: py-only={py - ts}, ts-only={ts - py}"


def _json_shape(value: Any) -> Any:
    """A value reduced to its JSON TYPES — the thing the TS mirror declares.

    A list becomes the shape of its first element (the producer's lists are
    homogeneous by construction), so a list of query strings and a list of
    engine objects are different shapes and an empty list is its own.
    """
    if isinstance(value, dict):
        return {key: _json_shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return ["<empty>"] if not value else [_json_shape(value[0])]
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string" if isinstance(value, str) else "null"


def test_hold_payload_shape_matches_ts_fixture():
    """The `hold` payload's FIELD TYPES, producer vs the frozen TS fixture.

    The action mirror above compares action NAMES, so it saw nothing wrong when
    `queued_queries` — a list of the query strings the loop is holding on —
    was declared `number` in `types/agent.ts`. The parser dutifully coerced the
    list to 0 and the hold panel's "N queries waiting to run" clause became
    unreachable: a shape drift, invisible to every name-level check and to every
    hand-written test object, because the test object was written from the same
    wrong belief.

    So this re-runs the REAL producer and compares its payload's JSON types
    against `hold-action.json`, which the frontend's vitest consumes verbatim.
    Values are free to differ (`resume_at` is a wall-clock instant); types are
    not.
    """
    captured: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, payload: dict[str, Any]) -> None:
        captured.append((name, payload))

    asyncio.run(
        emit_hold(
            emit,
            reason="search_pool_cooling",
            cooling={"brave": 245.0, "mojeek": 612.5},
            resume_in_s=245.0,
            sources_retained=42,
            position=(7, 16),
            queued_queries=["one query", "another query", "a third query"],
        )
    )
    assert [name for name, _ in captured] == ["hold"]
    frozen = json.loads((_TS_FIXTURES / "hold-action.json").read_text())
    assert frozen["tool_name"] == "hold"
    assert _json_shape(captured[0][1]) == _json_shape(frozen["arguments"]), (
        "the `hold` payload's field types moved away from the frozen frontend "
        "fixture — regenerate it and update the TS mirror together"
    )
    block = re.search(r"export interface ResearchHoldPayload \{(.*?)\n\}", _TS, re.DOTALL)
    assert block is not None, "types/agent.ts declares no ResearchHoldPayload"
    assert re.search(r"^\s*queued_queries:\s*string\[\];", block.group(1), re.MULTILINE), (
        "ResearchHoldPayload.queued_queries must mirror the producer's list of "
        "query strings"
    )


def test_deep_research_progress_actions_have_ts_mirrors():
    """Deep Research progress rides the ActionEvent stream, not new EventKinds.

    `turn`, `hold`, `hold_resumed`, `review`, `rework` and `continuation` reach
    the UI as `ActionEvent.tool_call.tool_name`, so the EventKind mirror above
    cannot see them. Without this check the backend can start emitting a
    progress signal the frontend has no name for — which is exactly the state
    the heartbeat rework exists to end. The Python side is the one declaration
    (`RESEARCH_PROGRESS_ACTIONS`); the TS side must name each one where it
    dispatches on the action name.
    """
    py = set(RESEARCH_PROGRESS_ACTIONS)
    ts = set(re.findall(r'"([a-z_]+)"', _TS_ACTION_SOURCES)) | set(
        re.findall(r"^\s*([a-z_]+):", _TS_ACTION_SOURCES, re.MULTILINE)
    )
    missing = py - ts
    assert not missing, (
        "deep research progress actions the backend emits with no TS mirror "
        f"(invisible to the UI): {sorted(missing)}"
    )
