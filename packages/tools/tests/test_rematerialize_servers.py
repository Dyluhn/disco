"""C3 — re-materialize agent-launched dev servers on a sandbox recreate.

The previous behavior was that only the static `python3 -m http.server` preview
survived a mid-session death / recreate: any real app the agent launched
(vite, express, uvicorn, http.server on a non-default USER_PORT) silently
vanished on suspend/wake. This file pins the contract for the fix:

- A USER_PORT-binding shell command that's still `running` IS recorded.
- A one-shot / build command (running=False) is NOT recorded.
- On recreate, recorded entries are re-issued best-effort.
- Ports already bound on the fresh instance are SKIPPED (no duplication).
- A failed rehydrate does NOT raise (best-effort, like the auto-preview).

Tests use fakes only — no real tmux, no real containers, no real /proc probes.
"""

from __future__ import annotations

import pytest
from disco.tools.sandbox.base import (
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxService,
    SandboxSpec,
    SandboxUnavailableError,
)
from disco.tools.sandbox.shell_sessions import (
    ShellSessionManager,
)

# ---------------------------------------------------------------------------
# Test fakes — no real tmux, no real containers
# ---------------------------------------------------------------------------


class _FakeInstance:
    """In-memory `SandboxInstance` + a `cmd_log` so tests can assert on every
    `tmux send-keys` / `capture-pane` call the manager made. Same shape as
    the one in test_preview_session.py but with a public `cmd_log` and a
    `canned_outputs` map so we can drive the manager's polling loop."""

    id = "fake-sbx-c3"
    owner_id = "local"
    conversation_id = "conv-c3"
    spec = SandboxSpec()

    def __init__(self) -> None:
        self.cmd_log: list[str] = []
        self.canned_outputs: dict[str, object] = {}
        self.default_output = ""
        self.default_exit_code = 0
        # If set, every exec_shell raises this. Used to simulate death.
        self.raise_on_exec: Exception | None = None

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self.cmd_log.append(cmd)
        if self.raise_on_exec is not None:
            raise self.raise_on_exec
        # capture-pane has a special list-or-default contract used by the
        # manager's poll loop; route it through `canned_outputs["capture-pane"]`.
        if "capture-pane" in cmd and "capture-pane" in self.canned_outputs:
            val = self.canned_outputs["capture-pane"]
            if isinstance(val, list):
                if val:
                    ec, out = val.pop(0)
                else:
                    ec, out = self.canned_outputs.get("capture-pane_default", (0, ""))
            else:
                ec, out = val
            return ExecResult(exit_code=ec, stdout=out, stderr="")
        # Generic key match for has-session / new-session / send-keys / list-panes
        for k, v in self.canned_outputs.items():
            if k in cmd and k != "capture-pane":
                if isinstance(v, list):
                    if v:
                        ec, out = v.pop(0)
                    else:
                        ec, out = self.canned_outputs.get(f"{k}_default", (0, ""))
                else:
                    ec, out = v  # type: ignore[misc]
                return ExecResult(exit_code=ec, stdout=out, stderr="")
        return ExecResult(exit_code=self.default_exit_code, stdout=self.default_output, stderr="")

    async def read_file(self, path: str) -> bytes:
        return b""

    async def write_file(self, path: str, data: bytes) -> None:
        pass

    async def list_dir(self, path: str) -> list[str]:
        return []

    def display_url(self) -> str | None:
        return None

    def expose_port(self, port: int) -> str | None:
        return None

    async def destroy(self) -> None:
        pass


class _DyingInstance(_FakeInstance):
    """Fake whose first exec_shell after `die()` raises — the typed
    `SandboxUnavailableError` the session layer catches to trigger recreate."""

    def __init__(self) -> None:
        super().__init__()
        self._dead = False

    def die(self) -> None:
        self._dead = True

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        if self._dead:
            raise SandboxUnavailableError(f"{self.id} died")
        return await super().exec_shell(cmd, timeout_s=timeout_s)


class _CountingService(SandboxService):
    """A `SandboxService` whose first `create()` returns the pre-built initial
    instance and subsequent calls return fresh ones — so `_recreate` can swap
    in a new one and we can assert on it by index."""

    name = "fake-c3"

    def __init__(self, initial: _FakeInstance | None = None) -> None:
        self.created: list[_FakeInstance] = []
        # Pre-build one for the initial _ensure() call.
        self._initial: _FakeInstance = initial if initial is not None else _FakeInstance()
        self.created.append(self._initial)
        self._calls = 0

    async def create(self, spec, *, owner_id, conversation_id) -> SandboxInstance:
        self._calls += 1
        if self._calls == 1:
            return self._initial
        inst = _FakeInstance()
        self.created.append(inst)
        return inst

    async def get(self, instance_id):
        return None


