"""Private shell-operation identities for asynchronous foreground observations.

A pane capture begun before a write/dispatch/recreation cannot establish the
new operation's idle state. Pending writes also cannot publish the old prompt
as idle before Enter has been delivered. These identities are process-local;
they carry no authority across sandbox recreation.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..shell_sessions import SessionView, ShellSessionManager


@dataclass(frozen=True)
class _ForegroundEpoch:
    token: object
    pending: bool


def _begin(
    manager: ShellSessionManager, name: str, state: str | None, *, token: object | None = None
) -> object:
    token = object() if token is None else token
    manager._foreground_epochs[name] = _ForegroundEpoch(token, pending=True)
    _set_state(manager, name, state)
    return token


def _settle(manager: ShellSessionManager, name: str, token: object, state: str | None) -> None:
    epoch = manager._foreground_epochs.get(name)
    if epoch is not None and epoch.token is token:
        manager._foreground_epochs[name] = _ForegroundEpoch(token, pending=False)
        _set_state(manager, name, state)


def _set_state(manager: ShellSessionManager, name: str, state: str | None) -> None:
    if state is None:
        manager._foreground_state.pop(name, None)
    else:
        manager._foreground_state[name] = state


async def _view(manager: ShellSessionManager, name: str, tail_chars: int) -> SessionView:
    from ..shell_sessions import SessionView
    from .text import _MARKER_RE

    full = manager._full_name(name)
    epochs = manager._foreground_epochs
    epoch = epochs.get(name)
    code, out = await manager._run_tmux_safe(f"capture-pane -t {shlex.quote(full)} -p -S -")
    fresh = epochs is manager._foreground_epochs and epochs.get(name) is epoch
    if not fresh:
        return SessionView(
            running=manager._foreground_state.get(name) != "idle", output=out[-tail_chars:]
        )
    if code != 0:
        if name in manager._lost_sessions:
            return SessionView(running=False, output="session lost: sandbox was recreated")
        return SessionView(running=False, output=f"Session not found or error: {out}")

    out = out.rstrip("\r\n")
    lines = out.split("\n")
    running = manager._foreground_state.get(name) != "idle"
    if not (epoch is not None and epoch.pending) and lines:
        m = _MARKER_RE.search(lines[-1])
        if m:
            running = False
            manager._foreground_state[name] = "idle"

    return SessionView(running=running, output=out[-tail_chars:])
