"""BP-G9 — track + expose/proxy EACH agent-launched USER_PORT server, not
just the single 'preview'. Multi-service builds (API + frontend) are
first-class.

The previous behavior was that the session only managed ONE preview session
on ONE port (the static `python3 -m http.server` on 8000). For multi-service
builds (an API on 3000 + a Vite dev server on 5173, for example), the agent
had to use `shell_exec` for each one and rely on C3's implicit recording —
and there was no session-level visibility into "what's exposed on this box
right now?". This file pins the contract for the fix:

- `ensure_service(name, port, command)` starts + tracks a USER_PORT service.
- Multiple `ensure_service` calls register multiple entries.
- The legacy `ensure_preview(port)` still works (single-service path
  unchanged).
- `ensure_service` REFUSES to track a non-USER_PORT (containment: a port
  outside the curated set must NEVER become a tracked/exposed URL).
- `tracked_services()` and `tracked_ports()` are read-only views for
  the UI/runtime.
- The session-level tracking is independent of C3's `_persistent_servers`
  (C3's rematerialize hook is tested in test_rematerialize_servers.py;
  C5's memory hook in test_c5_pmx_memory_persistence.py — we do NOT
  disturb them here).

Tests use fakes only — no real tmux, no real containers, no real /proc.
The `ShellSessionManager.exec()` call inside `ensure_service` is stubbed
out (we are testing the SESSION-level tracking contract, not the tmux
plumbing — the tmux plumbing has its own test file).
"""

from __future__ import annotations

import json

import pytest
from disco.tools.sandbox.base import (
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxSpec,
)
from disco.tools.sandbox.session import SandboxSession, TrackedService
from disco.tools.sandbox.shell_sessions import ExecOutcome

# ---------------------------------------------------------------------------
# Test fakes — in-memory, no tmux, no real /proc
# ---------------------------------------------------------------------------


class _MultiSvcInstance:
    """In-memory `SandboxInstance` for the multi-service test.

    - `exec_shell` returns canned output for the port_owner probe (so a
      test can decide whether a port is "free" or "already bound"),
      "tmp/fake-ws" for `pwd`, and "" for everything else.
    - `expose_port` returns a deterministic URL for any USER_PORT, None
      otherwise (mirroring the production gate in `_container.py`).
    """

    id = "fake-multi"
    owner_id = "local"
    conversation_id = "conv-multi"
    spec = SandboxSpec()

    def __init__(self) -> None:
        self._destroyed = False
        # If a port appears here, `exec_shell("python3 -c ...port_owner...")
        # will return a JSON list with a pid for it. Used by the
        # "port-already-bound" test.
        self.bound_ports: dict[int, int] = {}

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        # port_owner probe: a JSON list of {port, pid, ...} entries.
        if "python3 -c" in cmd and "import json" in cmd:
            entries = [
                {"port": p, "pid": pid, "cmdline": "node", "session": "agent"}
                for p, pid in self.bound_ports.items()
            ]
            return ExecResult(exit_code=0, stdout=json.dumps(entries), stderr="")
        if cmd.strip() == "pwd":
            return ExecResult(exit_code=0, stdout="/tmp/fake-ws", stderr="")
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def read_file(self, path: str) -> bytes:
        return b""

    async def write_file(self, path: str, data: bytes) -> None:
        pass

    async def list_dir(self, path: str) -> list[str]:
        return []

    def display_url(self) -> str | None:
        return None

    def expose_port(self, port: int) -> str | None:
        from disco.tools.sandbox._container import USER_PORTS

        if port in USER_PORTS:
            return f"http://fake-host:{port}"
        return None

    async def destroy(self) -> None:
        self._destroyed = True


class _MultiSvcService:
    """A `SandboxService` whose `create()` returns a fresh `_MultiSvcInstance`
    each call. Used for tests that need to track instances across recreates."""

    name = "fake-multi"

    def __init__(self) -> None:
        self.created: list[SandboxInstance] = []

    async def create(self, spec, *, owner_id, conversation_id):
        inst = _MultiSvcInstance()
        inst.owner_id = owner_id
        inst.conversation_id = conversation_id
        self.created.append(inst)
        return inst

    async def get(self, instance_id):
        return None


