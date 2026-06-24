"""Backend-aware preview-target resolver + process-backend control-port
containment (Build Soak Bug 6 + Bug 7). Pure-unit coverage of the SHARED resolver
that the finish gate (core) and `verify_web_app` (tools) both consume."""

from __future__ import annotations

from disco.core.loop.preview_target import (
    PREVIEW_PORTS,
    PortOwnership,
    explicit_target_allowed,
    parse_port_ownership,
    process_safe_preview_port,
    remap_reserved_preview_serve,
    reserved_control_ports,
    reserved_port_command_violation,
    resolve_preview_port,
    target_url_port,
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


def test_process_unattributed_owned_port_is_undetectable_not_guessed():
    # A non-reserved port IS listening but we CANNOT tie it to this conversation
    # (no session, or a foreign session) → UNDETECTABLE (None). Never guess: it
    # could be a sibling conversation's app or an unrelated server (false PASS).
    assert (
        resolve_preview_port(
            host_shared=True,
            owned={5000: PortOwnership(pid=99, session=None)},
            conversation_id=CID,
        )
        is None
    )
    assert (
        resolve_preview_port(
            host_shared=True,
            owned={3000: PortOwnership(pid=99, session="disco-sibling1-dev")},
            conversation_id=CID,
        )
        is None
    )


def test_process_only_8000_owned_returns_none_not_8000():
    # The ONLY listener is the agent-server on 8000 → undetectable, NEVER 8000.
    owned = {8000: PortOwnership(pid=111, session="disco-other-preview")}
    assert (
        resolve_preview_port(host_shared=True, owned=owned, conversation_id=CID) is None
    )


def test_process_frontend_5173_and_agent_8000_reachable_but_foreign_returns_none():
    # P1 #1: 5173 (the Vite UI) and 8000 (agent-server) are reachable but NEITHER is
    # conversation-owned → resolver returns None (NOT 5173, NOT 8000). A static build
    # then finishes honestly-unverifiable instead of a FALSE PASS against the UI.
    owned = {
        8000: PortOwnership(pid=111, session="disco-other-preview"),
        5173: PortOwnership(pid=222, session=None),
    }
    assert (
        resolve_preview_port(host_shared=True, owned=owned, conversation_id=CID) is None
    )


def test_process_conversation_owned_5173_is_reserved_not_targeted():
    # Even if THIS conversation somehow owns 5173, it is a reserved infra port (the
    # UI's Vite) → not a verify target. Undetectable rather than verify the UI port.
    owned = {5173: PortOwnership(pid=9, session="disco-conv_abc-preview")}
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
    assert safe not in (8000, 8800, 5173)


def test_reserved_control_ports_default_8000_8800_and_frontend_5173():
    assert reserved_control_ports() == frozenset({8000, 8800, 5173})


# ---- explicit-url validation (Bug 7 explicit-url bypass) --------------------


def test_target_url_port_parses_explicit_port_only():
    assert target_url_port("http://127.0.0.1:5173/") == 5173
    assert target_url_port("127.0.0.1:8080") == 8080
    assert target_url_port("http://localhost:3000/app") == 3000
    # no explicit port (or unparseable / empty) → None (cannot confirm a preview)
    assert target_url_port("http://127.0.0.1/") is None
    assert target_url_port("") is None
    assert target_url_port("not a url") is None


def test_explicit_target_rejected_for_reserved_or_foreign_on_shared_host():
    conv = {8080: PortOwnership(pid=9, session="disco-conv_abc-preview")}
    # reserved control/UI ports are never a valid explicit target on the shared host
    for p in (8000, 8800, 5173):
        assert not explicit_target_allowed(
            port=p, host_shared=True, owned=conv, conversation_id=CID
        )
    # a sibling-owned / unattributed / unlistening port → rejected
    foreign = {3000: PortOwnership(pid=5, session="disco-sibling1-dev")}
    assert not explicit_target_allowed(
        port=3000, host_shared=True, owned=foreign, conversation_id=CID
    )
    assert not explicit_target_allowed(
        port=4321, host_shared=True, owned={}, conversation_id=CID
    )
    assert not explicit_target_allowed(
        port=None, host_shared=True, owned=conv, conversation_id=CID
    )


def test_explicit_target_honored_for_conversation_owned_and_on_isolated():
    conv = {8080: PortOwnership(pid=9, session="disco-conv_abc-preview")}
    # this conversation's own non-reserved served port → honored
    assert explicit_target_allowed(
        port=8080, host_shared=True, owned=conv, conversation_id=CID
    )
    # isolated backend → any explicit target honored (8000 is the box's app)
    assert explicit_target_allowed(
        port=8000, host_shared=False, owned={}, conversation_id=CID
    )


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
        "vite --port 5173",  # the frontend/UI port is reserved too
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
        "vite --port 5174",                     # non-reserved (5173 IS reserved)
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


def test_reserved_port_violation_message_is_actionable():
    # Bug 16 (3a): the refusal must name the rejected port, the whole reserved set,
    # AND a concrete safe replacement so the model can recover instead of STUCKing.
    r = reserved_control_ports()
    safe = str(process_safe_preview_port(reserved=r))
    bind_msg = reserved_port_command_violation("python3 -m http.server 8000", r)
    assert bind_msg is not None
    assert "8000" in bind_msg                       # the rejected port
    for p in r:                                      # the whole reserved set
        assert str(p) in bind_msg
    assert safe in bind_msg                          # a safe replacement
    assert "refused:" in bind_msg
    kill_msg = reserved_port_command_violation("fuser -k 8000/tcp", r)
    assert kill_msg is not None and safe in kill_msg and "8000" in kill_msg


# ---- Bug 16: reserved-port preview-serve REMAP ------------------------------


def test_remap_reserved_http_server_serve_to_safe_port():
    r = reserved_control_ports()
    safe = process_safe_preview_port(reserved=r)
    # bare serve on a reserved control port → rewritten to the safe port
    out = remap_reserved_preview_serve("python3 -m http.server 8000 -d .", r)
    assert out == f"python3 -m http.server {safe} -d ."
    assert str(safe) != "8000"
    # the Vite/UI port too
    assert remap_reserved_preview_serve("python -m http.server 5173", r) == (
        f"python -m http.server {safe}"
    )


def test_remap_covers_tmux_send_keys_wrapped_serve():
    # The shape `shell_exec("preview", ...)` actually reaches the backend as.
    r = reserved_control_ports()
    safe = process_safe_preview_port(reserved=r)
    cmd = "tmux send-keys -t disco-conv_abc-preview -l 'python3 -m http.server 8000 -d .'"
    out = remap_reserved_preview_serve(cmd, r)
    assert out == (
        f"tmux send-keys -t disco-conv_abc-preview -l 'python3 -m http.server {safe} -d .'"
    )
    # and the remapped string no longer trips the containment refusal
    assert reserved_port_command_violation(out, r) is None


def test_remap_leaves_non_reserved_and_non_serve_commands_untouched():
    r = reserved_control_ports()
    # already-safe http.server port → nothing to remap
    assert remap_reserved_preview_serve("python3 -m http.server 3000", r) is None
    # a reserved KILL is NOT a serve → never remapped (left for the refusal)
    assert remap_reserved_preview_serve("fuser -k 8000/tcp", r) is None
    assert remap_reserved_preview_serve("lsof -ti:8000 | xargs kill", r) is None
    # an arbitrary reserved bind that is NOT http.server → not remapped
    assert remap_reserved_preview_serve("uvicorn app:app --port 8000", r) is None
    assert remap_reserved_preview_serve("serve -l 0.0.0.0:8000", r) is None


def test_remap_never_touches_serve_shaped_text_in_echo_print_or_quotes():
    # Bug-16 REVIEW: the rewrite must touch ONLY a genuine `python -m http.server`
    # invocation at a command position — NEVER serve-shaped TEXT the model meant to
    # run verbatim. Each of these must come back None (unchanged), so the model's
    # intended output / string literal is never silently corrupted + executed.
    r = reserved_control_ports()
    for cmd in (
        # serve form as the ARGUMENT of echo (not a command) — must run verbatim
        "echo python3 -m http.server 8000",
        "echo 'python3 -m http.server 8000'",
        'printf "%s" "python3 -m http.server 8000"',
        # serve-shaped substring inside a python -c string literal (no `-m http.server`)
        "python3 -c \"print('http.server 8000')\"",
        # a bare quoted serve-shaped string (no -m http.server invocation)
        "echo 'http.server 8000'",
        # a shell comment that mentions the serve form
        "# run python3 -m http.server 8000 to preview",
        "ls  # python3 -m http.server 8000",
        # cat of a literal mentioning the serve form
        "cat <<'EOF'\npython3 -m http.server 8000\nEOF",
    ):
        assert remap_reserved_preview_serve(cmd, r) is None, cmd


def test_remap_matches_real_serve_at_command_positions():
    # The genuine serve invocation IS remapped at each real command position:
    # at the start, after a shell separator, and via the tmux `-l` preview literal.
    r = reserved_control_ports()
    safe = process_safe_preview_port(reserved=r)
    assert remap_reserved_preview_serve("python3 -m http.server 8000", r) == (
        f"python3 -m http.server {safe}"
    )
    # after `&&` — only the serve's port is rewritten, the `cd` prefix is preserved
    assert remap_reserved_preview_serve("cd build && python3 -m http.server 8000", r) == (
        f"cd build && python3 -m http.server {safe}"
    )
    # an absolute python path still counts as a real invocation
    assert remap_reserved_preview_serve("/usr/bin/python3 -m http.server 5173", r) == (
        f"/usr/bin/python3 -m http.server {safe}"
    )


def test_remap_then_resolver_targets_the_conversation_owned_safe_port():
    # The full Bug-16 chain: remap → the safe port is served in disco-{cid8}-preview →
    # conversation-owned → resolve_preview_port(host_shared=True) targets it (never 8000).
    r = reserved_control_ports()
    out = remap_reserved_preview_serve("python3 -m http.server 8000", r)
    safe = process_safe_preview_port(reserved=r)
    assert out == f"python3 -m http.server {safe}"
    assert safe not in r
    owned = {safe: PortOwnership(pid=4242, session="disco-conv_abc-preview")}
    assert (
        resolve_preview_port(host_shared=True, owned=owned, conversation_id=CID) == safe
    )
