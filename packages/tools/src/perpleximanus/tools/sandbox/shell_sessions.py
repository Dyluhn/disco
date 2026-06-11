"""ShellSessionManager - persistent named shell sessions backed by tmux."""

import asyncio
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .base import SandboxInstance

_PREFIX = "pmx"
_PS1_MARKER = "__PMX_PS1__"
_PS1 = f"{_PS1_MARKER}$?__$ "
_MARKER_RE = re.compile(r"__PMX_PS1__(\d+)__\$\s*$")
_VIEW_TAIL_CHARS = 10_000
_EXEC_RETURN_CHARS = 6_000
_POLL_S = 0.5
_EXEC_WAIT_S = 15.0
_TMUX_TIMEOUT_S = 10


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

    def reset_known_sessions(self) -> None:
        self._lost_sessions |= self._known_sessions
        self._known_sessions.clear()

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
                return ExecOutcome(running=False, exit_code=exit_code, output=cleaned[-_EXEC_RETURN_CHARS:])

        _, post_cap = await self._run_tmux_safe(f"capture-pane -t {shlex.quote(full)} -p -S -")
        post_cap = post_cap.rstrip("\r\n")
        if post_cap.startswith(pre_cap):
            delta = post_cap[len(pre_cap):]
        else:
            delta = post_cap

        cleaned_running, _ = self._strip_output(delta)
        return ExecOutcome(
            running=True,
            exit_code=None,
            output=cleaned_running[-_EXEC_RETURN_CHARS:],
            note="still running after 15s — use shell_view / shell_wait"
        )

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
            prefix_match = f"{_PREFIX}-"
            if self.namespace:
                prefix_match = f"{_PREFIX}-{self.namespace}"

            if line.startswith(prefix_match):
                name = line[len(prefix_match):]
                view = await self.view(name, tail_chars=1000)
                last_lines = "\n".join(view.output.split("\n")[-3:])
                sessions.append(SessionInfo(name=name, busy=view.running, last_lines=last_lines))
        return sessions
