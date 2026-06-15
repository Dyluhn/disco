"""ShellSessionManager - persistent named shell sessions backed by tmux."""

import asyncio
import logging
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .base import SandboxInstance

_LOG = logging.getLogger(__name__)

_PREFIX = "disco"  # WRITE side: new tmux sessions are disco-*
_LEGACY_PREFIX = "pmx"  # READ side: still swept on teardown (no orphans across rename)
_PS1_MARKER = "__DISCO_PS1__"
_PS1 = f"{_PS1_MARKER}$?__$ "
# Derive the parse regex from the marker constant so the WRITE (PS1) and READ
# (parse) sides can never drift — the exact bug a literal `__PMX_PS1__` here
# re-introduced after the marker was renamed.
_MARKER_RE = re.compile(re.escape(_PS1_MARKER) + r"(\d+)__\$\s*$")
_VIEW_TAIL_CHARS = 10_000
_EXEC_RETURN_CHARS = 6_000
_POLL_S = 0.5
_EXEC_WAIT_S = 15.0
_TMUX_TIMEOUT_S = 10


@dataclass
class PersistentServer:
    """Metadata for one agent-launched dev server the session should try to
    RE-MATERIALIZE on a sandbox recreate (C3). A PersistentServer is recorded
    ONLY for shell sessions whose command argv contains a USER_PORT and is
    still running (i.e. a long-running server, not a one-shot build). On
    recreate, the recorded entries are re-issued best-effort so a real app
    (vite, express, uvicorn, http.server on a non-default port, …) survives
    suspend/wake — not just the static auto-preview.

    The `name` is the tmux session name (no namespace prefix), `command` is the
    full command string originally exec'd, `exec_dir` is where it was run, and
    `port` is the USER_PORT it binds (the rehydrate skip-check uses this to
    avoid duplicating a server that's already running on the fresh instance).
    """
    name: str
    command: str
    exec_dir: str | None
    port: int


class SessionBusy(Exception):
    pass


@dataclass
class ExecOutcome:
    running: bool
    exit_code: int | None
    output: str
    note: str | None = None


@dataclass
class SessionView:
    running: bool
    output: str


@dataclass
class SessionInfo:
    name: str
    busy: bool
    last_lines: str