async def _return(inst):
    return inst


def _fasten_polls(monkeypatch, wait_s: float = 0.1, poll_s: float = 0.02) -> None:
    """Shrink the manager's poll/wait windows so a "still running" test exits
    in <1s instead of 15s. The behavior is unchanged — exec() just decides
    "still running" sooner."""
    from disco.tools.sandbox import shell_sessions

    monkeypatch.setattr(shell_sessions, "_EXEC_WAIT_S", wait_s)
    monkeypatch.setattr(shell_sessions, _POLL_S_NAME := "_POLL_S", poll_s)


def _setup_running_capture(inst: _FakeInstance) -> None:
    """Configure `inst` so that any `tmux` invocation through the manager
    makes the command APPEAR to still be running (no PS1 marker in any
    capture-pane output)."""
    inst.canned_outputs["has-session"] = (0, "")
    inst.canned_outputs["capture-pane"] = [
        # is_busy view (during exec) — idle prompt, session is ready
        (0, "__DISCO_PS1__0__$ "),
        # pre_cap — idle prompt, used as the delta baseline
        (0, "__DISCO_PS1__0__$ "),
        # first poll post_cap — command has emitted output, no marker yet
        (0, "starting server...\n"),
    ]
    inst.canned_outputs["capture-pane_default"] = (0, "starting server...\n")


def _setup_finished_capture(inst: _FakeInstance, output_body: str = "ok") -> None:
    """Configure `inst` so the manager's first poll finds a PS1 marker
    (command has FINISHED)."""
    inst.canned_outputs["has-session"] = (0, "")
    inst.canned_outputs["capture-pane"] = [
        (0, "__DISCO_PS1__0__$ "),  # is_busy
        (0, "__DISCO_PS1__0__$ "),  # pre_cap
        (0, f"__DISCO_PS1__0__$ \n{output_body}\n__DISCO_PS1__0__$ "),  # finished
    ]


# ---------------------------------------------------------------------------
# 1) Classifier — pure unit, no instance
# ---------------------------------------------------------------------------


def test_classify_persistent_server_various_commands():
    """The detection signal is "argv contains a USER_PORT" — the only safe
    in-string check for "this command will bind a port we expose" without a
    full process-startup simulation. False positives (e.g. `echo 8000`) are
    filtered by the `running=True` gate in `exec()` — one-shots never
    record. Non-USER_PORTs (e.g. 9999) are explicitly out of scope."""
    m = ShellSessionManager(_return)  # type: ignore[arg-type]
    assert m._classify_persistent_server("python3 -m http.server 8000") == 8000
    assert m._classify_persistent_server("vite --port 5173") == 5173
    assert m._classify_persistent_server("npm run dev --port 3000") == 3000
    assert m._classify_persistent_server("npm run build") is None
    assert m._classify_persistent_server("npm run dev") is None  # no explicit port
    assert m._classify_persistent_server("curl http://localhost:3000/api") is None
    assert m._classify_persistent_server("lsof -i :8000") is None  # ':8000' is not a digit token
    assert m._classify_persistent_server("python3 -m http.server 9999") is None
    # Unbalanced quote — shlex.split raises, we fall back to a coarse split.
    # Result is unspecified; just must not raise.
    m._classify_persistent_server("echo 'unbalanced")


# ---------------------------------------------------------------------------
# 2) Recording — a still-running USER_PORT-binding command IS recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recording_persistent_server_after_running_exec(monkeypatch):
    _fasten_polls(monkeypatch)

    inst = _FakeInstance()
    _setup_running_capture(inst)
    mgr = ShellSessionManager(lambda: _return(inst))

    outcome = await mgr.exec("dev", "vite --port 5173", None)

    assert outcome.running is True
    assert "dev" in mgr._persistent_servers, (
        "a still-running USER_PORT-binding exec MUST be recorded; otherwise "
        "the rehydrate on recreate has nothing to re-issue"
    )
    recorded = mgr._persistent_servers["dev"]
    assert recorded.name == "dev"
    assert recorded.command == "vite --port 5173"
    assert recorded.port == 5173
    assert recorded.exec_dir is None


