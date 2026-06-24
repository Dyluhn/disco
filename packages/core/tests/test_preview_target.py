"""Backend-aware preview-target resolver + process-backend control-port
containment (Build Soak Bug 6 + Bug 7). Pure-unit coverage of the SHARED resolver
that the finish gate (core) and `verify_web_app` (tools) both consume."""

from __future__ import annotations

from disco.core.loop.preview_target import (
    PREVIEW_PORTS,
    PortOwnership,
    parse_port_ownership,
    process_safe_preview_port,
    reserved_control_ports,
    reserved_port_command_violation,
    resolve_preview_port,
)

CID = "conv_abcd1234ef"  # cid8 == "conv_abc"


# ---- resolver: process / shared-host backend --------------------------------


def test_process_never_targets_reserved_control_port_even_if_owned():
    # 8000 owned by the agent-server (a NON-conversation tmux session); 8080 owned
    # by THIS conversation. The build's app is 8080 — 8000 is the control port.
    owned = {
        8000: PortOwnership(pid=111, session="disco-other999-preview"),
        8080: PortOwnership(pid=222, session="disco-conv_abc-preview"),
    }
    assert (
        resolve_preview_port(host_shared=True, owned=owned, conversation_id=CID) == 8080
    )


def test_process_prefers_conversation_owned_over_foreign():
    # 3000 owned by a sibling conversation, 8080 owned by us → pick 8080 (ours).
    owned = {
        3000: PortOwnership(pid=111, session="disco-sibling1-dev"),
        8080: PortOwnership(pid=222, session="disco-conv_abc-dev"),
    }
    assert (
        resolve_preview_port(host_shared=True, owned=owned, conversation_id=CID) == 8080
    )


def test_process_falls_back_to_any_nonreserved_owned():
    # No session attribution, but a non-reserved port IS listening → use it (it is
    # NOT the control port).
    owned = {5173: PortOwnership(pid=99, session=None)}
    assert (
        resolve_preview_port(host_shared=True, owned=owned, conversation_id=CID) == 5173
    )


def test_process_only_8000_owned_returns_none_not_8000():
    # The ONLY listener is the agent-server on 8000 → undetectable, NEVER 8000.
    owned = {8000: PortOwnership(pid=111, session="disco-other-preview")}
    assert (
        resolve_preview_port(host_shared=True, owned=owned, conversation_id=CID) is None
    )


def test_process_nothing_owned_returns_none():
    assert resolve_preview_port(host_shared=True, owned={}, conversation_id=CID) is None


# ---- resolver: isolated container backend (unchanged) -----------------------


def test_container_keeps_8000_as_canonical_app():
    owned = {8000: PortOwnership(pid=5, session=None)}
    assert (
        resolve_preview_port(host_shared=False, owned=owned, conversation_id=CID) == 8000
    )


def test_container_first_owned_else_canonical_default():
    # nothing owned → default to the canonical first preview port (8000) inside box.
    assert (
        resolve_preview_port(host_shared=False, owned={}, conversation_id=CID)
        == PREVIEW_PORTS[0]
    )
    # first OWNED port wins in preference order.
    owned = {3000: PortOwnership(pid=7, session=None)}
    assert (
        resolve_preview_port(host_shared=False, owned=owned, conversation_id=CID) == 3000
    )


# ---- process-safe default + reserved set ------------------------------------


def test_process_safe_preview_port_is_never_a_control_port():
    safe = process_safe_preview_port()
    assert safe not in reserved_control_ports()
    assert safe in PREVIEW_PORTS
    assert safe != 8000


def test_reserved_control_ports_default_8000_and_8800():
    assert reserved_control_ports() == frozenset({8000, 8800})


# ---- ownership probe parsing ------------------------------------------------


def test_parse_port_ownership_roundtrip_and_malformed():
    out = parse_port_ownership(
        '[{"port": 8080, "pid": 5, "session": "disco-conv_abc-preview"},'
        ' {"port": 8000, "pid": null, "session": null}]'
    )
    assert out[8080] == PortOwnership(pid=5, session="disco-conv_abc-preview")
    assert out[8000].pid is None
    # malformed / empty → empty map (never raises, never wedges the gate)
    assert parse_port_ownership("not json") == {}
    assert parse_port_ownership("") == {}


# ---- reserved-port command containment --------------------------------------


def test_reserved_port_command_violation_blocks_binds():
    r = reserved_control_ports()
    for cmd in (
        "python3 -m http.server 8000",
        "python -m http.server 8000 -d .",
        "uvicorn app:app --port 8000",
        "flask run --port=8000",
        "vite -p 8000",
        "serve -l 0.0.0.0:8000",
        "python -m http.server 8800",
    ):
        assert reserved_port_command_violation(cmd, r) is not None, cmd


def test_reserved_port_command_violation_blocks_kills():
    r = reserved_control_ports()
    assert reserved_port_command_violation("fuser -k 8000/tcp", r) is not None
    assert reserved_port_command_violation("lsof -ti:8000 | xargs kill", r) is not None


def test_reserved_port_command_violation_allows_safe_commands():
    r = reserved_control_ports()
    for cmd in (
        "python3 -m http.server 8080",          # non-reserved preview port
        "vite --port 5173",
        "head -c 8000 file.bin",                # 8000 as a byte count, not a port
        "echo serving 8000 items",
        "python3 -c 'print(8000)'",
        "ls -la",
        # the gate's own urlopen probe targets a resolved non-control port
        "python3 -c \"import urllib.request as U; U.urlopen('http://127.0.0.1:8080/')\"",
        # the ownership probe passes ports as bare argv (no bind context)
        "python3 -c '...' 8000 5173 3000 8080 5000 4321",
    ):
        assert reserved_port_command_violation(cmd, r) is None, cmd
