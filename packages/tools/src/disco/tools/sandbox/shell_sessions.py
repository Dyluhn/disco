"""ShellSessionManager - persistent named shell sessions backed by tmux."""

# MODULE SURFACE, PRESERVED (Epic 10-B) ------------------------------------------
#
# `secrets` is not called directly here anymore (`exec_dispatch` owns the
# `token_hex` dispatch), but the attribute must stay reachable as
# `shell_sessions.secrets`: `test_shell_sessions.py` and
# `test_rematerialize_servers.py` do
# `monkeypatch.setattr(shell_sessions_module.secrets, "token_hex", …)` to make
# dispatch tokens deterministic. That patches an attribute *on the shared
# `secrets` module object*, which is why `exec_dispatch`'s own `import secrets`
# still observes it — but the patch target expression has to resolve here first.
# Deleting it as "unused" (which `ruff --fix` offers) breaks both tests at patch
# time. It, and the marker constants below, use the redundant-alias re-export
# form (`X as X`) — ruff's sanctioned re-export marker, no suppression needed.
import asyncio
import logging
import re
import secrets as secrets
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .base import SandboxInstance, clean_sandbox_env
from .shell_sessions_parts import exec_dispatch as _exec_dispatch_part
from .shell_sessions_parts import rematerialize as _rematerialize_part
from .shell_sessions_parts import text as _text_part
from .shell_sessions_parts.text import _DONE_MARKER as _DONE_MARKER
from .shell_sessions_parts.text import _FRESH_MARKER_RE as _FRESH_MARKER_RE
from .shell_sessions_parts.text import _MARKER_RE, _PS1
from .shell_sessions_parts.text import _PS1_MARKER as _PS1_MARKER

_LOG = logging.getLogger(__name__)

_PREFIX = "disco"  # WRITE side: new tmux sessions are disco-*
_LEGACY_PREFIX = "pmx"  # READ side: still swept on teardown (no orphans across rename)
_VIEW_TAIL_CHARS = 10_000
_SHELL_COMMANDS = frozenset({"bash", "sh", "dash", "zsh", "fish", "ash"})  # an idle pane's foreground
_EXEC_RETURN_CHARS = 6_000
_POLL_S = 0.5
_EXEC_WAIT_S = 15.0
_TMUX_TIMEOUT_S = 10
_BACKGROUND_OWNER_ATTEMPTS = 3
_BACKGROUND_OWNER_INTERVAL_S = 0.1


