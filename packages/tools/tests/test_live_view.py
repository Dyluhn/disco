"""P2 tests: daemon headed/headless selection logic and live_view ensure_live."""
from unittest.mock import MagicMock, patch


def test_browser_state_headless_by_default():
    """BrowserState.start() with no display arg → headless=True (the default)."""
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_pw = MagicMock()
    mock_pw.chromium.launch.return_value = mock_browser
    mock_browser.new_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page
    mock_sp_instance = MagicMock()
    mock_sp_instance.start.return_value = mock_pw

    import disco.tools.builtin._browser_daemon as daemon_mod

    with patch.object(daemon_mod, "sync_playwright", return_value=mock_sp_instance):
        state = daemon_mod.BrowserState()
        state.start()
        # Must be headless=True
        mock_pw.chromium.launch.assert_called_once()
        call_kwargs = mock_pw.chromium.launch.call_args
        assert call_kwargs.kwargs.get("headless", True) is True


def test_browser_state_headed_when_display_given():
    """BrowserState.start(display=':1') → headless=False."""
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_pw = MagicMock()
    mock_pw.chromium.launch.return_value = mock_browser
    mock_browser.new_context.return_value = mock_context
    mock_context.new_page.return_value = mock_page
    mock_sp_instance = MagicMock()
    mock_sp_instance.start.return_value = mock_pw

    import disco.tools.builtin._browser_daemon as daemon_mod

    with patch.object(daemon_mod, "sync_playwright", return_value=mock_sp_instance):
        state = daemon_mod.BrowserState()
        state.start(display=":1")
        call_kwargs = mock_pw.chromium.launch.call_args
        assert call_kwargs.kwargs.get("headless") is False


def test_live_view_ensure_live_calls_all_three():
    """ensure_live() calls ensure_xvfb, ensure_x11vnc, and ensure_websockify."""
    import disco.tools.builtin.live_view as lv_mod

    with patch.object(lv_mod, "ensure_xvfb", return_value=True) as mock_xvfb, \
         patch.object(lv_mod, "ensure_x11vnc", return_value=True) as mock_vnc, \
         patch.object(lv_mod, "ensure_websockify", return_value=True) as mock_ws, \
         patch.object(lv_mod, "_start_watchdog"):
        result = lv_mod.ensure_live()
        assert result is True
        mock_xvfb.assert_called_once()
        mock_vnc.assert_called_once()
        mock_ws.assert_called_once()


# ---------------------------------------------------------------------------
# Security-hardening regression guards (the gpt-5.5 BLOCK findings).
# ---------------------------------------------------------------------------


def _reset_state():
    import disco.tools.builtin.live_view as lv_mod

    for k in lv_mod._state:
        lv_mod._state[k] = None


def test_ensure_live_tears_down_on_partial_failure():
    """B4/fail-closed: if any leg fails, ensure_live() must NOT leave a half-open
    stack — it tears down what started and returns False."""
    import disco.tools.builtin.live_view as lv_mod

    _reset_state()
    with patch.object(lv_mod, "ensure_xvfb", return_value=True), \
         patch.object(lv_mod, "ensure_x11vnc", return_value=True), \
         patch.object(lv_mod, "ensure_websockify", return_value=False), \
         patch.object(lv_mod, "teardown") as mock_teardown, \
         patch.object(lv_mod, "_start_watchdog") as mock_wd:
        result = lv_mod.ensure_live()
        assert result is False
        mock_teardown.assert_called_once()   # never leave Xvfb-only up
        mock_wd.assert_not_called()           # no watchdog for a stack that didn't come up


def test_port_listening_detects_real_listener_including_loopback_only():
    """The bind probe must see a REAL listener — including one bound to loopback
    only (exactly how x11vnc binds): a 0.0.0.0 probe-bind overlaps every
    interface, so EADDRINUSE fires either way. And a freed port reads free."""
    import socket

    import disco.tools.builtin.live_view as lv_mod

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))  # loopback-bound, like x11vnc
        srv.listen(1)
        port = srv.getsockname()[1]
        assert lv_mod._port_listening(port) is True
    finally:
        srv.close()
    # No connection was ever made, so no TIME_WAIT — the freed port reads free.
    assert lv_mod._port_listening(port) is False


