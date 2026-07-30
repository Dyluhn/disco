"""Process sandbox backend — tool-sandbox-contract.md §5.1 (development only).

A local subprocess with a per-instance workspace dir. Weak isolation (no
hardware/microVM boundary) — DEV ONLY; production uses the `e2b`/Firecracker
backend behind the same `SandboxService` interface (§5.1, deferred). Two
properties it DOES uphold, which the security tests rely on:
  - subprocesses run with a CLEAN minimal env — never `os.environ` — so no host
    secret or config leaks into the box (§6 rule 1, defense in depth);
  - file ops are jailed to the workspace (path-escape attempts are rejected).

[CONTRACT caveat §7 rule 3] the `process` backend does NOT truly block network
egress (that needs OS namespaces / a proxy, which `e2b`/`gvisor` provide). The
deny-by-default egress *policy* is modeled on `SandboxSpec.egress_allowed(...)`
and enforced by network-using tools; the documented guarantee here is weaker
than production.
"""

from __future__ import annotations

import asyncio
import errno
import os
import re
import shlex
import shutil
import signal
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path

from disco.core.events import RuntimeConstraintDeclaration

from ._container import (
    MAX_SANDBOX_LIST_ENTRIES,
    MAX_SANDBOX_READ_BYTES,
    SANDBOX_READ_TIMEOUT_S,
    bounded_exec_argv,
)
from .base import (
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxPermissionError,
    SandboxSpec,
    SandboxUnavailableError,
    strip_redundant_workspace_prefix,
)
from .capability_relay import CapabilityRelayServer, RelayConfigurationError, serve_in_thread
from .config import SandboxConfig

# ROOT-1 (slides spiral): a genuine `/workspace`-rooted path token in a shell
# command — the leading `/workspace` AND the rest of the path up to the next token
# boundary (unescaped whitespace / quote / shell operator / end). Capturing the WHOLE token
# (not just the `/workspace` prefix) lets the rewrite RESOLVE + JAIL it via the same
# helper the file tools use, so a `..` traversal can't escape the jail.
# The lookbehind keeps it from matching a mid-path occurrence ('/foo/workspace') or a
# substring ('myworkspace'); the lookahead right after `/workspace` keeps it from
# matching a longer name ('/workspaces') — it only fires when `/workspace` is followed
# by a path separator, a token boundary, or end-of-string.
# Shell expansion inside a token: $VAR, ${VAR}, $(cmd), `cmd`.
_SHELL_EXPANSION_RE = re.compile(r"[$`]")

_WORKSPACE_TOKEN_RE = re.compile(
    r"(?<![\w/.])/workspace(?=/|$|[\s'\";|&<>()`])"
    r"(?:/(?:\\[^\n]|[^\s\\'\";|&<>()`])*)?"
)

# Bug 19 (P0): BEST-EFFORT, DEV-ONLY host-process-SIGNAL refusal for the PROCESS backend
# ONLY — NOT containment (the real containment is the fail-closed production-validity gate
# refusing this backend for prod/soak; see __init__.preflight_build_sandbox_backend).
# This backend shares the host PID namespace (no isolation — §5.1 dev-only), so a
# `kill <pid>` the build issues against a PID it discovered (`ss -lntp`/`pgrep`) can
# take down the AGENT-SERVER itself (observed LIVE: MiniMax-M3 ran `kill 931479` —
# the agent-server's uvicorn pid — and the whole dev stack went down). The Bug-7/16
# reserved-PORT containment blocks reserved-port BIND/KILL shapes by port number, but
# NOT a raw `kill <pid>` of a discovered PID. A build has NO legitimate need to signal
# host processes — its OWN foreground server is managed via the named preview/dev
# session (`shell_kill_process` → ShellSessionManager.kill_foreground) — so we
# BLANKET-refuse process-signal command shapes here. BEST-EFFORT command-pattern
# matching (like the reserved-port scan): trivially bypassable (renamed binary, raw
# os.kill in a python -c, env-indirection) — the robust long-term answer is a
# PID-namespaced/isolated backend (gVisor/container), which already has its OWN PID
# namespace so a kill there only hits sandbox processes (the container path is left
# UNCHANGED). This net stops the trivial stack-takedown. Each pattern fires only when
# the signalling verb is a COMMAND head — string start, or after a shell separator
# (`;`/`&`/`|`/`(`/backtick/`&&`/`||`) — so `kill`/`pkill`/`killall` buried inside an
# `echo`/quoted argument is not falsely refused, and `pytest -k kill_switch` (no word
# boundary; `-k` is a flag) never matches.
_CMD_HEAD = r"(?:^|[;&|`(\n]|\|\||&&)\s*"
_HOST_SIGNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(_CMD_HEAD + r"kill\b"),  # kill <pid> / kill -9 / kill -TERM / kill -s TERM
    re.compile(_CMD_HEAD + r"pkill\b"),  # pkill / pkill -f uvicorn
    re.compile(_CMD_HEAD + r"killall\b"),  # killall python
    re.compile(r"\bfuser\b[^\n]*\s-k\b"),  # fuser -k 8000/tcp (free a port by killing owner)
    re.compile(r"\bxargs\b[^\n|]*\bkill\b"),  # lsof -ti:PORT | xargs kill / xargs -r kill
)