@dataclass
class PersistentServer:
    """Metadata for one agent-launched dev server the session should try to
    RE-MATERIALIZE on a sandbox recreate (C3). A PersistentServer is recorded
    ONLY for shell sessions whose command argv contains a USER_PORT and either
    keeps the foreground busy or has a background listener positively attributed
    to that exact tmux session (i.e. a server, not a one-shot build). On recreate,
    the recorded entries are re-issued best-effort so a real app
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


def _short_session_name(namespace: str, full: str) -> str | None:
    """A namespace's short session name for a tmux session name, or None.

    Dual-read: sessions under the current `disco-` AND legacy `pmx-` prefix so a rename
    never strands a session. W3 C-5: internal `__`-prefixed sessions (e.g. `__kernel`)
    are never enumerated — the single choke point every listing consumer inherits."""
    suffix = f"-{namespace}" if namespace else "-"
    for prefix in (_PREFIX, _LEGACY_PREFIX):
        candidate = f"{prefix}{suffix}"
        if full.startswith(candidate):
            name = full[len(candidate) :]
            return None if name.startswith("__") else name
    return None


class ShellSessionManager:
    def __init__(
        self, instance_getter: Callable[[], Awaitable[SandboxInstance]], namespace: str = ""
    ) -> None:
        self._get_instance = instance_getter
        self.namespace = namespace
        self._known_sessions: set[str] = set()
        # Sessions that existed before a sandbox recreate — they are gone on the
        # new instance, and view() uses this to explain *why* instead of a generic
        # "not found".
        self._lost_sessions: set[str] = set()
        # Foreground-shell state proven by THIS manager generation. Background
        # children may append output after bash has already printed a fresh prompt;
        # the pane's final line then looks busy even though the shell is idle. Never
        # infer this from arbitrary scrollback: exec() sets idle only from its own
        # post-dispatch delta, and a fresh command resets it to busy.
        self._foreground_state: dict[str, str] = {}
        # C3: persistent dev servers the agent launched — keys are tmux session
        # names (no namespace prefix), values are the command/exec_dir/port
        # needed to RE-MATERIALIZE them on the fresh instance after a recreate.
        # Survives `reset_known_sessions()`: tmux sessions on the old box are
        # gone, but the METADATA needed to restart them is what carries across.
        self._persistent_servers: dict[str, PersistentServer] = {}
        # A generation rotation retires the whole namespace exactly once; see
        # _retire_previous_generation.
        self._retired_previous_generation = False

    def reset_known_sessions(self) -> None:
        # Note: _persistent_servers is intentionally NOT cleared here. The
        # whole point is to survive the recreate so a real app can be restarted
        # on the fresh instance. `_known_sessions` and `_lost_sessions` track
        # the tmux-level state, which genuinely is gone.
        self._lost_sessions |= self._known_sessions
        self._known_sessions.clear()
        self._foreground_state.clear()

    def _classify_persistent_server(self, command: str) -> int | None:
        """Return the USER_PORT this command will bind, or None.

        Signal: a still-running command whose argv references a port in
        USER_PORTS. This is the cleanest available signal that the agent
        launched a long-running dev server (the alternative — inferring server
        intent from the binary name — is brittle across vite/next/uvicorn/…).

        Classification alone never records anything: foreground commands must
        remain busy, while background commands must prove exact port ownership.
        One-shots (`npm run build`, `python -m pytest`), `lsof -i :8000`, and
        `curl http://localhost:3000/...` therefore cannot pollute the registry.
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

    async def _process_session_environment(self) -> dict[str, str]:
        """Return explicit per-session capability env for the process backend.

        A process sandbox's tmux client has the correct clean environment, but
        tmux itself is a host-global long-lived server.  Its global environment
        can belong to an earlier conversation, so relying on inheritance can
        point helper processes (notably the browser daemon) at another workspace
        or at the guest-only fallback ``/workspace``.  Container backends own
        their tmux server and expose no host ``workspace_path``; their behavior is
        intentionally unchanged.
        """
        inst = await self._get_instance()
        workspace = getattr(inst, "workspace_path", None)
        if not workspace:
            return {}
        return clean_sandbox_env(workspace)

    async def _bind_existing_process_session(self, full: str) -> None:
        """Refresh tmux's session env after an agent-server restart.

        Existing shells created by the repaired path already carry these values;
        setting the session copy makes future respawns/helpers deterministic
        without injecting text into a possibly busy shell.
        """
        for key, value in (await self._process_session_environment()).items():
            await self._run_tmux(
                f"set-environment -t {shlex.quote(full)} {shlex.quote(key)} {shlex.quote(value)}"
            )

    async def _session_owned_by_current_workspace(self, full: str) -> bool:
        """Does this surviving tmux session belong to THIS sandbox generation?

        Process sandboxes share the HOST tmux server, so a session outlives the
        agent-server that created it (the app lifespan closes stores and the MCP
        pool on SIGTERM; it destroys no sandbox and sweeps no tmux). The next
        generation composes a NEW workspace, and `set-environment` only seeds
        shells tmux spawns AFTER it — the pane's already-running shell keeps the
        dead generation's PATH/HOME/TMPDIR/DISCO_WORKSPACE and cwd forever.

        Adopting such a pane hands every helper launched into it a previous
        generation's workspace IDENTITY. The browser daemon derives both its
        port file and its instance hash from `DISCO_WORKSPACE`, so an adopted
        pane makes it publish into the dead tree and report the dead hash: the
        allocator's identity pin then refuses it (correctly) on every poll and
        the backend is reported as having no browser at all — the earliest
        broken link behind counted restart trial `p4_ff_react_restart` 450000,
        where browser verification worked before the restart and was
        permanently unavailable after it.

        So a session is OWNED by the generation whose workspace it was created
        for; one bound to any other workspace is not adopted, it is replaced.
        The read is taken BEFORE `_bind_existing_process_session` overwrites the
        session copy. Unknown/unreadable binding fails toward replacement — the
        safe direction, since recreating a shell costs one tmux round-trip and
        adopting a dead one costs the whole run. Container backends own their
        own tmux server per instance and are never subject to this: they expose
        no host workspace, so they keep the previous adopt-always behavior.
        """
        expected = (await self._process_session_environment()).get("DISCO_WORKSPACE")
        if expected is None:
            return True  # container backend — its tmux server dies with the box
        code, out = await self._run_tmux_safe(
            f"show-environment -t {shlex.quote(full)} DISCO_WORKSPACE"
        )
        if code != 0:
            return False
        return out.strip() == f"DISCO_WORKSPACE={expected}"

    async def _run_tmux(self, cmd: str) -> str:
        inst = await self._get_instance()
        res = await inst.exec_shell(f"tmux {cmd}", timeout_s=_TMUX_TIMEOUT_S)
        if res.exit_code != 0:
            raise RuntimeError(f"tmux {cmd} failed: {res.stdout} {res.stderr}")
        return res.stdout

    async def _run_tmux_safe(self, cmd: str) -> tuple[int, str]:
        inst = await self._get_instance()
        res = await inst.exec_shell(f"tmux {cmd}", timeout_s=_TMUX_TIMEOUT_S)
        return res.exit_code, res.stdout

    async def _retire_previous_generation(self) -> list[str]:
        """Kill every session in THIS conversation's namespace, once.

        Scoped by construction: only `disco-<namespace>*` (and the legacy `pmx-`
        prefix) is matched, so one conversation's rotation can never touch
        another's sessions, and a namespace-less manager (container backends,
        which own a tmux server per box) sweeps nothing.
        """
        if self._retired_previous_generation:
            return []
        self._retired_previous_generation = True
        code, out = await self._run_tmux_safe("list-sessions -F '#{session_name}'")
        if code != 0:
            return []
        prefixes = tuple(f"{prefix}-{self.namespace}" for prefix in (_PREFIX, _LEGACY_PREFIX))
        if not self.namespace:
            return []
        retired: list[str] = []
        for line in out.splitlines():
            session = line.strip()
            if not session.startswith(prefixes):
                continue
            await self._run_tmux_safe(f"kill-session -t {shlex.quote(session)}")
            retired.append(session)
        self._foreground_state.clear()
        self._known_sessions.clear()
        return retired

    async def ensure(self, name: str, exec_dir: str | None = None) -> None:
        full = self._full_name(name)
        if name in self._known_sessions:
            return

        code, _ = await self._run_tmux_safe(f"has-session -t {shlex.quote(full)} 2>/dev/null")
        if code == 0:
            if await self._session_owned_by_current_workspace(full):
                await self._bind_existing_process_session(full)
                self._known_sessions.add(name)
                self._lost_sessions.discard(name)
                return
            # A previous generation's pane: replace it rather than adopt a dead
            # workspace identity. Sweep the WHOLE namespace, not just this name —
            # every session under it is bound to the same dead workspace, so none
            # is adoptable, and the ones nothing happens to re-`ensure` are exactly
            # the ones that leak. A dev server left running in such a pane keeps
            # its port bound, so the next generation's bind fails EADDRINUSE with
            # no visible owner (the "no free platform preview port" shape).
            await self._retire_previous_generation()

        cd_args = f"-c {shlex.quote(exec_dir)}" if exec_dir else ""
        session_env = await self._process_session_environment()
        env_args = " ".join(
            f"-e {shlex.quote(f'{key}={value}')}" for key, value in session_env.items()
        )
        await self._run_tmux(
            f"new-session -d -s {shlex.quote(full)} -x 250 -y 50 {env_args} {cd_args}".rstrip()
        )

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

    def _strip_output(
        self,
        delta: str,
        *,
        completion_re: re.Pattern[str] | None = None,
        echoed_dispatch: str | None = None,
    ) -> tuple[str, int | None]:
        """Peel a raw tmux pane delta down to the command's real output; see
        `shell_sessions_parts.text` for the full contract."""
        return _text_part.strip_output(
            delta, completion_re=completion_re, echoed_dispatch=echoed_dispatch
        )

    @staticmethod
    def _completion_dispatch(command: str, token: str) -> tuple[str, re.Pattern[str]]:
        """Wrap a command with an unguessable, line-anchored completion record;
        see `shell_sessions_parts.text` for the full contract."""
        return _text_part.completion_dispatch(command, token)

    @staticmethod
    def _backgrounded(command: str) -> bool:
        """Whether clean shell syntax contains a single-ampersand background edge;
        see `shell_sessions_parts.text` for the full contract."""
        return _text_part.is_backgrounded(command)

    async def _remap_reserved_preview_serve(self, command: str) -> str:
        """Bug 16 — the CLEAN-command home for the reserved-port preview-serve remap;
        see `shell_sessions_parts.exec_dispatch` for the full contract."""
        return await _exec_dispatch_part.remap_reserved_preview_serve(self, command)

    async def exec(self, name: str, command: str, exec_dir: str | None) -> ExecOutcome:
        """Dispatch one command into a tmux session and attribute its outcome;
        see `shell_sessions_parts.exec_dispatch` for the full contract."""
        return await _exec_dispatch_part.exec_command(self, name, command, exec_dir)

    async def _record_persistent_if_match(
        self,
        name: str,
        command: str,
        exec_dir: str | None,
        outcome: ExecOutcome,
        *,
        backgrounded: bool,
        background_owner_before: tuple[bool, int | None] | None = None,
    ) -> bool:
        """Update `_persistent_servers` from a finished exec() outcome; see
        `shell_sessions_parts.exec_dispatch` for the full contract."""
        return await _exec_dispatch_part.record_persistent_if_match(
            self,
            name,
            command,
            exec_dir,
            outcome,
            backgrounded=backgrounded,
            background_owner_before=background_owner_before,
        )

    async def _background_port_owner(
        self, name: str, port: int, *, wait_for_listener: bool = False
    ) -> tuple[bool, int | None]:
        """Return ``(probe_conclusive, exact-session-listener-pid)`` boundedly;
        see `shell_sessions_parts.exec_dispatch` for the full contract."""
        return await _exec_dispatch_part.background_port_owner(
            self, name, port, wait_for_listener=wait_for_listener
        )

    def recorded_persistent_servers(self) -> list[PersistentServer]:
        """Snapshot of the persistent-server registry (read-only view, for
        diagnostics + tests). Order is insertion order — the rehydrate
        re-issues in the same order the agent launched them."""
        return list(self._persistent_servers.values())

    async def stop_foreground_server(
        self,
        name: str,
        *,
        expected_command: str,
        expected_port: int,
    ) -> str:
        """Stop and revoke one exact foreground-server generation; see
        `shell_sessions_parts.rematerialize` for the full contract."""
        return await _rematerialize_part.stop_foreground_server(
            self, name, expected_command=expected_command, expected_port=expected_port
        )

    async def rehydrate_persistent_servers(
        self, port_check: Callable[[int], Awaitable[bool]] | None = None
    ) -> list[str]:
        """Re-issue each recorded persistent-server command on the CURRENT
        instance; see `shell_sessions_parts.rematerialize` for the full contract."""
        return await _rematerialize_part.rehydrate_persistent_servers(self, port_check)

    async def view(self, name: str, tail_chars: int = _VIEW_TAIL_CHARS) -> SessionView:
        full = self._full_name(name)
        code, out = await self._run_tmux_safe(f"capture-pane -t {shlex.quote(full)} -p -S -")
        if code != 0:
            if name in self._lost_sessions:
                return SessionView(running=False, output="session lost: sandbox was recreated")
            return SessionView(running=False, output=f"Session not found or error: {out}")

        out = out.rstrip("\r\n")
        lines = out.split("\n")
        running = self._foreground_state.get(name) != "idle"
        if lines:
            m = _MARKER_RE.search(lines[-1])
            if m:
                running = False
                self._foreground_state[name] = "idle"

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

        # Text delivery itself is state-ambiguous: a transport exception may be
        # raised after tmux accepted bytes, and a successful no-Enter write leaves
        # unsubmitted text at the prompt.  Both must invalidate cached idle proof.
        self._foreground_state.pop(name, None)
        await self._run_tmux(f"send-keys -t {shlex.quote(full)} -l {shlex.quote(text)}")
        if press_enter:
            try:
                await self._run_tmux(f"send-keys -t {shlex.quote(full)} Enter")
            except Exception:
                self._foreground_state.pop(name, None)
                raise
            self._foreground_state[name] = "busy"

    async def kill_foreground(self, name: str) -> str:
        full = self._full_name(name)
        code, _ = await self._run_tmux_safe(f"has-session -t {shlex.quote(full)} 2>/dev/null")
        if code != 0:
            return f"Session '{name}' does not exist."

        if self._foreground_state.get(name) == "idle" or (
            name not in self._foreground_state and not await self.is_busy(name)
        ):
            return f"Session '{name}' is already idle; no signal sent."

        await self._run_tmux(f"send-keys -t {shlex.quote(full)} C-c")

        for _ in range(6):
            await asyncio.sleep(_POLL_S)
            if not await self.is_busy(name):
                return f"Sent Ctrl-C; session '{name}' is now idle."

        await self._run_tmux(f"kill-session -t {shlex.quote(full)}")
        if name in self._known_sessions:
            self._known_sessions.remove(name)
        self._foreground_state.pop(name, None)
        await self.ensure(name)
        return f"Process ignored Ctrl-C; session '{name}' was killed and recreated."

    async def _list_from_panes(self) -> list[SessionInfo]:
        code, out = await self._run_tmux_safe(
            "list-panes -a -F '#{session_name}\t#{pane_current_command}'"
        )
        if code != 0:
            return []
        busy: dict[str, bool] = {}
        for line in out.splitlines():
            session, _, command = line.strip().partition("\t")
            name = _short_session_name(self.namespace, session)
            if name is not None:
                busy[name] = busy.get(name, False) or (bool(command) and command not in _SHELL_COMMANDS)
        return [SessionInfo(name=n, busy=b, last_lines="") for n, b in sorted(busy.items())]

    async def list(self, *, output: bool = True) -> list[SessionInfo]:
        """This namespace's sessions. With ``output=False`` one ``list-panes`` call answers
        busy-or-idle from each pane's foreground command and ``last_lines`` is empty — the
        check ledger's purity check needs only that, and the per-pane capture the default
        path does cost ~2 s per ledger snapshot on a five-session build (Pharmacy run 4)."""
        if not output:
            return await self._list_from_panes()
        code, out = await self._run_tmux_safe("list-sessions -F '#{session_name}'")
        if code != 0:
            return []
        sessions = []
        for line in out.splitlines():
            name = _short_session_name(self.namespace, line.strip())
            if name is not None:
                view = await self.view(name, tail_chars=1000)
                last_lines = "\n".join(view.output.split("\n")[-3:])
                sessions.append(SessionInfo(name=name, busy=view.running, last_lines=last_lines))
        return sessions

