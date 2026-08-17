"""Fail-closed cross-language contract parity: the TS mirror matches the wire.

`current/frontend/src/types/agent.ts` hand-mirrors `core/_event_types.py::EventKind` and
`core/wire.py`'s `WSServerFrame`/`WSClientFrame` with no codegen. Until this
gate landed there was no enforcement at all, and the mirror had drifted in
production: `KnowledgeEvent`, `DatasourceEvent` and `RuntimeConstraintEvent`
existed on the backend with no TS type, and the TS `WSServerFrame` omitted the
backend's declared `token` variant.

The companion `test_event_kind_frontend_contract.py` pins a *different*
dimension — that every backend kind has a UI **disposition** in
`eventDisposition.ts`. A kind can be classified there (and so pass that test)
while having no TS **type**, which is exactly how the three missing events
survived. This module pins the type mirror.

## Why the frontend surface is read with the compiler, not a regex

`WSServerFrame`'s union members are type literals whose properties are
separated by `;`, so a non-greedy `= (.*?);` captures only the first member and
reports a one-variant union — an under-report that passes. The extractor
(`current/frontend/scripts/extract-contract-surface.mjs`) parses with the pinned
TypeScript 5.9.3 compiler and refuses to emit a partial surface.

## Why there are two server-frame unions

`connection` is **not** backend drift, though it looks like it: the WS client
synthesizes it locally (`api/agent.ts` emits it on socket recovery and on
degradation) and `useBuildStream`'s reducer folds it into `connectionState`.
Deleting it to make the sets match would have broken reconnect-degradation UI.
So the TS side declares `WSWireServerFrame` (mirrors Python exactly) and
`WSClientSynthesizedFrame` (local only), with `WSServerFrame` as their union.

`test_synthesized_frames_are_disjoint_from_the_wire` is what keeps that split
honest: without it, the synthesized union would be an escape hatch — any real
wire frame could be parked there to dodge parity.

## Why this lives in development/tests/architecture and not current/packages/core/tests

It is cross-language governance machinery, like everything else here — it asserts
a property OF the repo, not a behaviour of `disco.core`. It was written under
`current/packages/core/tests/` first, and that placement also perturbed an unrelated
agent-server test (`test_c2_bound_download.py::test_event_loop_stays_responsive_
during_large_assessment`) purely by being imported during collection: that test
asserts a wall-clock ratio it documents as "machine-independent", and adding this
module to the `packages` collection flipped it from pass to fail reproducibly
(6/6 with, 7/7 without, timings stable to +/-6ms). The fragility is that test's,
and is recorded as a finding; this module simply belongs here.

## Deliberately out of scope

This gate proves **discriminant parity** (which event kinds and frame types
exist on both sides), which is what Amendment A3 requires and what silent
breakage has actually come from. It does not prove per-field type parity; that
is a codegen-shaped problem and is not claimed here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import typing
from pathlib import Path
from typing import Any

import pytest
from disco.core.events import EventKind
from disco.core.wire import WSClientFrame, WSServerFrame

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXTRACTOR = _REPO_ROOT / "current" / "frontend" / "scripts" / "extract-contract-surface.mjs"

Surface = dict[str, Any]


def _literal_values(model: type, field: str) -> set[str]:
    """The declared string literals of a Pydantic model's discriminator field."""
    return set(typing.get_args(model.model_fields[field].annotation))


def _string_set(surface: Surface, key: str) -> set[str]:
    """Read a list-of-strings out of the extractor payload, checking its shape.

    A runtime check rather than a cast or a checker suppression: the payload
    crosses a process boundary, so its shape is an assumption worth testing,
    and silencing the type checker at exactly this kind of new seam is the
    failure mode the engineering standards name explicitly.
    """
    value = surface[key]
    assert isinstance(value, list), f"{key}: expected a list, got {type(value).__name__}"
    for item in value:
        assert isinstance(item, str), f"{key}: expected strings, got {type(item).__name__}"
    return set(value)


def _string_map(surface: Surface, key: str) -> dict[str, str]:
    """Read a mapping-of-strings out of the extractor payload, checking its shape."""
    value = surface[key]
    assert isinstance(value, dict), f"{key}: expected an object, got {type(value).__name__}"
    for name, mapped in value.items():
        assert isinstance(name, str) and isinstance(mapped, str), (
            f"{key}: expected string keys and values"
        )
    return dict(value)