# Actionable refusal — MUST start with "refused:" so system.py (_exec_outcome, Bug 16)
# promotes it to the ToolOutcome.error text the model actually sees (the loop drops tool
# `content`), instead of a bare "exited 126" the model can't recover from.
_HOST_SIGNAL_REFUSAL = (
    "refused: killing host processes is not permitted on this (process) backend — it "
    "shares the host, so a `kill <pid>` can take down the platform. Ports 8000/8800 are "
    "platform-owned; serve your preview on a non-reserved port such as 8080 (it runs in "
    "your named preview/dev session). To restart your OWN server, stop/start that session "
    "(shell_kill_process), never `kill`/`pkill`/`killall`/`fuser -k` a host PID or free a port."
)


# The capability generation this prohibition is true under. It names the BACKEND
# KIND, not an instance: the refusal is a property of running on a shared host, so
# it holds for every process-backend sandbox and must EXPIRE the moment the build
# moves to an isolated backend (which has its own PID namespace and is not routed
# through this check at all).
PROCESS_BACKEND_CAPABILITY_GENERATION = "sandbox-backend:process"

# Stable identity for the prohibition. Re-observing it re-emits this same key, so
# the View keeps exactly one live copy however many times the model retries.
HOST_SIGNAL_CONSTRAINT_KEY = "sandbox.host_signal_prohibited"


def host_signal_constraint(
    capability_generation: str = PROCESS_BACKEND_CAPABILITY_GENERATION,
) -> RuntimeConstraintDeclaration:
    """The typed form of the host-signal refusal.

    Deliberately built from the same constant the model is shown, so the typed
    directive and the tool-error text can never drift apart.
    """

    return RuntimeConstraintDeclaration(
        constraint_key=HOST_SIGNAL_CONSTRAINT_KEY,
        scope=PROCESS_BACKEND_CAPABILITY_GENERATION,
        guidance=(
            "Killing host processes is not permitted on this (process) backend: it "
            "shares the host, so signalling a host PID can take down the platform. "
            "Ports 8000/8800 are platform-owned."
        ),
        alternative=(
            "Serve a preview on a non-reserved port such as 8080, and stop/start "
            "your OWN server with the managed session tool (shell_kill_process) "
            "rather than kill/pkill/killall/fuser -k on a host PID."
        ),
        capability_generation=capability_generation,
        transient=False,
    )


def process_backend_signal_command_violation(command: str) -> str | None:
    """Bug 19 — return an actionable refusal string if `command` would SIGNAL a host
    process on the process (dev) backend, else None. BLANKET refusal of host-signal
    shapes (`kill`/`pkill`/`killall`/`fuser -k`/`… | xargs kill`); scoped to the
    process backend only (the container/isolated backend has its own PID namespace —
    a kill there only hits sandbox processes — and is NOT routed through this check).

    NOT CONTAINMENT (P1) — this is a BEST-EFFORT, DEV-ONLY string scan and is trivially
    bypassable (a renamed binary, a raw `os.kill` in `python -c`, env indirection). It
    must never be presented as real isolation. The REAL containment for prod/soak is the
    fail-closed production-validity gate (`preflight_build_sandbox_backend`) REFUSING the
    process backend outright, so a production/soak build is never on a shared-host box in
    the first place and never relies on this refusal. It survives only to give a local-dev
    build an actionable nudge instead of a silent stack takedown."""
    for pat in _HOST_SIGNAL_PATTERNS:
        if pat.search(command):
            return _HOST_SIGNAL_REFUSAL
    return None


