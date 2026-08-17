"""Contract tests — the TS frontend types and the Python wire/event models are
hand-mirrored; this proves they don't drift. A new frame or event added on one
side without the other fails here (exactly the bug I hit syncing WSServerFrame's
`file_stream` by hand).

Known intentional asymmetries are allow-listed WITH a reason — so the test catches
NEW drift while documenting the deliberate one-sided cases.

Run: PYTHONPATH=. uv run pytest development/harness/tests/test_contract.py
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

from disco.core.events import EventKind
from disco.core.wire import WSClientFrame, WSServerFrame

_TS = (Path(__file__).resolve().parents[3] / "current" / "frontend" / "src" / "types" / "agent.ts").read_text()

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