def _stub_sessions_exec(monkeypatch, running: bool = True) -> list[tuple]:
    """Replace `ShellSessionManager.exec` with an AsyncMock that records
    every (name, command, exec_dir) call. The default outcome is
    `running=True` (mirroring a real long-running dev server) so any
    downstream C3-style recording would still fire — though for THIS
    test file we only assert on the session-level `_tracked_services`
    registry, not the C3 one.

    Speeds tests up by skipping the actual tmux dance (which is tested
    end-to-end in `test_rematerialize_servers.py`).
    """
    calls: list[tuple[str, str, str | None]] = []

    async def _fake(self, name, command, exec_dir):
        calls.append((name, command, exec_dir))
        return ExecOutcome(running=running, exit_code=None, output="")

    monkeypatch.setattr(
        "disco.tools.sandbox.shell_sessions.ShellSessionManager.exec",
        _fake,
    )
    return calls


# ---------------------------------------------------------------------------
# 1) Single-service path is unchanged (legacy `ensure_preview` still works)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_preview_legacy_path_still_works(monkeypatch):
    """The original `ensure_preview(port=PREVIEW_PORT)` still starts the
    static preview on the requested port, registers it in
    `_tracked_services`, and returns True. No regression for the
    single-service path."""
    exec_calls = _stub_sessions_exec(monkeypatch)
    svc = _MultiSvcService()
    session = SandboxSession(svc, conversation_id="conv-multi-single")
    try:
        # ensure_preview defaults to PREVIEW_PORT (8000)
        result = await session.ensure_preview()
        assert result is True
        # Tracked in `_tracked_services` under port 8000.
        assert 8000 in session._tracked_services
        tracked = session._tracked_services[8000]
        assert tracked.name == "preview"
        assert "http.server" in tracked.command
        assert "8000" in tracked.command
        # Exposed via `expose_port` (the production URL gate).
        assert session.expose_port(8000) == "http://fake-host:8000"
        # The accessors agree.
        svcs = session.tracked_services()
        assert len(svcs) == 1
        assert svcs[0].port == 8000
        assert session.tracked_ports() == [8000]
        # The session actually issued the static http.server command.
        assert any("http.server" in cmd for _, cmd, _ in exec_calls), exec_calls
    finally:
        await session.destroy()


@pytest.mark.asyncio
async def test_ensure_preview_non_default_port_tracks_it(monkeypatch):
    """`ensure_preview(port=N)` tracks port N, not always 8000. The
    single-service path supports ANY USER_PORT (not just the default
    preview port) — that flexibility was always there in the signature,
    but the tracking is BP-G9's new addition."""
    _stub_sessions_exec(monkeypatch)
    svc = _MultiSvcService()
    session = SandboxSession(svc, conversation_id="conv-multi-nondef")
    try:
        result = await session.ensure_preview(port=4321)
        assert result is True
        assert 4321 in session._tracked_services
        assert session.expose_port(4321) == "http://fake-host:4321"
        # And 8000 is NOT tracked (the user asked for 4321 only).
        assert 8000 not in session._tracked_services
    finally:
        await session.destroy()


# ---------------------------------------------------------------------------
# 2) Multi-service: two USER_PORT servers both tracked + exposed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_user_port_services_both_tracked_and_exposed(monkeypatch):
    """BP-G9 acceptance: two servers on two USER_PORTs (e.g. API on 3000 +
    frontend on 5173) → both tracked, both exposed. The single 'preview'
    special case is replaced by a general multi-service registry."""
    _stub_sessions_exec(monkeypatch)
    svc = _MultiSvcService()
    session = SandboxSession(svc, conversation_id="conv-multi-two")
    try:
        # API service on port 3000.
        api_url = await session.ensure_service(
            "api", 3000, "uvicorn main:app --port 3000", exec_dir="/app"
        )
        assert api_url == "http://fake-host:3000", (
            f"ensure_service must return the exposed URL on success; got {api_url!r}"
        )
        # Frontend service on port 5173.
        web_url = await session.ensure_service("web", 5173, "vite --port 5173", exec_dir="/web")
        assert web_url == "http://fake-host:5173"

        # Both tracked in `_tracked_services`.
        tracked = session.tracked_services()
        assert len(tracked) == 2, f"expected 2 tracked services, got {tracked}"
        ports = {s.port for s in tracked}
        assert ports == {3000, 5173}, f"expected ports {{3000, 5173}}, got {ports}"

        # Each entry has the right name + command + exec_dir.
        api = next(s for s in tracked if s.port == 3000)
        web = next(s for s in tracked if s.port == 5173)
        assert api.name == "api"
        assert "uvicorn" in api.command
        assert api.exec_dir == "/app"
        assert web.name == "web"
        assert "vite" in web.command
        assert web.exec_dir == "/web"

        # Both exposed via `session.expose_port(port)` — the same gate the
        # host_proxy uses to wire the browser through to the in-box server.
        assert session.expose_port(3000) == "http://fake-host:3000"
        assert session.expose_port(5173) == "http://fake-host:5173"

        # `tracked_ports()` is sorted (deterministic, useful for UI/tests).
        assert session.tracked_ports() == [3000, 5173]
    finally:
        await session.destroy()