async def cleanup_process_tmux_sessions(conversation_id: str) -> int:
    """Kill only process-backend tmux sessions owned by ``conversation_id``.

    Process sandboxes share the host tmux server, so their sessions outlive the Agent
    process that created them.  Conversation ids are namespaced to their first eight
    characters by ``ShellSessionManager``; support both the current ``disco-`` prefix
    and the legacy ``pmx-`` prefix.  Argument-vector subprocesses avoid a shell and an
    exact prefix check prevents one conversation's cleanup from touching another.

    Best-effort and idempotent: tmux may be absent, have no server, or race with a
    concurrent destroy.  Return the number of sessions successfully removed.
    """
    namespace = f"{conversation_id[:8]}-"
    prefixes = (f"disco-{namespace}", f"pmx-{namespace}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "list-sessions",
            "-F",
            "#{session_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return 0
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return 0

    removed = 0
    for session_name in stdout.decode("utf-8", errors="replace").splitlines():
        if not session_name.startswith(prefixes):
            continue
        try:
            kill = await asyncio.create_subprocess_exec(
                "tmux",
                "kill-session",
                "-t",
                session_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            continue
        if await kill.wait() == 0:
            removed += 1
    return removed


class ProcessSandboxInstance:
    """[CONTRACT boundary] An in-subprocess instance with a jailed workspace."""

    #: This backend shares the host network namespace (no isolation, §5.1), so
    #: 127.0.0.1:<reserved> IS the agent-server/app-server — control ports MUST stay
    #: reserved. Consumed by verify_app / finish preview targeting; do NOT key
    #: host-shared off `workspace_path` (isolated containers have one too).
    shares_host_network: bool = True

    def __init__(
        self,
        id: str,
        owner_id: str,
        conversation_id: str,
        spec: SandboxSpec,
        workspace: Path,
        relay: CapabilityRelayServer | None = None,
    ) -> None:
        self.id = id
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.spec = spec
        self._workspace = workspace.resolve()
        self._relay = relay
        self._destroyed = False

    @property
    def host_service_relay_url(self) -> str | None:
        """Loopback is reachable because this dev-only backend shares host networking."""
        if self._relay is None:
            return None
        host, port = self._relay.server_address[:2]
        return f"http://{host}:{port}"

    def _alive(self) -> None:
        if self._destroyed:
            raise SandboxError(f"sandbox instance {self.id} has been destroyed")

    def _resolve(self, path: str) -> Path:
        """Resolve `path` within the workspace; reject escapes (../, absolute)."""
        path = strip_redundant_workspace_prefix(path)  # ROOT-2: workspace/foo → foo
        target = (self._workspace / path).resolve()
        if target != self._workspace and self._workspace not in target.parents:
            # W1/codex round-6: a path escaping the jail is an ACCESS denial — type it so the
            # loop's `except (PermissionError, OSError)` bookkeeping handlers catch it instead
            # of a bare SandboxError escaping and crashing the task.
            raise SandboxPermissionError(f"path escapes workspace: {path!r}")
        return target

    def _clean_env(self) -> dict[str, str]:
        # No os.environ — nothing from the host (incl. any real secret) leaks in.
        # Shared with the jupyter kernel launcher (kernel.py) so the two cannot
        # drift — see base.clean_sandbox_env (W3 C-4).
        from .base import clean_sandbox_env

        return clean_sandbox_env(self._workspace)

    def _rewrite_workspace_paths(self, cmd: str) -> str:
        """ROOT-1 (slides spiral): make a literal ``/workspace`` resolve in the shell.

        The build/agent prompts tell the model files live in ``/workspace``, and the
        FILE tools honor that (``strip_redundant_workspace_prefix`` maps
        ``workspace/foo`` → ``foo``, jailed to the real dir). But this backend's shell
        runs with ``cwd`` = the real per-instance ``/tmp/disco-sbx-.../sbx_.../`` dir,
        which has NO literal ``/workspace`` — so a model command like
        ``ls /workspace/deck.pptx`` exits 2 and the agent hunts around (``find /`` …).
        On the container backends ``/workspace`` genuinely exists, so this only bites
        the process (dev) backend.

        Rewrite each genuine ``/workspace``-rooted path TOKEN (word-boundary — never
        ``/workspaces`` and never a mid-substring) to its ``shlex.quote``-d absolute
        real-workspace path. Relative paths already resolve via ``cwd``, so a NEW-file
        relative path (``echo hi > out.txt``) is untouched — only a ``/workspace``
        prefix is translated.

        [SECURITY — P1] The rewrite RESOLVES + JAILS each token through ``_resolve`` —
        the SAME jail the file tools use — so the shell ``/workspace`` semantics match
        the file-tool ``/workspace`` semantics exactly (single source of truth). A
        token whose ``..`` traversal escapes the workspace (e.g.
        ``/workspace/../../etc/passwd``) makes ``_resolve`` raise
        ``SandboxPermissionError`` and the whole command is rejected (fail closed) —
        the rewrite must never itself manufacture an out-of-jail absolute path.
        """

        # A token whose tail the SHELL computes at run time ("/workspace/$f",
        # "/workspace/${name}", "/workspace/`basename x`"). It cannot be resolved
        # here, and shlex.quote-ing it is actively WRONG: unquoted it suppresses
        # the expansion the model wrote, and inside existing quotes it injects
        # literal quote characters into the filename. Either way every path comes
        # back "missing" while a literal `ls` of the same file succeeds — the
        # environment contradicting itself, which is what sent counted seed 440023
        # into a read-loop it could not reason its way out of.
        def _sub(m: re.Match[str]) -> str:
            # _resolve strips the redundant /workspace prefix, joins onto the real
            # workspace root, resolves, and raises SandboxPermissionError on escape.
            # Parse shell escapes in this ONE matched token first.  In particular,
            # ``hero\ image.svg`` is one path; treating its space as a boundary
            # rewrote only ``hero\`` and silently manufactured a second argv token.
            token = m.group(0)
            if _SHELL_EXPANSION_RE.search(token):
                # Substitute ONLY the `/workspace` prefix and leave the shell's own
                # expansion — and the caller's quoting context — exactly as written.
                rest = token[len("/workspace") :]
                if ".." in rest:
                    raise SandboxPermissionError(
                        f"/workspace path with shell expansion may not traverse: {token!r}"
                    )
                root = str(self._workspace)
                if shlex.quote(root) != root:
                    # Bare substitution is only safe while the root needs no quoting;
                    # refuse loudly rather than emit a path that silently mis-parses.
                    raise SandboxPermissionError(
                        "cannot expand a /workspace path containing shell expansion "
                        f"because the workspace path requires quoting: {root!r}"
                    )
                return root + rest
            try:
                parsed = shlex.split(token, posix=True)
            except ValueError as exc:
                raise SandboxPermissionError(f"invalid /workspace path token: {token!r}") from exc
            if len(parsed) != 1:
                raise SandboxPermissionError(f"invalid /workspace path token: {token!r}")
            return shlex.quote(str(self._resolve(parsed[0])))

        return _WORKSPACE_TOKEN_RE.sub(_sub, cmd)

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self._alive()
        # Process-backend control-port containment (Bug 7) — BEST-EFFORT defense-in-
        # depth, NOT a guarantee. There is NO network namespace here, so the sandbox
        # shares the host's loopback with the agent-server; a command that binds/kills
        # a reserved control port (8000 agent-server / 8800 app-server / 5173 UI)
        # collides with + crashes it. This shell-string scan refuses the COMMON shapes
        # before running, but is trivially bypassable (raw socket.bind, renamed binary)
        # — the load-bearing Bug-7 fix is that verify no longer targets these ports
        # (see preview_target.resolve_preview_port); a netns is the robust follow-up.
        # Scoped to this backend only; an isolated container's 8000 is its own.
        #
        # NOTE (Bug 16) — the RECOVERY for a reserved-port preview SERVE does NOT live
        # here. This method receives an ALREADY-WRAPPED / arbitrary shell string (e.g.
        # `tmux send-keys -t … -l '…'`), which cannot be parsed quote/heredoc-safely; a
        # regex rewrite here would corrupt serve-shaped TEXT inside quotes. The remap is
        # therefore done upstream on the model's CLEAN `shell_exec` command, before it is
        # wrapped (see `ShellSessionManager.exec` + `remap_reserved_preview_serve`). What
        # stays here is the BEST-EFFORT containment REFUSAL — which only ever REJECTS
        # (never rewrites), so a false-refuse of an echo is harmless + recoverable, and it
        # is the actionable refuse-and-guide net for any reserved bind that reaches us.
        from disco.core.loop.preview_target import (
            reserved_control_ports,
            reserved_port_command_violation,
        )

        why = reserved_port_command_violation(cmd, reserved_control_ports())
        if why is not None:
            return ExecResult(exit_code=126, stdout="", stderr=why)
        # Bug 19 — BEST-EFFORT, DEV-ONLY refusal (NOT containment): blanket-reject host-
        # process-SIGNAL commands (`kill <pid>`/`pkill`/`killall`/`fuser -k`/`… | xargs
        # kill`) on the shared-host process backend, so a local-dev build gets an
        # actionable nudge instead of silently taking down the agent-server (or any host
        # process) via a raw `kill <pid>` of a PID it discovered. This is a string scan
        # and is trivially bypassable (renamed binary, `os.kill` in `python -c`); the REAL
        # containment is the production-validity gate refusing the process backend outright
        # (see process_backend_signal_command_violation's docstring). Runs AFTER the
        # reserved-port check so a reserved-port `fuser -k 8000` keeps its port-specific
        # message. Process backend ONLY — the container/isolated backend has its own PID
        # namespace and is not routed here. Returns BEFORE the subprocess launcher.
        signal_why = process_backend_signal_command_violation(cmd)
        if signal_why is not None:
            # Declare the prohibition as TYPED host authority alongside the text.
            # The refusal string alone is ordinary tool-error content: in the
            # k460000 diagnostic condensation forgot it and the model repeated the
            # same forbidden kill. A typed declaration carries its own identity, so
            # exactly one live copy stays near current context however many times it
            # is re-observed. Not transient -- this backend refuses the whole class.
            return ExecResult(
                exit_code=126,
                stdout="",
                stderr=signal_why,
                runtime_constraints=(host_signal_constraint(),),
            )
        cmd = self._rewrite_workspace_paths(cmd)
        proc = await asyncio.create_subprocess_exec(
            *bounded_exec_argv(cmd, timeout_s, python=sys.executable),
            cwd=str(self._workspace),
            env=self._clean_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 15)
        except asyncio.CancelledError:
            # Cancelling an auto-preview/tool task must not discard a live
            # bounded-exec helper. SIGTERM lets the helper forward termination
            # to its command process group and drain/close both pipes.
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.shield(asyncio.wait_for(proc.communicate(), timeout=5))
                except TimeoutError:
                    proc.kill()
                    await asyncio.shield(proc.wait())
            raise
        except TimeoutError:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
            # Report timed-out (not raised), consistent with the gVisor sibling: the
            # caller gets the flag + exit code rather than losing it to an exception.
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"command timed out after {timeout_s}s",
                timed_out=True,
            )
        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
        )

    async def read_file(self, path: str) -> bytes:
        self._alive()
        clean = strip_redundant_workspace_prefix(path)
        self._resolve(path)  # lexical jail before the descriptor walk

        def _read_securely() -> bytes:
            parts = [part for part in Path(clean).parts if part not in ("", ".")]
            if not parts:
                raise SandboxPermissionError(f"read_file {path!r}: invalid workspace path")
            dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            opened: list[int] = []
            try:
                current = os.open(self._workspace, dir_flags)
                opened.append(current)
                for part in parts[:-1]:
                    if part == "..":
                        if len(opened) == 1:
                            raise SandboxPermissionError(
                                f"read_file {path!r}: path escapes workspace"
                            )
                        os.close(opened.pop())
                        current = opened[-1]
                        continue
                    current = os.open(part, dir_flags, dir_fd=current)
                    opened.append(current)
                if parts[-1] == "..":
                    raise SandboxPermissionError(
                        f"read_file {path!r}: directory paths are not readable files"
                    )
                descriptor = os.open(parts[-1], file_flags, dir_fd=current)
                opened.append(descriptor)
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise SandboxPermissionError(
                        f"read_file {path!r}: only regular, non-symlink files may be read"
                    )
                if info.st_size > MAX_SANDBOX_READ_BYTES:
                    raise OSError(
                        errno.EFBIG,
                        f"read_file {path!r} exceeds the "
                        f"{MAX_SANDBOX_READ_BYTES}-byte transfer cap",
                    )
                chunks: list[bytes] = []
                remaining = MAX_SANDBOX_READ_BYTES + 1
                while remaining:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                if len(data) > MAX_SANDBOX_READ_BYTES:
                    raise OSError(
                        errno.EFBIG,
                        f"read_file {path!r} grew beyond the transfer cap while reading",
                    )
                return data
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
                    raise SandboxPermissionError(
                        f"read_file {path!r}: symlink and non-regular paths are denied"
                    ) from exc
                raise
            finally:
                for descriptor in reversed(opened):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_read_securely), timeout=SANDBOX_READ_TIMEOUT_S
            )
        except TimeoutError as exc:
            raise OSError(
                errno.ETIMEDOUT,
                f"read_file {path!r}: exceeded the {SANDBOX_READ_TIMEOUT_S}s read timeout",
            ) from exc

    async def write_file(self, path: str, data: bytes) -> None:
        self._alive()
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def delete_file(self, path: str) -> None:
        """Unlink one regular file through no-follow directory descriptors."""
        self._alive()
        path = strip_redundant_workspace_prefix(path)

        def _delete_securely() -> None:
            import posixpath

            normalized = posixpath.normpath(path.replace("\\", "/"))
            if normalized in {"", ".", ".."} or normalized.startswith("../"):
                raise SandboxPermissionError(f"delete_file path escapes workspace: {path!r}")
            parts = [part for part in normalized.split("/") if part not in {"", "."}]
            dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            opened: list[int] = []
            try:
                current = os.open(self._workspace, dir_flags)
                opened.append(current)
                for part in parts[:-1]:
                    current = os.open(part, dir_flags, dir_fd=current)
                    opened.append(current)
                info = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise SandboxPermissionError(
                        f"delete_file {path!r}: only regular, non-symlink files may be deleted"
                    )
                os.unlink(parts[-1], dir_fd=current)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
                    raise SandboxPermissionError(
                        f"delete_file {path!r}: symlink and non-regular paths are denied"
                    ) from exc
                raise
            finally:
                for descriptor in reversed(opened):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

        await asyncio.to_thread(_delete_securely)

    async def atomic_write(self, path: str, data: bytes) -> None:
        """CD-TOOLS-3: write `data` to `path` atomically — a tmp in the SAME dir + os.replace, so
        a reader/crash never observes a partially-written file (os.replace is atomic on the same
        filesystem). Used by safe_write_file + exact_replace for the final commit.

        Security (codex round-1): the tmp is created with tempfile.mkstemp — a RANDOM name +
        O_CREAT|O_EXCL|O_NOFOLLOW semantics — so a model CANNOT pre-create a predictable
        ``<target>.disco-tmp`` symlink that the write would follow into a governed
        path. os.replace targets the link itself (never follows a symlinked target),
        so the final swap is safe too."""
        self._alive()
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".disco-tmp-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)  # clean a leftover tmp if os.replace failed

    async def resolve_relpath(self, path: str) -> str:
        """CD-TOOLS-3/4: the REAL (symlink-followed) path of `path`, RELATIVE to the workspace
        root, as a forward-slash string. Lets the governed-artifact guard see through a symlink
        (link.json -> .disco/appspec.json) — `_resolve` already follows symlinks + rejects jail
        escapes. async to match the container backend's exec-based resolve; the body is a cheap
        in-process path op. Raises (SandboxPermissionError) on an escaping path,
        like the file ops."""
        target = self._resolve(path)
        if target == self._workspace:
            return "."
        return target.relative_to(self._workspace).as_posix()

    async def list_dir(self, path: str) -> list[str]:
        self._alive()
        return sorted(p.name for p in self._resolve(path).iterdir())

    async def list_dir_bounded(self, path: str, limit: int) -> tuple[list[tuple[str, str]], bool]:
        """List without retaining more than ``limit + 1`` directory entries."""

        self._alive()
        if limit <= 0 or limit > MAX_SANDBOX_LIST_ENTRIES:
            raise ValueError(f"list limit must be between 1 and {MAX_SANDBOX_LIST_ENTRIES}")
        found: list[tuple[str, str]] = []
        with os.scandir(self._resolve(path)) as entries:
            for entry in entries:
                if entry.is_symlink():
                    kind = "other"
                elif entry.is_file(follow_symlinks=False):
                    kind = "file"
                elif entry.is_dir(follow_symlinks=False):
                    kind = "directory"
                else:
                    kind = "other"
                found.append((entry.name, kind))
                if len(found) > limit:
                    return sorted(found[:limit]), True
        return sorted(found), False

    async def file_exists(self, path: str) -> bool:
        """[B4] Workspace-jailed existence check. The process workspace lives on
        the host FS, so this is a direct `Path.exists()` against the resolved
        target. A path that escapes the jail is False (not raised — the predicate
        named an out-of-scope path, which simply does not exist *in* the
        workspace); a plain absence is False."""
        self._alive()
        try:
            clean = strip_redundant_workspace_prefix(path)
            current = self._workspace
            for part in Path(clean).parts:
                current /= part
                if current.is_symlink():
                    return False
            target = self._resolve(path)
        except SandboxError:
            return False
        return target.is_file()

    @property
    def workspace_path(self) -> str | None:
        """W5 — expose the workspace root for C18 / C1c predicate resolution.
        Process backend: always an absolute host path (the temp dir is on the host).
        Container backends return None (they have a separate FS namespace)."""
        return str(self._workspace)

    def display_url(self) -> str | None:
        return None  # the process backend has no display

    def expose_port(self, port: int) -> str | None:
        """Dev-mode usability: host processes bind host ports directly, so hand
        back the local URL when the port is actually bound. No isolation boundary
        to defend on this backend. Generic services stay within USER_PORTS; the
        platform-owned Preview lifecycle may additionally expose its bounded
        host-managed range."""
        from disco.core.loop.preview_target import (
            is_managed_host_preview_port,
            reserved_control_ports,
        )

        from ._container import USER_PORTS

        # Process-backend containment (Bug 7): never advertise a reserved control
        # port (8000 = agent-server, 8800 = app-server) as a user preview URL — on
        # the shared host that port is the server's own, not the build's app.
        if port in reserved_control_ports():
            return None
        if port not in USER_PORTS and not is_managed_host_preview_port(port):
            return None
        import socket

        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                pass
        except OSError:
            return None
        return f"http://127.0.0.1:{port}"

    async def destroy(self) -> None:
        if self._destroyed:
            return

        if self._relay is not None:
            await asyncio.to_thread(self._relay.shutdown)
            self._relay.server_close()
            self._relay = None

        # Container backends die with their private tmux server.  The process backend
        # shares the host server and must remove its exact current/legacy namespaces.
        await cleanup_process_tmux_sessions(self.conversation_id)

        await self._terminate_workspace_processes()

        shutil.rmtree(self._workspace, ignore_errors=True)
        self._destroyed = True

    def _workspace_process_pids(self) -> set[int]:
        """Find descendants by workspace capability or cwd, excluding shared tmux."""
        proc_root = Path("/proc")
        if not proc_root.is_dir():
            return set()
        marker = b"DISCO_WORKSPACE=" + str(self._workspace).encode()
        selected: set[int] = set()
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                comm = (entry / "comm").read_text().strip().lower()
            except OSError:
                continue
            if comm.startswith("tmux"):
                continue
            belongs = False
            try:
                environ = (entry / "environ").read_bytes().split(b"\0")
                belongs = marker in environ
            except OSError:
                pass
            if not belongs:
                try:
                    cwd = Path(os.readlink(entry / "cwd"))
                    belongs = cwd == self._workspace or self._workspace in cwd.parents
                except (OSError, RuntimeError):
                    pass
            if belongs:
                selected.add(pid)
        return selected

    async def _terminate_workspace_processes(self) -> None:
        """Terminate detached process-kernel/shell descendants before rmtree."""
        pids = self._workspace_process_pids()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = asyncio.get_running_loop().time() + 2.0
        survivors = pids
        while survivors and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
            survivors &= self._workspace_process_pids()
        for pid in survivors:
            # Re-identify immediately before SIGKILL so PID reuse cannot target an
            # unrelated process after the grace period.
            if pid not in self._workspace_process_pids():
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = asyncio.get_running_loop().time() + 0.5
        while survivors and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
            survivors &= self._workspace_process_pids()
        if survivors:
            raise SandboxUnavailableError(
                "process sandbox termination could not be confirmed for "
                f"workspace-owned pid(s): {sorted(survivors)}"
            )


