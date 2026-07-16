"""ShellSessionManager - persistent named shell sessions backed by tmux."""

import asyncio
import logging
import re
import secrets
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .base import SandboxInstance, clean_sandbox_env

_LOG = logging.getLogger(__name__)

_PREFIX = "disco"  # WRITE side: new tmux sessions are disco-*
_LEGACY_PREFIX = "pmx"  # READ side: still swept on teardown (no orphans across rename)
_PS1_MARKER = "__DISCO_PS1__"
_PS1 = f"{_PS1_MARKER}$?__$ "
# Derive the parse regex from the marker constant so the WRITE (PS1) and READ
# (parse) sides can never drift — the exact bug a literal `__PMX_PS1__` here
# re-introduced after the marker was renamed.
_MARKER_RE = re.compile(re.escape(_PS1_MARKER) + r"(\d+)__\$\s*$")
_FRESH_MARKER_RE = re.compile(re.escape(_PS1_MARKER) + r"(\d+)__\$[ \t]*")
_DONE_MARKER = "__DISCO_DONE_"
_VIEW_TAIL_CHARS = 10_000
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

    async def ensure(self, name: str, exec_dir: str | None = None) -> None:
        full = self._full_name(name)
        if name in self._known_sessions:
            return

        code, _ = await self._run_tmux_safe(f"has-session -t {shlex.quote(full)} 2>/dev/null")
        if code == 0:
            await self._bind_existing_process_session(full)
            self._known_sessions.add(name)
            self._lost_sessions.discard(name)
            return

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
        if not delta:
            return "", None

        text = delta.lstrip("\n")
        if echoed_dispatch is not None and text.startswith(echoed_dispatch):
            text = text[len(echoed_dispatch) :].lstrip("\n")
        else:
            # Compatibility for captured panes produced before the private
            # wrapper (and for narrow fakes): discard one echoed command line.
            _echo, separator, remainder = text.partition("\n")
            text = remainder if separator else ""
        text = text.rstrip("\n")

        if not text:
            return "", None
        matches = list((completion_re or _FRESH_MARKER_RE).finditer(text))
        if not matches:
            return text, None
        marker = matches[-1]
        # Strip only the fresh prompt token. Preserve every byte written after it:
        # background-child stderr is the actionable evidence H322 previously hid
        # behind a false "still running" verdict.
        cleaned = text[: marker.start()] + text[marker.end() :]
        if completion_re is not None:
            # Bash prints its normal PS1 immediately after the private completion
            # record.  Strip that display token too, while preserving late stderr
            # emitted by a background child after the prompt.
            cleaned = _FRESH_MARKER_RE.sub("", cleaned, count=1)
        return cleaned.strip("\n"), int(marker.group(1))

    @staticmethod
    def _completion_dispatch(command: str, token: str) -> tuple[str, re.Pattern[str]]:
        """Wrap a command with an unguessable, line-anchored completion record.

        A public PS1 string is display, not proof: the command echo or arbitrary
        process output can contain it.  ``eval`` preserves the interactive shell's
        state-changing semantics while the private token is kept out of the marker
        literal in the echoed wrapper (it is supplied separately to ``printf``).
        """

        rc_name = f"__disco_rc_{token}"
        dispatched = (
            f"eval {shlex.quote(command)}; {rc_name}=$?; "
            f"printf '\\n{_DONE_MARKER}%s__%s__\\n' {shlex.quote(token)} \"${rc_name}\""
        )
        completion_re = re.compile(rf"(?m)^{re.escape(_DONE_MARKER + token)}__(\d+)__[ \t]*$")
        return dispatched, completion_re

    @staticmethod
    def _backgrounded(command: str) -> bool:
        """Whether clean shell syntax contains a single-ampersand background edge."""

        quote = ""
        escaped = False
        for index, char in enumerate(command):
            if escaped:
                escaped = False
                continue
            if char == "\\" and quote != "'":
                escaped = True
                continue
            if quote:
                if char == quote:
                    quote = ""
                continue
            if char in ("'", '"'):
                quote = char
                continue
            if char == "#" and (
                index == 0 or command[index - 1].isspace() or command[index - 1] in ";|&()"
            ):
                break
            if char != "&":
                continue
            previous = command[index - 1] if index else ""
            following = command[index + 1] if index + 1 < len(command) else ""
            if previous in {"&", "<", ">", "|"} or following in {"&", ">", "|"}:
                continue
            return True
        return False

    async def _remap_reserved_preview_serve(self, command: str) -> str:
        """Bug 16 — the CLEAN-command home for the reserved-port preview-serve remap.

        `command` here is the model's RAW `shell_exec` command, BEFORE it is wrapped into
        `tmux send-keys -l '<command>'` below — so a genuine leading `python -m
        http.server <reserved>` serve can be remapped to a process-safe port precisely
        and unambiguously (no scan over arbitrary/wrapped shell text; see
        `remap_reserved_preview_serve`). Gated to SHARED-host backends via the SAME signal
        the verify resolver uses (`workspace_path is not None` ⇒ process/local, where 8000
        is the agent-server's own control port); an ISOLATED container keeps 8000 as its
        canonical app port and is never remapped. Best-effort: any probe failure leaves
        the command unchanged (the containment refusal is still the safety net)."""
        try:
            inst = await self._get_instance()
        except Exception:  # noqa: BLE001 — instance unavailable; leave command unchanged
            return command
        if getattr(inst, "workspace_path", None) is None:
            return command  # isolated backend — 8000 is the box's own app, keep it
        from disco.core.loop.preview_target import remap_reserved_preview_serve

        remapped = remap_reserved_preview_serve(command)
        return remapped if remapped is not None else command

    async def exec(self, name: str, command: str, exec_dir: str | None) -> ExecOutcome:
        # Bug 16: remap a reserved-port preview SERVE on the CLEAN command, before the
        # tmux-wrap below — so a model serving on 8000/5173 lands on a safe, conversation-
        # owned port the verify resolver can target (instead of STUCK verify_no_progress),
        # while the Bug-7 crash vector stays closed (we never wrap/run a reserved bind).
        command = await self._remap_reserved_preview_serve(command)
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
                pane_out.strip().splitlines()[0].strip() if rc == 0 and pane_out.strip() else ""
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
        _, pre_cap = await self._run_tmux_safe(f"capture-pane -J -t {shlex.quote(full)} -p -S -")
        pre_cap = pre_cap.rstrip("\r\n")

        backgrounded = self._backgrounded(command)
        port = self._classify_persistent_server(command) if backgrounded else None
        background_owner_before = (
            await self._background_port_owner(name, port) if port is not None else None
        )
        token = secrets.token_hex(16)
        dispatched, completion_re = self._completion_dispatch(command, token)

        # A transport error can occur after tmux accepted the bytes.  From this
        # point until a private completion record is observed, cached idle proof
        # is invalid even when the first send call raises.
        self._foreground_state[name] = "busy"
        try:
            await self._run_tmux(f"send-keys -t {shlex.quote(full)} -l {shlex.quote(dispatched)}")
        except Exception:
            self._foreground_state.pop(name, None)
            raise
        try:
            await self._run_tmux(f"send-keys -t {shlex.quote(full)} Enter")
        except Exception:
            # Command text is now sitting unsubmitted at the prompt. It is neither
            # proven idle nor running; clearing proof prevents a later exec from
            # appending a second command and accidentally submitting the concatenation.
            self._foreground_state.pop(name, None)
            raise
        # Wall-clock deadline, NOT a poll-count accumulator: over Docker-over-SSH each
        # capture-pane round-trip costs 1-2s that a `+= _POLL_S` counter never sees,
        # silently stretching "15s" to a minute (caught live on the gvisor backend).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _EXEC_WAIT_S
        while loop.time() < deadline:
            await asyncio.sleep(_POLL_S)

            _, post_cap = await self._run_tmux_safe(
                f"capture-pane -J -t {shlex.quote(full)} -p -S -"
            )
            post_cap = post_cap.rstrip("\r\n")

            prefix_continuity = post_cap.startswith(pre_cap)
            if prefix_continuity:
                delta = post_cap[len(pre_cap) :]
            else:
                delta = post_cap

            # Only this exec's private, line-anchored completion record is proof.
            # The public prompt marker can occur in command echo or process output.
            marker_is_fresh = completion_re.search(delta) is not None
            if marker_is_fresh:
                cleaned, exit_code = self._strip_output(
                    delta, completion_re=completion_re, echoed_dispatch=dispatched
                )
                outcome = ExecOutcome(
                    running=False, exit_code=exit_code, output=cleaned[-_EXEC_RETURN_CHARS:]
                )
                self._foreground_state[name] = "idle"
                recorded = await self._record_persistent_if_match(
                    name,
                    command,
                    exec_dir,
                    outcome,
                    backgrounded=backgrounded,
                    background_owner_before=background_owner_before,
                )
                if backgrounded:
                    outcome.note = (
                        "background server ownership confirmed"
                        if recorded
                        else "shell returned; background process status is unverified — use "
                        "server_status or preview_start"
                    )
                return outcome

        _, post_cap = await self._run_tmux_safe(f"capture-pane -J -t {shlex.quote(full)} -p -S -")
        post_cap = post_cap.rstrip("\r\n")
        if post_cap.startswith(pre_cap):
            delta = post_cap[len(pre_cap) :]
        else:
            delta = post_cap

        cleaned_running, _ = self._strip_output(delta, echoed_dispatch=dispatched)
        outcome = ExecOutcome(
            running=True,
            exit_code=None,
            output=cleaned_running[-_EXEC_RETURN_CHARS:],
            note="still running after 15s — use shell_view / shell_wait",
        )
        await self._record_persistent_if_match(
            name,
            command,
            exec_dir,
            outcome,
            backgrounded=backgrounded,
            background_owner_before=background_owner_before,
        )
        return outcome

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
        """Update `_persistent_servers` based on a finished exec() outcome.

        - If the command is STILL running and references a USER_PORT in argv,
          record (or refresh) the entry — it's a dev server the agent launched
          that we should restart on a recreate.
        - If the foreground shell returned after a syntactically backgrounded
          command, record only when the USER_PORT listener is positively owned by
          this exact tmux session.
        - Otherwise, drop any stale entry for that session: the agent finished
          the server (`Ctrl-C` / `kill`), replaced it with a one-shot, or
          replaced it with a server on a different port. Either way the OLD
          entry no longer reflects reality and re-running it would be wrong.
        """
        port = self._classify_persistent_server(command)
        if backgrounded:
            # A missed/delayed prompt must not bypass attribution: background
            # commands ALWAYS need exact listener ownership, even when exec() times
            # out and reports running=True. This excludes a foreign auto-preview.
            after = (
                await self._background_port_owner(name, port, wait_for_listener=True)
                if port is not None
                else None
            )
            if (
                port is not None
                and background_owner_before is not None
                and background_owner_before[0]
                and after is not None
                and after[0]
                and after[1] is not None
                and after[1] != background_owner_before[1]
            ):
                self._persistent_servers[name] = PersistentServer(
                    name=name, command=command, exec_dir=exec_dir, port=port
                )
                return True
            # A failed/redundant background launch cannot disprove an already
            # recorded server.  Preserve it, but return False so the new launch is
            # never attributed to the pre-existing listener.
            return False
        elif outcome.running and port is not None:
            self._persistent_servers[name] = PersistentServer(
                name=name, command=command, exec_dir=exec_dir, port=port
            )
            return True
        # Not running, or not a port-binding command — forget any prior entry.
        self._persistent_servers.pop(name, None)
        return False

    async def _background_port_owner(
        self, name: str, port: int, *, wait_for_listener: bool = False
    ) -> tuple[bool, int | None]:
        """Return ``(probe_conclusive, exact-session-listener-pid)`` boundedly."""

        from .port_owner import port_owner

        observed_absence = False
        for attempt in range(_BACKGROUND_OWNER_ATTEMPTS):
            try:
                inst = await self._get_instance()
                owner = await port_owner(inst, port)
            except Exception:  # noqa: BLE001 — inconclusive ownership is never admission
                owner = None
            # The probe contract distinguishes a successful absence result
            # (PortOwner with pid=None) from raw None/exception (probe failure).
            if owner is not None and owner.pid is None:
                observed_absence = True
                if not wait_for_listener:
                    return True, None
            elif owner is not None and owner.pid is not None:
                return True, owner.pid if owner.session == self._full_name(name) else None
            if attempt + 1 < _BACKGROUND_OWNER_ATTEMPTS:
                await asyncio.sleep(_BACKGROUND_OWNER_INTERVAL_S)
        return (True, None) if observed_absence else (False, None)

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
                _LOG.warning("rehydrate of persistent server %r failed: %r", name, exc)
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
                name = line[len(prefix_match) :]
                # W3 C-5: never enumerate internal, `__`-prefixed sessions (e.g.
                # `__kernel`, whose pane holds the gateway launch line). This is
                # the single choke point every listing consumer inherits — the
                # model-facing `server_status` tool and any other caller.
                if name.startswith("__"):
                    continue
                view = await self.view(name, tail_chars=1000)
                last_lines = "\n".join(view.output.split("\n")[-3:])
                sessions.append(SessionInfo(name=name, busy=view.running, last_lines=last_lines))
        return sessions