def test_port_listening_uses_no_external_binary():
    """Regression (found live 2026-07-09): the old lsof-based probe returned False
    when lsof was MISSING from the sandbox image — silently DISARMING the
    foreign-listener fail-closed gate. The probe must be pure-stdlib: it may not
    shell out at all, so image drift can never neuter it again."""
    import disco.tools.builtin.live_view as lv_mod

    with patch.object(
        lv_mod.subprocess, "run", side_effect=AssertionError("probe must not shell out")
    ), patch.object(
        lv_mod.subprocess, "Popen", side_effect=AssertionError("probe must not shell out")
    ):
        # Port 1 is privileged: the bind fails (not with EADDRINUSE) and the probe
        # must report OCCUPIED — the fail-closed direction — without any subprocess.
        assert lv_mod._port_listening(1) is True


def test_x11vnc_fails_closed_on_foreign_listener():
    """B3: a VNC server we did NOT start already on 5901 → refuse, never reuse/bridge."""
    import disco.tools.builtin.live_view as lv_mod

    _reset_state()
    # No tracked handle, but the port is occupied → foreign → fail closed.
    with patch.object(lv_mod, "_port_listening", return_value=True), \
         patch.object(lv_mod, "_spawn") as mock_spawn:
        assert lv_mod.ensure_x11vnc() is False
        mock_spawn.assert_not_called()  # must NOT start/adopt anything


def test_websockify_fails_closed_on_foreign_listener():
    """B3: a foreign process already on 6080 → refuse rather than bridge to it."""
    import disco.tools.builtin.live_view as lv_mod

    _reset_state()
    with patch.object(lv_mod, "_port_listening", return_value=True), \
         patch.object(lv_mod, "_spawn") as mock_spawn:
        assert lv_mod.ensure_websockify() is False
        mock_spawn.assert_not_called()


def test_x11vnc_argv_is_loopback_and_viewonly_no_selfdaemon():
    """B3/B4: x11vnc must bind loopback, be -viewonly, and NOT use -bg (which would
    detach from our Popen handle and defeat teardown)."""
    import disco.tools.builtin.live_view as lv_mod

    _reset_state()
    fake = MagicMock()
    fake.poll.return_value = None  # alive
    with patch.object(lv_mod, "_port_listening", return_value=False), \
         patch.object(lv_mod, "_spawn", return_value=fake) as mock_spawn, \
         patch.object(lv_mod.time, "sleep"):
        assert lv_mod.ensure_x11vnc() is True
    argv = mock_spawn.call_args.args[0]
    assert "-localhost" in argv
    assert "-viewonly" in argv
    assert "-bg" not in argv  # self-daemonising flag removed
    assert f"{lv_mod.VNC_PORT}" in argv


def test_websockify_argv_binds_container_iface_not_loopback():
    """MAJOR: websockify must bind 0.0.0.0 (container interface) so the published-port
    mapping can reach it; loopback would be unreachable through Docker/Podman publish.
    And NO --daemon (would orphan the real bridge from our handle)."""
    import disco.tools.builtin.live_view as lv_mod

    _reset_state()
    fake = MagicMock()
    fake.poll.return_value = None
    with patch.object(lv_mod, "_port_listening", return_value=False), \
         patch.object(lv_mod, "_spawn", return_value=fake) as mock_spawn, \
         patch.object(lv_mod.time, "sleep"):
        assert lv_mod.ensure_websockify() is True
    argv = mock_spawn.call_args.args[0]
    assert f"0.0.0.0:{lv_mod.NOVNC_PORT}" in argv          # container iface, not 127.0.0.1
    assert f"127.0.0.1:{lv_mod.VNC_PORT}" in argv          # bridges to the loopback x11vnc
    assert "--daemon" not in argv
    assert "--web" in argv


