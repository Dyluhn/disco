"""Contract tests — the TS frontend types and the Python wire/event models are
hand-mirrored; this proves they don't drift. A new frame or event added on one
side without the other fails here (exactly the bug I hit syncing WSServerFrame's
`file_stream` by hand).

Known intentional asymmetries are allow-listed WITH a reason — so the test catches
NEW drift while documenting the deliberate one-sided cases.

Run: PYTHONPATH=. uv run pytest harness/tests/test_contract.py
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

from disco.core.events import EventKind
from disco.core.wire import WSClientFrame, WSServerFrame

_TS = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "types" / "agent.ts").read_text()

# Deliberate one-sided cases (documented, not bugs):
_SERVER_PY_ONLY = {"token"}  # research-answer token stream uses a separate FE path
_SERVER_TS_ONLY = {
    "connection"
}  # synthetic browser reconnect state; never arrives from Python
_CLIENT_PY_ONLY = {"pause"}  # accepted by the wire but not yet implemented/sent by the UI
_EVENT_PY_ONLY = {"knowledge", "datasource"}  # internal events; not rendered in the UI


def _py_literals(model, field: str) -> set[str]:
    return set(typing.get_args(model.model_fields[field].annotation))


def _ts_inline_union_types(union_name: str) -> set[str]:
    """The `type: "..."` discriminants of an inline `export type X = | {...} | {...};`.
    Line-based (the `;` inside each member breaks a naive regex)."""
    out: set[str] = set()
    capturing = False
    for ln in _TS.splitlines():
        if ln.startswith(f"export type {union_name} ="):
            capturing = True
            continue
        if capturing:
            s = ln.strip()
            if not s or s.startswith(("export ", "/**")):
                break
            if s.startswith("//"):
                continue
            out.update(re.findall(r'type:\s*"([^"]+)"', ln))
    return out


def test_ws_server_frame_types_match():
    py = _py_literals(WSServerFrame, "type")
    ts = _ts_inline_union_types("WSServerFrame")
    new = py - ts - _SERVER_PY_ONLY
    assert py - ts == _SERVER_PY_ONLY, f"NEW WSServerFrame drift: py-only={new}"
    assert ts - py == _SERVER_TS_ONLY, f"NEW TS-only WSServerFrame drift: {ts - py}"


def test_ws_client_frame_types_match():
    py = _py_literals(WSClientFrame, "type")
    ts = _ts_inline_union_types("WSClientFrame")
    new = py - ts - _CLIENT_PY_ONLY
    assert py - ts == _CLIENT_PY_ONLY, f"NEW WSClientFrame drift: py-only={new}"
    assert not (ts - py), f"TS sends a frame Python doesn't accept: {ts - py}"


def test_event_kinds_have_ts_mirrors():
    py = {e.value for e in EventKind}
    ts = set(re.findall(r'kind:\s*"([^"]+)"', _TS))  # one per AgentEvent interface
    missing = py - ts - _EVENT_PY_ONLY
    assert not missing, f"event kinds in Python with no TS mirror (invisible to the UI): {missing}"