# ---------------------------------------------------------------------------
# 3) One-shot / build commands are NOT recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_shot_command_is_not_recorded(monkeypatch):
    _fasten_polls(monkeypatch)

    inst = _FakeInstance()
    _setup_finished_capture(inst, output_body="build complete")
    mgr = ShellSessionManager(lambda: _return(inst))

    outcome = await mgr.exec("build", "npm run build", None)

    assert outcome.running is False
    assert "build" not in mgr._persistent_servers, (
        "a FINISHED exec MUST NOT be recorded — re-running `npm run build` "
        "on a recreate would be wrong (a build is a one-shot, not a server)"
    )


@pytest.mark.asyncio
async def test_finished_command_with_a_user_port_in_argv_is_not_recorded(monkeypatch):
    """Even if the command line contains a USER_PORT, if exec() returned
    running=False, we DON'T record. `curl --connect-timeout 1 127.0.0.1:8000`
    could be a one-shot ping and the running gate is what saves us."""
    _fasten_polls(monkeypatch)

    inst = _FakeInstance()
    _setup_finished_capture(inst, output_body="hi")
    mgr = ShellSessionManager(lambda: _return(inst))

    # argv literally has "8000" as a token, but exec returns finished.
    outcome = await mgr.exec("ping", "nc -zv 127.0.0.1 8000", None)
    assert outcome.running is False
    assert "ping" not in mgr._persistent_servers


# ---------------------------------------------------------------------------
# 4) Stale entry dropped when a session is replaced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_entry_replaced_when_session_gets_new_command(monkeypatch):
    """The agent first launches `vite --port 5173` (recorded), then kills it
    and starts `npm run build` (finished, not recorded). The old entry MUST be
    dropped — otherwise on recreate we'd try to restart a server the user
    intentionally shut down."""
    _fasten_polls(monkeypatch)

    inst = _FakeInstance()
    mgr = ShellSessionManager(lambda: _return(inst))

    # First exec — vite, still running → recorded.
    _setup_running_capture(inst)
    await mgr.exec("dev", "vite --port 5173", None)
    assert mgr._persistent_servers["dev"].port == 5173

    # Second exec in the same session — build, finished → entry cleared.
    _setup_finished_capture(inst, output_body="built")
    await mgr.exec("dev", "npm run build", None)
    assert "dev" not in mgr._persistent_servers, (
        "a FINISHED exec in a previously-recorded session must drop the stale "
        "entry — the server is no longer running"
    )


# ---------------------------------------------------------------------------
# 5) Rehydrate — re-issues the recorded command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rehydrate_reissues_recorded_command(monkeypatch):
    _fasten_polls(monkeypatch)

    inst = _FakeInstance()
    mgr = ShellSessionManager(lambda: _return(inst))

    # Record a dev server.
    _setup_running_capture(inst)
    await mgr.exec("dev", "vite --port 5173", None)

    # Simulate the recreate: clear cmd_log + reset the capture-pane list so
    # the rehydrate's exec() also sees a "still running" outcome.
    inst.cmd_log.clear()
    _setup_running_capture(inst)

    async def _port_free(_port: int) -> bool:
        return False  # nothing bound → rehydrate MUST re-issue

    logs = await mgr.rehydrate_persistent_servers(port_check=_port_free)

    # The rehydrate re-issued the command — there should be a fresh
    # `tmux send-keys ... vite --port 5173` call on the instance.
    send_keys = [c for c in inst.cmd_log if "send-keys" in c and "vite" in c]
    assert send_keys, f"rehydrate did not re-issue the recorded command. cmd_log={inst.cmd_log}"
    # And the entry is refreshed (re-recorded by the re-issued exec).
    assert mgr._persistent_servers["dev"].port == 5173
    # The log line is human-readable + includes the session name + command.
    assert any("re-materialized" in line and "'dev'" in line for line in logs), logs


# ---------------------------------------------------------------------------
# 6) Rehydrate — skips when the port is already bound (no duplication)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rehydrate_skips_when_port_already_bound(monkeypatch):
    _fasten_polls(monkeypatch)

    inst = _FakeInstance()
    mgr = ShellSessionManager(lambda: _return(inst))

    _setup_running_capture(inst)
    await mgr.exec("dev", "vite --port 5173", None)

    inst.cmd_log.clear()

    async def _port_taken(_port: int) -> bool:
        return True  # 5173 is owned on the fresh instance — DO NOT re-issue

    logs = await mgr.rehydrate_persistent_servers(port_check=_port_taken)

    send_keys = [c for c in inst.cmd_log if "send-keys" in c]
    assert not send_keys, (
        f"rehydrate MUST NOT re-issue when the port is already bound, but issued: {send_keys}"
    )
    assert any("already bound" in line for line in logs), logs