def test_watchdog_tears_down_on_partial_death():
    """A child can die while the others stay alive. is_live() then goes False, but the
    survivors must NOT be left orphaned — the watchdog tears the partial stack down."""
    import disco.tools.builtin.live_view as lv_mod

    _reset_state()
    dead = MagicMock()
    dead.poll.return_value = 0  # exited
    alive1 = MagicMock()
    alive1.poll.return_value = None  # still running
    alive2 = MagicMock()
    alive2.poll.return_value = None
    lv_mod._state["xvfb"] = alive1
    lv_mod._state["x11vnc"] = alive2
    lv_mod._state["websockify"] = dead
    lv_mod._watchdog_stop.clear()

    with patch.object(lv_mod, "_WATCHDOG_TICK_S", 0.01), \
         patch.object(lv_mod, "teardown") as mock_teardown:
        lv_mod._watchdog_loop()  # one tick → sees not-is-live + a survivor → teardown
    mock_teardown.assert_called_once()
    _reset_state()


def test_watchdog_no_teardown_when_all_dead():
    """If ALL children are already gone there is nothing to reap — the watchdog just
    exits (teardown would be a redundant no-op; we avoid signalling dead groups)."""
    import disco.tools.builtin.live_view as lv_mod

    _reset_state()
    for k in lv_mod._state:
        p = MagicMock()
        p.poll.return_value = 0
        lv_mod._state[k] = p
    lv_mod._watchdog_stop.clear()

    with patch.object(lv_mod, "_WATCHDOG_TICK_S", 0.01), \
         patch.object(lv_mod, "teardown") as mock_teardown:
        lv_mod._watchdog_loop()
    mock_teardown.assert_not_called()
    _reset_state()


def test_start_watchdog_is_restart_safe_after_teardown():
    """Round-4 race: teardown() leaves _watchdog_stop SET. A restart (close→reopen)
    must CLEAR it and start a fresh, running watchdog — else the new stack has no idle
    / partial-death reaper at all."""
    import disco.tools.builtin.live_view as lv_mod

    _reset_state()
    # Make the stack look LIVE and recently-active so the fresh watchdog stays in its
    # loop (it must NOT legitimately exit via partial-death or idle during the asserts —
    # that was the timing-flake the review caught with an empty _state).
    for k in lv_mod._state:
        p = MagicMock()
        p.poll.return_value = None  # alive
        lv_mod._state[k] = p
    lv_mod._last_active = lv_mod.time.monotonic()  # fresh → not idle
    # Simulate the post-teardown residue: stop flag set, no tracked thread.
    lv_mod._watchdog = None
    lv_mod._watchdog_stop.set()

    with patch.object(lv_mod, "_WATCHDOG_TICK_S", 0.01):
        lv_mod._start_watchdog()
        try:
            # The stop flag must be cleared so the fresh loop actually runs.
            assert lv_mod._watchdog_stop.is_set() is False
            assert lv_mod._watchdog is not None and lv_mod._watchdog.is_alive()
        finally:
            # Clean up the fresh daemon thread.
            lv_mod._watchdog_stop.set()
            if lv_mod._watchdog is not None:
                lv_mod._watchdog.join(timeout=1)
    _reset_state()


def test_touch_resets_idle_timer():
    """The heartbeat: touch() must advance the last-activity timestamp so the idle
    watchdog does not reap an actively-watched session."""
    import disco.tools.builtin.live_view as lv_mod

    lv_mod._last_active = 0.0
    lv_mod.touch()
    assert lv_mod._last_active > 0.0


def test_teardown_kills_process_groups():
    """B4: teardown must kill the process GROUP of each child (our start_new_session
    children), so it actually reaps the server instead of no-op'ing on a dead parent."""
    import disco.tools.builtin.live_view as lv_mod

    _reset_state()
    procs = {}
    for k in ("xvfb", "x11vnc", "websockify"):
        p = MagicMock()
        p.pid = 1000 + len(procs)
        lv_mod._state[k] = p
        procs[k] = p

    with patch.object(lv_mod.os, "getpgid", side_effect=lambda pid: pid) as mock_pgid, \
         patch.object(lv_mod.os, "killpg") as mock_killpg:
        lv_mod.teardown()

    assert mock_killpg.call_count == 3          # all three groups signalled
    assert mock_pgid.call_count == 3
    # Each child is reaped (wait) — otherwise a SIGTERM'd process lingers as a zombie.
    for p in procs.values():
        p.wait.assert_called()
    # state cleared so a later ensure_* starts fresh (idempotent, no stale handles)
    assert all(lv_mod._state[k] is None for k in ("xvfb", "x11vnc", "websockify"))