@pytest.fixture(scope="module")
def surface() -> Surface:
    """The frontend's declared contract surface, via the pinned TS compiler.

    Fails rather than skips when the toolchain is absent. A skip here would be
    indistinguishable from agreement, and this campaign exists to end gates that
    are inventoried but never actually run.
    """
    if shutil.which("node") is None:
        pytest.fail("`node` is not on PATH — the contract parity gate cannot run")
    if not _EXTRACTOR.is_file():
        pytest.fail(f"contract surface extractor missing: {_EXTRACTOR}")
    result = subprocess.run(
        ["node", str(_EXTRACTOR)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            "frontend contract surface could not be extracted "
            f"(exit {result.returncode}):\n{result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def test_agent_event_union_mirrors_every_backend_event_kind(surface: Surface) -> None:
    backend = {kind.value for kind in EventKind}
    frontend = _string_set(surface, "agent_event_kinds")

    assert backend, "backend EventKind is empty — the denominator is broken"
    assert not backend - frontend, (
        "backend EventKind(s) with NO TypeScript type in the AgentEvent union: "
        f"{sorted(backend - frontend)}. The UI cannot narrow to them, so their "
        "payload fields are unreachable from typed code. Add an interface to "
        "current/frontend/src/types/agent.ts mirroring the backend event and put it in "
        "the AgentEvent union."
    )
    assert not frontend - backend, (
        "AgentEvent declares TypeScript type(s) for kind(s) the backend does not "
        f"emit: {sorted(frontend - backend)}. Remove them, or add the backend "
        "EventKind if the event is real."
    )


def test_agent_event_kind_maps_to_exactly_one_interface(surface: Surface) -> None:
    """Each kind resolves to one interface — the extractor refuses duplicates."""
    interfaces = _string_map(surface, "agent_event_interfaces")
    assert sorted(interfaces) == sorted(_string_set(surface, "agent_event_kinds"))
    assert len(set(interfaces.values())) == len(interfaces), (
        "two event kinds share one TypeScript interface: "
        f"{sorted(interfaces.items())}"
    )


def test_ws_server_frame_wire_types_match_the_backend(surface: Surface) -> None:
    backend = _literal_values(WSServerFrame, "type")
    frontend = _string_set(surface, "ws_wire_server_frame_types")

    assert backend, "backend WSServerFrame.type literal is empty"
    assert not backend - frontend, (
        "backend server frame type(s) missing from the TS WSWireServerFrame "
        f"union: {sorted(backend - frontend)}. The client cannot narrow on a "
        "frame the server may send."
    )
    assert not frontend - backend, (
        "TS WSWireServerFrame declares server frame type(s) the backend never "
        f"sends: {sorted(frontend - backend)}. If the frame is synthesized by "
        "the client, declare it in WSClientSynthesizedFrame instead."
    )


def test_ws_client_frame_types_match_the_backend(surface: Surface) -> None:
    backend = _literal_values(WSClientFrame, "type")
    frontend = _string_set(surface, "ws_client_frame_types")

    assert backend, "backend WSClientFrame.type literal is empty"
    assert not backend - frontend, (
        "backend client frame type(s) the frontend cannot construct: "
        f"{sorted(backend - frontend)}"
    )
    assert not frontend - backend, (
        "frontend declares client frame type(s) the backend will reject "
        f"(WSClientFrame forbids extras): {sorted(frontend - backend)}"
    )


def test_synthesized_frames_are_disjoint_from_the_wire(surface: Surface) -> None:
    """The client-synthesized union must not shelter real wire frames.

    Without this, `WSClientSynthesizedFrame` would be a hole in the gate above:
    any backend frame parked there would satisfy both set comparisons.
    """
    backend = _literal_values(WSServerFrame, "type")
    synthesized = _string_set(surface, "ws_client_synthesized_frame_types")

    assert synthesized, (
        "WSClientSynthesizedFrame is empty — if the client no longer synthesizes "
        "any frame, delete the union and the disjointness carve-out with it "
        "rather than leaving an unused escape hatch."
    )
    assert not synthesized & backend, (
        "frame type(s) declared as client-synthesized are ALSO declared by the "
        f"backend: {sorted(synthesized & backend)}. Either the backend really "
        "sends them (move to WSWireServerFrame) or the backend declaration is "
        "dead (remove it there) — they cannot be both."
    )


def test_composed_server_frame_union_has_no_third_source(surface: Surface) -> None:
    """`WSServerFrame` is exactly wire ∪ synthesized — nothing smuggled in."""
    wire = _string_set(surface, "ws_wire_server_frame_types")
    synthesized = _string_set(surface, "ws_client_synthesized_frame_types")
    composed = _string_set(surface, "ws_server_frame_types")

    assert composed == wire | synthesized, (
        "WSServerFrame is not exactly WSWireServerFrame | WSClientSynthesizedFrame; "
        f"unaccounted: {sorted(composed - (wire | synthesized))}, "
        f"missing: {sorted((wire | synthesized) - composed)}"
    )