# ---------------------------------------------------------------------------
# 7) Rehydrate — does NOT raise on a failed restart (best-effort)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rehydrate_does_not_raise_on_failed_restart(monkeypatch):
    _fasten_polls(monkeypatch)

    inst = _FakeInstance()
    mgr = ShellSessionManager(lambda: _return(inst))

    _setup_running_capture(inst)
    await mgr.exec("dev", "vite --port 5173", None)

    # Now make the next exec_shell blow up (e.g. the fresh instance's tmux
    # has a transient hiccup). The rehydrate must catch and continue.
    inst.raise_on_exec = RuntimeError("tmux not ready")

    async def _port_free(_port: int) -> bool:
        return False

    # The key invariant: no exception bubbles out.
    logs = await mgr.rehydrate_persistent_servers(port_check=_port_free)

    assert any("failed to re-materialize" in line for line in logs), logs


# ---------------------------------------------------------------------------
# 8) End-to-end — SandboxSession._recreate calls rehydrate_persistent_servers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_recreate_invokes_persistent_rehydrate_exactly_once(monkeypatch):
    """H321: one sandbox generation change cannot replay server intent twice."""

    from disco.tools.sandbox.session import SandboxSession

    initial = _FakeInstance()
    session = SandboxSession(
        _CountingService(initial=initial), conversation_id="conv-c3-rehydrate-once"
    )

    async def _noop_preview(port: int = 8000) -> bool:
        return False

    session.ensure_preview = _noop_preview  # type: ignore[method-assign]
    await session._ensure()
    calls = 0

    async def _count_rehydrate(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(session.sessions, "rehydrate_persistent_servers", _count_rehydrate)

    await session._recreate(initial)

    assert calls == 1
    assert session._generation == 2
    await session.destroy()


@pytest.mark.asyncio
async def test_session_recreate_triggers_rehydrate(monkeypatch):
    """The wiring test: a `SandboxSession` that has recorded a USER_PORT-
    binding dev server, on `_recreate`, RE-ISSUES that command on the new
    instance. This is the contract C3 ships — supersedes the static-only
    preview path for any server that actually owned a USER_PORT."""
    _fasten_polls(monkeypatch)

    from disco.tools.sandbox.session import SandboxSession

    initial = _DyingInstance()
    svc = _CountingService(initial=initial)
    # The initial instance behaves as running + has the dev server running.
    _setup_running_capture(initial)

    session = SandboxSession(svc, conversation_id="conv-c3-recreate")

    # Speed up the auto-preview task so it doesn't race the assertion.
    async def _noop_preview(port: int = 8000) -> bool:  # type: ignore[no-redef]
        return False

    session.ensure_preview = _noop_preview  # type: ignore[method-assign]

    # Trigger first create.
    await session._ensure()
    assert session._instance is initial

    # Launch a USER_PORT-binding dev server — must be recorded.
    outcome = await session.sessions.exec("dev", "vite --port 5173", None)
    assert outcome.running is True
    assert "dev" in session.sessions._persistent_servers
    assert session.sessions._persistent_servers["dev"].port == 5173

    # Simulate a death: the next exec_shell on the current instance raises.
    # Then a tool call goes through `_resilient`, which catches the typed
    # death and calls `_recreate`. The fresh instance must RE-ISSUE the
    # recorded dev command (best-effort, non-fatal).
    initial.die()

    # Trigger the recreate by routing one resilient op through the session.
    try:
        await session._resilient(lambda i: i.exec_shell("true", timeout_s=5))
    except SandboxError:
        pass  # _resilient converts the death to a SandboxError for the loop

    # The new instance is whatever the session is now bound to.
    new_inst = session._instance
    assert new_inst is not initial
    # Configure its canned outputs so the rehydrate's exec() also looks "running".
    _setup_running_capture(new_inst)
    # The rehydrate already ran inside `_recreate` — re-run it with a
    # fresh canned_outputs set on the new instance so we can observe
    # the send-keys call.
    await session.sessions.rehydrate_persistent_servers(port_check=lambda _p: False)

    # The new instance received a send-keys for the rehydrated command.
    send_keys = [c for c in new_inst.cmd_log if "send-keys" in c and "vite" in c and "5173" in c]
    assert send_keys, (
        f"after _recreate, the new instance did not receive the rehydrated "
        f"dev-server command. cmd_log={new_inst.cmd_log}"
    )
    # And the entry is preserved (re-recorded by the rehydrate's re-exec).
    assert session.sessions._persistent_servers["dev"].port == 5173