class ShellSessionManager:
    def __init__(
        self,
        instance_getter: Callable[[], Awaitable[SandboxInstance]],
        namespace: str = ""
    ) -> None:
        self._get_instance = instance_getter
        self.namespace = namespace
        self._known_sessions: set[str] = set()
        # Sessions that existed before a sandbox recreate — they are gone on the
        # new instance, and view() uses this to explain *why* instead of a generic
        # "not found".
        self._lost_sessions: set[str] = set()
        # C3: persistent dev servers the agent launched — keys are tmux session
        # names (no namespace prefix), values are the command/exec_dir/port
        # needed to RE-MATERIALIZE them on the fresh instance after a recreate.
        # Survives `reset_known_sessions()`: tmux sessions on the old box are
        # gone, but the METADATA needed to restart them is what carries across.
        self._persistent_servers: dict[str, PersistentServer] = {}

    def reset_known_sessions(self) -> None:
        # Note: _persistent_servers is intentionally NOT cleared here. The
        # whole point is to survive the recreate so a real app can be restarted
        # on the fresh instance. `_known_sessions` and `_lost_sessions` track
        # the tmux-level state, which genuinely is gone.
        self._lost_sessions |= self._known_sessions
        self._known_sessions.clear()

    def _classify_persistent_server(self, command: str) -> int | None:
        """Return the USER_PORT this command will bind, or None.

        Signal: a still-running command whose argv references a port in
        USER_PORTS. This is the cleanest available signal that the agent
        launched a long-running dev server (the alternative — inferring server
        intent from the binary name — is brittle across vite/next/uvicorn/…).

        One-shots (`npm run build`, `python -m pytest`) never reach this code
        path because their `exec()` returns `running=False`. `lsof -i :8000` or
        `curl http://localhost:3000/...` aren't `running` either, so they
        don't pollute the dict either.
        """
        # shlex.split raises on unbalanced quotes; fall back to a coarse split
        # so we still record the (common) case `python3 -m http.server 8000`.
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        for tok in tokens:
            # exact numeric token, not a substring (e.g. "127.0.0.1:8000" must
            # not match — its tokens are "127.0.0.1:8000", a single string)
            if tok.isdigit():
                port = int(tok)
                if port in self._allowed_persistent_ports():
                    return port
        return None

    def _allowed_persistent_ports(self) -> frozenset[int]:
        # Local import to avoid a top-level cycle (port_owner pulls from
        # base, which is fine; this is purely for the dependency direction
        # remaining one-way: shell_sessions <- port_owner, not the reverse).
        from ._container import USER_PORTS
        return USER_PORTS

    def _full_name(self, name: str) -> str:
        if self.namespace:
            return f"{_PREFIX}-{self.namespace}{name}"
        return f"{_PREFIX}-{name}"

    async def _run_tmux(self, cmd: str) -> str:
        inst = await self._get_instance()
        res = await inst.exec_shell(f"tmux {cmd}", timeout_s=_TMUX_TIMEOUT_S)
        if res.exit_code != 0:
            raise RuntimeError(
                f"tmux {cmd} failed: {res.stdout} "
                f"{res.stderr}"
            )
        return res.stdout

    async def _run_tmux_safe(self, cmd: str) -> tuple[int, str]:
        inst = await self._get_instance()
        res = await inst.exec_shell(f"tmux {cmd}", timeout_s=_TMUX_TIMEOUT_S)
        return res.exit_code, res.stdout

    async def ensure(self, name: str, exec_dir: str | None = None) -> None:
        full = self._full_name(name)
        if name in self._known_sessions:
            return

        code, _ = await self._run_tmux_safe(f"has-session -t {shlex.quote(full)} 2>/dev/null")
        if code == 0:
            self._known_sessions.add(name)
            self._lost_sessions.discard(name)
            return

        cd_args = f"-c {shlex.quote(exec_dir)}" if exec_dir else ""
        await self._run_tmux(f"new-session -d -s {shlex.quote(full)} -x 250 -y 50 {cd_args}")

        setup_cmd = f"export PS1='{_PS1}' PS2='' PROMPT_COMMAND=''; history -c; clear"
        await self._run_tmux(f"send-keys -t {shlex.quote(full)} -l {shlex.quote(setup_cmd)}")
        await self._run_tmux(f"send-keys -t {shlex.quote(full)} Enter")

        for _ in range(10):
            await asyncio.sleep(_POLL_S)
            view = await self.view(name)
            if not view.running:
                self._known_sessions.add(name)
                self._lost_sessions.discard(name)
                return

        self._known_sessions.add(name)
        self._lost_sessions.discard(name)

    async def is_busy(self, name: str) -> bool:
        view = await self.view(name)
        return view.running

    def _strip_output(self, delta: str) -> tuple[str, int | None]:
        if not delta:
            return "", None
            
        lines = delta.split("\n")
        while lines and lines[-1] == "":
            lines.pop()

        if lines:
            lines = lines[1:]

        if not lines:
            return "", None

        last_line = lines[-1]
        m = _MARKER_RE.search(last_line)
        if m:
            exit_code = int(m.group(1))
            lines = lines[:-1]
            return "\n".join(lines), exit_code
        return "\n".join(lines), None

    async def exec(self, name: str, command: str, exec_dir: str | None) -> ExecOutcome:
        try:
            await self.ensure(name, exec_dir)
        except Exception as e:
            raise RuntimeError(
                f"session '{name}' could not be reached (sandbox shell unavailable or "
                f"recreated) — retry once; if it persists, use a new session name or "
                f"server_start. {e}"
            ) from e
        if await self.is_busy(name):
            full = self._full_name(name)
            rc, pane_out = await self._run_tmux_safe(
                f"list-panes -t {shlex.quote(full)} -F '#{{pane_current_command}}'"
            )
            cmd_name = (
                pane_out.strip().splitlines()[0].strip()
                if rc == 0 and pane_out.strip()
                else ""
            )
            if cmd_name:
                raise SessionBusy(
                    f"session '{name}' is busy running '{cmd_name}' — "
                    "wait for it (shell_wait), "
                    "interact with it (shell_write_to_process), "
                    "kill it (shell_kill_process), "
                    "or use a different session name."
                )
            raise SessionBusy(
                f"Previous command not finished in session '{name}'. Wait for it (shell_wait), "
                "interact with it (shell_write_to_process), kill it (shell_kill_process), "
                "or use a different session name."
            )

        full = self._full_name(name)
        _, pre_cap = await self._run_tmux_safe(f"capture-pane -t {shlex.quote(full)} -p -S -")
        pre_cap = pre_cap.rstrip("\r\n")

        await self._run_tmux(f"send-keys -t {shlex.quote(full)} -l {shlex.quote(command)}")
        await self._run_tmux(f"send-keys -t {shlex.quote(full)} Enter")

        # Wall-clock deadline, NOT a poll-count accumulator: over Docker-over-SSH each
        # capture-pane round-trip costs 1-2s that a `+= _POLL_S` counter never sees,
        # silently stretching "15s" to a minute (caught live on the gvisor backend).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _EXEC_WAIT_S
        while loop.time() < deadline:
            await asyncio.sleep(_POLL_S)

            _, post_cap = await self._run_tmux_safe(f"capture-pane -t {shlex.quote(full)} -p -S -")
            post_cap = post_cap.rstrip("\r\n")

            if post_cap.startswith(pre_cap):
                delta = post_cap[len(pre_cap):]
            else:
                delta = post_cap

            delta_lines = delta.split("\n")
            while delta_lines and delta_lines[-1] == "":
                delta_lines.pop()
                
            if delta_lines and _MARKER_RE.search(delta_lines[-1]):
                cleaned, exit_code = self._strip_output(delta)
                outcome = ExecOutcome(running=False, exit_code=exit_code, output=cleaned[-_EXEC_RETURN_CHARS:])
                self._record_persistent_if_match(name, command, exec_dir, outcome)
                return outcome

        _, post_cap = await self._run_tmux_safe(f"capture-pane -t {shlex.quote(full)} -p -S -")
        post_cap = post_cap.rstrip("\r\n")
        if post_cap.startswith(pre_cap):
            delta = post_cap[len(pre_cap):]
        else:
            delta = post_cap

        cleaned_running, _ = self._strip_output(delta)
        outcome = ExecOutcome(
            running=True,
            exit_code=None,
            output=cleaned_running[-_EXEC_RETURN_CHARS:],
            note="still running after 15s — use shell_view / shell_wait"
        )
        self._record_persistent_if_match(name, command, exec_dir, outcome)
        return outcome

    def _record_persistent_if_match(
        self, name: str, command: str, exec_dir: str | None, outcome: ExecOutcome
    ) -> None:
        """Update `_persistent_servers` based on a finished exec() outcome.

        - If the command is STILL running and references a USER_PORT in argv,
          record (or refresh) the entry — it's a dev server the agent launched
          that we should restart on a recreate.
        - Otherwise, drop any stale entry for that session: the agent finished
          the server (`Ctrl-C` / `kill`), replaced it with a one-shot, or
          replaced it with a server on a different port. Either way the OLD
          entry no longer reflects reality and re-running it would be wrong.
        """
        if outcome.running:
            port = self._classify_persistent_server(command)
            if port is not None:
                self._persistent_servers[name] = PersistentServer(
                    name=name, command=command, exec_dir=exec_dir, port=port
                )
                return
        # Not running, or not a port-binding command — forget any prior entry.
        self._persistent_servers.pop(name, None)

    def recorded_persistent_servers(self) -> list[PersistentServer]:
        """Snapshot of the persistent-server registry (read-only view, for
        diagnostics + tests). Order is insertion order — the rehydrate
        re-issues in the same order the agent launched them."""
        return list(self._persistent_servers.values())

    async def rehydrate_persistent_servers(
        self, port_check: Callable[[int], Awaitable[bool]] | None = None
    ) -> list[str]:
        """Re-issue each recorded persistent-server command on the CURRENT
        instance. Called by `SandboxSession._recreate` after a fresh instance
        is up so a real app (vite / express / uvicorn / http.server on a
        non-default port) survives suspend/wake, not just the static
        auto-preview.

        `port_check(port)` -> True iff a USER_PORT is already bound on the
        fresh instance. We SKIP those to avoid duplicating a server that's
        already running (the no-duplication guarantee). Defaults to a /proc
        probe via `port_owner`.

        Best-effort: any single failure is logged and the rehydrate continues
        with the next entry. The function NEVER raises — rehydrate is a
        convenience, like the static auto-preview, and the box must not care.
        Returns a list of human-readable log lines for diagnostics/tests.
        """
        from .port_owner import port_owner

        if port_check is None:
            async def _default_check(port: int) -> bool:
                try:
                    inst = await self._get_instance()
                except Exception:  # noqa: BLE001 — instance gone; just attempt rehydrate
                    return False
                try:
                    owner = await port_owner(inst, port)
                except Exception:  # noqa: BLE001 — probe failed; treat as not bound
                    return False
                return owner is not None and owner.pid is not None

            port_check = _default_check

        logs: list[str] = []
        # Snapshot the keys so the dict isn't mutated mid-iteration by the
        # recording that happens inside the re-issued exec() call.
        for name, srv in list(self._persistent_servers.items()):
            try:
                if await port_check(srv.port):
                    logs.append(
                        f"port {srv.port} already bound on the new instance — "
                        f"skipping rehydrate of session '{name}' "
                        f"(cmd: {srv.command})"
                    )
                    continue
            except Exception as exc:  # noqa: BLE001 — port-check failure is non-fatal
                logs.append(
                    f"port-check for {srv.port} failed while rehydrating "
                    f"'{name}': {exc!r} — attempting re-issue anyway"
                )
            try:
                await self.exec(name, srv.command, srv.exec_dir)
            except Exception as exc:  # noqa: BLE001 — best-effort; one bad rehydrate must not block the rest
                logs.append(
                    f"failed to re-materialize persistent server '{name}' "
                    f"(cmd: {srv.command}): {exc!r}"
                )
                _LOG.warning(
                    "rehydrate of persistent server %r failed: %r", name, exc
                )
                continue
            logs.append(
                f"re-materialized persistent server '{name}' on port {srv.port} "
                f"(cmd: {srv.command})"
            )
        return logs

    async def view(self, name: str, tail_chars: int = _VIEW_TAIL_CHARS) -> SessionView:
        full = self._full_name(name)
        code, out = await self._run_tmux_safe(f"capture-pane -t {shlex.quote(full)} -p -S -")
        if code != 0:
            if name in self._lost_sessions:
                return SessionView(running=False, output="session lost: sandbox was recreated")
            return SessionView(running=False, output=f"Session not found or error: {out}")

        out = out.rstrip("\r\n")
        lines = out.split("\n")
        running = True
        if lines:
            m = _MARKER_RE.search(lines[-1])
            if m:
                running = False

        return SessionView(running=running, output=out[-tail_chars:])

    async def wait(self, name: str, seconds: int) -> SessionView:
        max_sec = min(seconds, 300)
        # Wall-clock deadline (see exec): poll-count accumulators under-count on
        # high-latency transports and overshoot the promised wait.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_sec
        while loop.time() < deadline:
            view = await self.view(name)
            if not view.running:
                return view
            await asyncio.sleep(_POLL_S)
        return await self.view(name)

    async def write(self, name: str, text: str, press_enter: bool) -> None:
        full = self._full_name(name)
        code, _ = await self._run_tmux_safe(f"has-session -t {shlex.quote(full)} 2>/dev/null")
        if code != 0:
            raise RuntimeError(f"Session '{name}' does not exist.")

        await self._run_tmux(f"send-keys -t {shlex.quote(full)} -l {shlex.quote(text)}")
        if press_enter:
            await self._run_tmux(f"send-keys -t {shlex.quote(full)} Enter")

    async def kill_foreground(self, name: str) -> str:
        full = self._full_name(name)
        code, _ = await self._run_tmux_safe(f"has-session -t {shlex.quote(full)} 2>/dev/null")
        if code != 0:
            return f"Session '{name}' does not exist."

        await self._run_tmux(f"send-keys -t {shlex.quote(full)} C-c")

        for _ in range(6):
            await asyncio.sleep(_POLL_S)
            if not await self.is_busy(name):
                return f"Sent Ctrl-C; session '{name}' is now idle."

        await self._run_tmux(f"kill-session -t {shlex.quote(full)}")
        if name in self._known_sessions:
            self._known_sessions.remove(name)
        await self.ensure(name)
        return f"Process ignored Ctrl-C; session '{name}' was killed and recreated."

    async def list(self) -> list[SessionInfo]:
        code, out = await self._run_tmux_safe("list-sessions -F '#{session_name}'")
        if code != 0:
            return []

        sessions = []
        for line in out.splitlines():
            line = line.strip()
            # Dual-read: list sessions under the current `disco-` AND legacy `pmx-`
            # prefix so a rename never strands a session. Strip whichever matched.
            suffix = f"-{self.namespace}" if self.namespace else "-"
            prefix_match = None
            for p in (_PREFIX, _LEGACY_PREFIX):
                cand = f"{p}{suffix}"
                if line.startswith(cand):
                    prefix_match = cand
                    break
            if prefix_match is not None:
                name = line[len(prefix_match):]
                view = await self.view(name, tail_chars=1000)
                last_lines = "\n".join(view.output.split("\n")[-3:])
                sessions.append(SessionInfo(name=name, busy=view.running, last_lines=last_lines))
        return sessions