class ProcessSandboxService:
    """[CONTRACT boundary] Creates process-backed instances (dev)."""

    name = "process"
    # EPIC H (§1.4/§9.3): NOT production-valid. This backend runs builds as bare host
    # subprocesses in the SHARED host PID + network namespace — it is the source of the
    # isolation incidents (a build `kill <pid>` took down the agent-server). The
    # production / Build-Soak path refuses it (`require_production_valid_backend`); it
    # remains the convenient default for local dev only.
    is_production_valid = False

    def __init__(self, root: str | None = None, *, config: SandboxConfig | None = None) -> None:
        self._root = Path(root or tempfile.mkdtemp(prefix="disco-sbx-")).resolve()
        self._cfg = config or SandboxConfig(backend="process")
        self._instances: dict[str, ProcessSandboxInstance] = {}

    async def create(
        self, spec: SandboxSpec | None, *, owner_id: str, conversation_id: str
    ) -> SandboxInstance:
        # Preserve the lightweight dev/integration convenience accepted by the
        # process backend: an omitted spec means the ordinary sealed defaults.
        # Production-valid backends still require their explicit deployment
        # configuration at the service boundary.
        spec = spec or SandboxSpec()
        instance_id = f"sbx_{uuid.uuid4().hex}"
        workspace = self._root / instance_id
        workspace.mkdir(parents=True, exist_ok=True)
        relay: CapabilityRelayServer | None = None
        try:
            if spec.host_services:
                relay = CapabilityRelayServer("127.0.0.1", 0, self._cfg.host_service_upstream)
                serve_in_thread(relay)
            instance = ProcessSandboxInstance(
                instance_id, owner_id, conversation_id, spec, workspace, relay
            )
        except Exception as exc:
            if relay is not None:
                relay.server_close()
            shutil.rmtree(workspace, ignore_errors=True)
            if isinstance(exc, RelayConfigurationError):
                raise SandboxUnavailableError(str(exc)) from exc
            raise
        self._instances[instance_id] = instance
        return instance

    async def get(self, instance_id: str) -> SandboxInstance | None:
        return self._instances.get(instance_id)

    async def healthcheck(self) -> None:
        """[W-48] The process (dev) backend runs tools directly on the host — there is
        no remote endpoint or container runtime to reach, so the preflight only
        confirms the workspace ROOT is present + writable (the one local resource it
        needs). Raises ``SandboxUnavailableError`` naming the root on failure; returns
        None on success."""

        def _probe() -> None:
            try:
                self._root.mkdir(parents=True, exist_ok=True)
                probe = self._root / ".disco-healthcheck"
                probe.write_text("ok")
                probe.unlink()
            except Exception as exc:  # noqa: BLE001 — map to a typed infra error
                raise SandboxUnavailableError(
                    f"process sandbox workspace root {self._root} is not writable: {exc}"
                ) from exc

        await asyncio.to_thread(_probe)

    async def list_live_instances(self) -> list[str]:
        """Process backend has no container layer — returns empty."""
        return []

    async def destroy_by_conversation(self, conversation_id: str) -> None:
        """Remove process resources even when an Agent restart lost instance handles."""
        await cleanup_process_tmux_sessions(conversation_id)

    async def sweep_stale_workspaces(self, max_age_s: float = 7 * 86400) -> int:
        """Delete per-instance workspace dirs under _root that haven't been touched in
        max_age_s seconds (default 7 days). Returns the count removed. Safe to call at
        startup — only removes dirs that are genuinely old (not fresh instances)."""
        removed = 0
        now = time.time()
        try:
            for child in self._root.iterdir():
                if not child.is_dir():
                    continue
                try:
                    age_s = now - child.stat().st_mtime
                    if age_s >= max_age_s:
                        shutil.rmtree(child, ignore_errors=True)
                        removed += 1
                except Exception:  # noqa: BLE001 — one bad dir must not abort the sweep
                    pass
        except Exception:  # noqa: BLE001 — root may not exist
            pass
        return removed

    async def sweep_stale_roots(self, max_age_s: float = 86400) -> int:
        """Remove orphaned /tmp/disco-sbx-* (and legacy pmx-sbx-*) root dirs left by
        prior process runs.

        Scans the parent of _root (typically /tmp) for directories whose name
        starts with ``disco-sbx-`` or legacy ``pmx-sbx-`` and whose mtime is older
        than *max_age_s* seconds (default 1 day).  The live ``_root`` is always
        excluded.

        Safety properties:
        - Only touches entries whose name starts with ``disco-sbx-`` or legacy
          ``pmx-sbx-``; all other siblings are unconditionally skipped.
        - Never follows or removes symlinks (uses ``lstat`` + ``is_symlink``
          guard before ``rmtree``).
        - Best-effort: per-entry errors are swallowed so a busy/unremovable dir
          cannot abort the overall sweep.

        Returns the count of roots removed.
        """
        removed = 0
        now = time.time()
        parent = self._root.parent  # typically /tmp
        # B5 dual-prefix: clean BOTH the new disco-sbx-* roots AND the legacy
        # pmx-sbx-* roots left by pre-rename runs (str.startswith takes a tuple).
        prefixes = ("disco-sbx-", "pmx-sbx-")
        try:
            for sibling in parent.iterdir():
                if not sibling.name.startswith(prefixes):
                    continue  # prefix guard — never touch unrelated dirs
                if sibling == self._root:
                    continue  # never delete the live root
                if sibling.is_symlink():
                    continue  # never follow or remove symlinks
                if not sibling.is_dir():
                    continue
                try:
                    age_s = now - sibling.lstat().st_mtime
                    if age_s >= max_age_s:
                        shutil.rmtree(sibling, ignore_errors=True)
                        removed += 1
                except Exception:  # noqa: BLE001 — one bad dir must not abort the sweep
                    pass
        except Exception:  # noqa: BLE001 — parent may not exist / unreadable
            pass
        return removed