# ---------------------------------------------------------------------------
# 3) ensure_preview + ensure_service coexist (mixed multi-service)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_preview_and_ensure_service_coexist(monkeypatch):
    """The static auto-preview (port 8000) plus an agent-launched API
    (port 3000) coexist in the same `_tracked_services` registry. Neither
    shadows the other; both are exposed independently."""
    _stub_sessions_exec(monkeypatch)
    svc = _MultiSvcService()
    session = SandboxSession(svc, conversation_id="conv-multi-mix")
    try:
        await session.ensure_preview(8000)
        url = await session.ensure_service("api", 3000, "uvicorn --port 3000", exec_dir="/app")
        assert url == "http://fake-host:3000"

        # Both tracked, both exposed, names distinct.
        svcs = {s.port: s.name for s in session.tracked_services()}
        assert svcs == {8000: "preview", 3000: "api"}, svcs
        assert session.tracked_ports() == [3000, 8000]
        assert session.expose_port(8000) == "http://fake-host:8000"
        assert session.expose_port(3000) == "http://fake-host:3000"
    finally:
        await session.destroy()


# ---------------------------------------------------------------------------
# 4) ensure_service refuses non-USER_PORT (containment)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_service_refuses_non_user_port(monkeypatch):
    """A port outside `USER_PORTS` must NEVER become a tracked service —
    the same gate `expose_port` enforces (a non-curated port has no
    published mapping, so no URL is handable). We refuse at the
    REGISTRATION point, not at exposure time, so the mistake is caught
    early. Containment by construction, not by after-the-fact filtering."""
    _stub_sessions_exec(monkeypatch)
    svc = _MultiSvcService()
    session = SandboxSession(svc, conversation_id="conv-multi-refuse")
    try:
        with pytest.raises(SandboxError) as excinfo:
            await session.ensure_service("rogue", 9999, "python3 -m rogue --port 9999")
        assert "not in USER_PORTS" in str(excinfo.value), (
            f"error should name the gate ('USER_PORTS'); got: {excinfo.value}"
        )
        # Nothing was tracked.
        assert 9999 not in session._tracked_services
        assert session.tracked_ports() == []
        # The internal-port (8899) is also rejected — even though the
        # backend knows about it, it must NEVER be exposed as a user URL.
        with pytest.raises(SandboxError):
            await session.ensure_service("sneak", 8899, "internal-svc")
    finally:
        await session.destroy()


# ---------------------------------------------------------------------------
# 5) ensure_service politely backs off when the port is already bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_service_polite_backoff_when_port_taken(monkeypatch):
    """If a USER_PORT is already bound on the box (e.g. the agent's own
    dev server, or another tracker's service), `ensure_service` records
    the INTENT but does NOT fight the current owner — same polite
    backoff as `ensure_preview` (BP-02's "no hidden supervisor
    resurrects" rule). Returns None instead of a URL.

    The intent IS recorded so the wake machinery can see "wanted a
    service on this port" — and the C3 rehydrate skip-check will
    short-circuit on a recreate (the port is already bound on the
    fresh box, so the re-issue is a no-op)."""
    from disco.tools.sandbox._container import USER_PORTS

    class _TakenPortInstance(_MultiSvcInstance):
        """Same as `_MultiSvcInstance` but port 3000 is permanently bound."""

        def __init__(self) -> None:
            super().__init__()
            self.bound_ports[3000] = 99999  # fake pid

    class _TakenPortService(_MultiSvcService):
        async def create(self, spec, *, owner_id, conversation_id):
            inst = _TakenPortInstance()
            inst.owner_id = owner_id
            inst.conversation_id = conversation_id
            self.created.append(inst)
            return inst

    _stub_sessions_exec(monkeypatch)
    svc = _TakenPortService()
    session = SandboxSession(svc, conversation_id="conv-multi-backoff")
    try:
        url = await session.ensure_service("api", 3000, "uvicorn --port 3000", exec_dir="/app")
        # Returns None — port was taken, no fight.
        assert url is None
        # Intent IS recorded (so wake machinery sees "wanted a service on 3000").
        assert 3000 in session._tracked_services
        assert session._tracked_services[3000].name == "api"
        # And 3000 is a USER_PORT, so the gate isn't the issue.
        assert 3000 in USER_PORTS
    finally:
        await session.destroy()


