"""Running one `exec()` round-trip and attributing its outcome (`ShellSessionManager`).

`exec_command` dispatches one command into a tmux session, polls for its
private completion record, and returns the outcome; `record_persistent_if_match`
updates the C3 persistent-server registry from that outcome so a real dev
server (vite/express/uvicorn/…) survives a sandbox recreate.

Calls that a test monkeypatches directly on the manager instance
(`mgr._background_port_owner`, `mgr.exec`, `mgr.ensure`, …) are resolved
through the passed `manager` at call time — never a sibling function in this
module called directly — so the patch still takes effect when the call
originates here instead of the class body. The four tuning constants
(`_EXEC_WAIT_S`/`_POLL_S`/`_EXEC_RETURN_CHARS`/`_BACKGROUND_OWNER_ATTEMPTS`/
`_BACKGROUND_OWNER_INTERVAL_S`) that tests monkeypatch on the `shell_sessions`
module are resolved the same way, through the imported module at call time.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
from typing import TYPE_CHECKING

from . import text

if TYPE_CHECKING:
    from ..shell_sessions import ExecOutcome, ShellSessionManager


async def remap_reserved_preview_serve(manager: ShellSessionManager, command: str) -> str:
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
        inst = await manager._get_instance()
    except Exception:  # noqa: BLE001 — instance unavailable; leave command unchanged
        return command
    if getattr(inst, "workspace_path", None) is None:
        return command  # isolated backend — 8000 is the box's own app, keep it
    from disco.core.loop.preview_target import remap_reserved_preview_serve as _remap

    remapped = _remap(command)
    return remapped if remapped is not None else command


async def _prepare_session(manager: ShellSessionManager, name: str, exec_dir: str | None) -> None:
    try:
        await manager.ensure(name, exec_dir)
    except Exception as e:
        raise RuntimeError(
            f"session '{name}' could not be reached (sandbox shell unavailable or "
            f"recreated) — retry once; if it persists, use a new session name or "
            f"server_start. {e}"
        ) from e
    await _reject_if_busy(manager, name)


async def _reject_if_busy(manager: ShellSessionManager, name: str) -> None:
    from .. import shell_sessions

    if not await manager.is_busy(name):
        return
    full = manager._full_name(name)
    rc, pane_out = await manager._run_tmux_safe(
        f"list-panes -t {shlex.quote(full)} -F '#{{pane_current_command}}'"
    )
    cmd_name = pane_out.strip().splitlines()[0].strip() if rc == 0 and pane_out.strip() else ""
    if cmd_name:
        raise shell_sessions.SessionBusy(
            f"session '{name}' is busy running '{cmd_name}' — "
            "wait for it (shell_wait), "
            "interact with it (shell_write_to_process), "
            "kill it (shell_kill_process), "
            "or use a different session name."
        )
    raise shell_sessions.SessionBusy(
        f"Previous command not finished in session '{name}'. Wait for it (shell_wait), "
        "interact with it (shell_write_to_process), kill it (shell_kill_process), "
        "or use a different session name."
    )


async def _capture_delta(manager: ShellSessionManager, full: str, pre_cap: str) -> str:
    _, post_cap = await manager._run_tmux_safe(f"capture-pane -J -t {shlex.quote(full)} -p -S -")
    post_cap = post_cap.rstrip("\r\n")
    if post_cap.startswith(pre_cap):
        return post_cap[len(pre_cap) :]
    return post_cap


async def _dispatch(
    manager: ShellSessionManager, name: str, full: str, command: str
) -> tuple[str, bool, tuple[bool, int | None] | None, str, re.Pattern[str]]:
    _, pre_cap = await manager._run_tmux_safe(f"capture-pane -J -t {shlex.quote(full)} -p -S -")
    pre_cap = pre_cap.rstrip("\r\n")

    backgrounded = text.is_backgrounded(command)
    port = manager._classify_persistent_server(command) if backgrounded else None
    background_owner_before = (
        await manager._background_port_owner(name, port) if port is not None else None
    )
    token = secrets.token_hex(16)
    dispatched, completion_re = text.completion_dispatch(command, token)

    # A transport error can occur after tmux accepted the bytes.  From this
    # point until a private completion record is observed, cached idle proof
    # is invalid even when the first send call raises.
    manager._foreground_state[name] = "busy"
    try:
        await manager._run_tmux(f"send-keys -t {shlex.quote(full)} -l {shlex.quote(dispatched)}")
    except Exception:
        manager._foreground_state.pop(name, None)
        raise
    try:
        await manager._run_tmux(f"send-keys -t {shlex.quote(full)} Enter")
    except Exception:
        # Command text is now sitting unsubmitted at the prompt. It is neither
        # proven idle nor running; clearing proof prevents a later exec from
        # appending a second command and accidentally submitting the concatenation.
        manager._foreground_state.pop(name, None)
        raise
    return pre_cap, backgrounded, background_owner_before, dispatched, completion_re


async def _poll_until_done(
    manager: ShellSessionManager,
    full: str,
    pre_cap: str,
    completion_re: re.Pattern[str],
    deadline: float,
) -> str | None:
    from .. import shell_sessions

    loop = asyncio.get_running_loop()
    # Wall-clock deadline, NOT a poll-count accumulator: over Docker-over-SSH each
    # capture-pane round-trip costs 1-2s that a `+= _POLL_S` counter never sees,
    # silently stretching "15s" to a minute (caught live on the gvisor backend).
    while loop.time() < deadline:
        await asyncio.sleep(shell_sessions._POLL_S)
        delta = await _capture_delta(manager, full, pre_cap)
        # Only this exec's private, line-anchored completion record is proof.
        # The public prompt marker can occur in command echo or process output.
        if completion_re.search(delta) is not None:
            return delta
    return None


async def exec_command(
    manager: ShellSessionManager, name: str, command: str, exec_dir: str | None
) -> ExecOutcome:
    from .. import shell_sessions

    # Bug 16: remap a reserved-port preview SERVE on the CLEAN command, before the
    # tmux-wrap below — so a model serving on 8000/5173 lands on a safe, conversation-
    # owned port the verify resolver can target (instead of STUCK verify_no_progress),
    # while the Bug-7 crash vector stays closed (we never wrap/run a reserved bind).
    command = await manager._remap_reserved_preview_serve(command)
    await _prepare_session(manager, name, exec_dir)

    full = manager._full_name(name)
    pre_cap, backgrounded, background_owner_before, dispatched, completion_re = await _dispatch(
        manager, name, full, command
    )

    deadline = asyncio.get_running_loop().time() + shell_sessions._EXEC_WAIT_S
    delta = await _poll_until_done(manager, full, pre_cap, completion_re, deadline)

    if delta is not None:
        cleaned, exit_code = text.strip_output(
            delta, completion_re=completion_re, echoed_dispatch=dispatched
        )
        outcome = shell_sessions.ExecOutcome(
            running=False,
            exit_code=exit_code,
            output=cleaned[-shell_sessions._EXEC_RETURN_CHARS :],
        )
        manager._foreground_state[name] = "idle"
        recorded = await record_persistent_if_match(
            manager,
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

    final_delta = await _capture_delta(manager, full, pre_cap)
    cleaned_running, _ = text.strip_output(final_delta, echoed_dispatch=dispatched)
    outcome = shell_sessions.ExecOutcome(
        running=True,
        exit_code=None,
        output=cleaned_running[-shell_sessions._EXEC_RETURN_CHARS :],
        note="still running after 15s — use shell_view / shell_wait",
    )
    await record_persistent_if_match(
        manager,
        name,
        command,
        exec_dir,
        outcome,
        backgrounded=backgrounded,
        background_owner_before=background_owner_before,
    )
    return outcome


async def record_persistent_if_match(
    manager: ShellSessionManager,
    name: str,
    command: str,
    exec_dir: str | None,
    outcome: ExecOutcome,
    *,
    backgrounded: bool,
    background_owner_before: tuple[bool, int | None] | None = None,
) -> bool:
    """Update `manager._persistent_servers` based on a finished exec() outcome.

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
    from .. import shell_sessions

    port = manager._classify_persistent_server(command)
    if backgrounded:
        # A missed/delayed prompt must not bypass attribution: background
        # commands ALWAYS need exact listener ownership, even when exec() times
        # out and reports running=True. This excludes a foreign auto-preview.
        after = (
            await manager._background_port_owner(name, port, wait_for_listener=True)
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
            manager._persistent_servers[name] = shell_sessions.PersistentServer(
                name=name, command=command, exec_dir=exec_dir, port=port
            )
            return True
        # A failed/redundant background launch cannot disprove an already
        # recorded server.  Preserve it, but return False so the new launch is
        # never attributed to the pre-existing listener.
        return False
    elif outcome.running and port is not None:
        manager._persistent_servers[name] = shell_sessions.PersistentServer(
            name=name, command=command, exec_dir=exec_dir, port=port
        )
        return True
    # Not running, or not a port-binding command — forget any prior entry.
    manager._persistent_servers.pop(name, None)
    return False


async def background_port_owner(
    manager: ShellSessionManager, name: str, port: int, *, wait_for_listener: bool = False
) -> tuple[bool, int | None]:
    """Return ``(probe_conclusive, exact-session-listener-pid)`` boundedly."""
    from .. import shell_sessions
    from ..port_owner import port_owner

    observed_absence = False
    for attempt in range(shell_sessions._BACKGROUND_OWNER_ATTEMPTS):
        try:
            inst = await manager._get_instance()
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
            return True, owner.pid if owner.session == manager._full_name(name) else None
        if attempt + 1 < shell_sessions._BACKGROUND_OWNER_ATTEMPTS:
            await asyncio.sleep(shell_sessions._BACKGROUND_OWNER_INTERVAL_S)
    return (True, None) if observed_absence else (False, None)