# ---------------------------------------------------------------------------
# 6) tracked_services accessor is read-only (returns a fresh list)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracked_services_returns_fresh_list(monkeypatch):
    """`tracked_services()` returns a fresh list on every call — mutating
    the snapshot does NOT affect session state (defense against an
    accidental UI/runtime that sorts / filters in place)."""
    _stub_sessions_exec(monkeypatch)
    svc = _MultiSvcService()
    session = SandboxSession(svc, conversation_id="conv-multi-readonly")
    try:
        await session.ensure_service("a", 3000, "cmd-a")
        snap1 = session.tracked_services()
        assert len(snap1) == 1
        # Mutate the snapshot in place.
        snap1.clear()
        snap1.append(TrackedService(name="bogus", port=9999, command="x", exec_dir=None))
        # Session state is unchanged.
        snap2 = session.tracked_services()
        assert len(snap2) == 1
        assert snap2[0].port == 3000
        # And the bogus entry didn't sneak in.
        assert all(s.port != 9999 for s in snap2)
    finally:
        await session.destroy()


# ---------------------------------------------------------------------------
# 7) tracked_services survives a recreate (C3 wake contract)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracked_services_survive_recreate(monkeypatch):
    """BP-G9 wake contract: the explicit `_tracked_services` registry
    SURVIVES `_recreate` (the C3 rematerialize hook re-issues every
    service on the fresh box). The metadata is the same data the
    `rehydrate_persistent_servers` C3 hook uses — but exposed through a
    session-level API for visibility.

    This is the analog of `test_session_recreate_triggers_rehydrate`
    (C3) for the EXPLICIT registration path. We do NOT assert that the
    commands are re-issued here (that's the C3 test's job) — just that
    the metadata persists across the recreate boundary.
    """
    _stub_sessions_exec(monkeypatch)
    svc = _MultiSvcService()
    session = SandboxSession(svc, conversation_id="conv-multi-recreate")
    try:
        # Track two services BEFORE the recreate.
        await session.ensure_service("api", 3000, "uvicorn --port 3000")
        await session.ensure_service("web", 5173, "vite --port 5173")
        before_ports = session.tracked_ports()
        assert sorted(before_ports) == [3000, 5173]
        before_names = {s.port: s.name for s in session.tracked_services()}
        assert before_names == {3000: "api", 5173: "web"}

        # Drive a recreate: the first instance is now "dead", the service
        # has a fresh one queued up (added by the test's stub_create).
        first_inst = await session._ensure()
        assert first_inst is svc.created[0]

        # Force the recreate — this is exactly what `_recreate` does when
        # the session catches a typed `SandboxUnavailableError` from the
        # instance. The new instance is what the service hands back.
        await session._recreate(first_inst)
        new_inst = session._instance
        assert new_inst is not first_inst
        assert new_inst is svc.created[1]

        # The tracked services SURVIVE the recreate. Names + commands +
        # ports all preserved. (The C3 hook re-issues them on the fresh
        # box; that re-issue is tested in test_rematerialize_servers.py.)
        after_ports = session.tracked_ports()
        assert sorted(after_ports) == [3000, 5173], (
            f"tracked ports lost across recreate: before={before_ports} after={after_ports}"
        )
        after_names = {s.port: s.name for s in session.tracked_services()}
        assert after_names == {3000: "api", 5173: "web"}
    finally:
        await session.destroy()


# ---------------------------------------------------------------------------
# 8) `_tracked_services` is a fresh dict per session (no cross-talk)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracked_services_isolated_per_session(monkeypatch):
    """Two `SandboxSession`s sharing a service have INDEPENDENT
    `_tracked_services` registries. Registering a service in one does
    not leak into the other (the dict is per-instance, not a class
    attribute — defense against a copy-paste refactor that promotes it
    to a class attr)."""
    _stub_sessions_exec(monkeypatch)
    svc = _MultiSvcService()
    a = SandboxSession(svc, conversation_id="conv-a")
    b = SandboxSession(svc, conversation_id="conv-b")
    try:
        await a.ensure_service("api-a", 3000, "uvicorn --port 3000")
        # a has 3000; b has nothing.
        assert a.tracked_ports() == [3000]
        assert b.tracked_ports() == []
        await b.ensure_service("api-b", 5173, "vite --port 5173")
        # Each is isolated.
        assert a.tracked_ports() == [3000]
        assert b.tracked_ports() == [5173]
    finally:
        await a.destroy()
        await b.destroy()
