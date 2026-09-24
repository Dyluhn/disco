"""A delayed pane read cannot establish idle for a newer shell operation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from disco.agent_server.preview_manager import PreviewManager
from disco.agent_server.preview_models import PreviewSession, PreviewStatus
from disco.tools.sandbox import shell_sessions
from disco.tools.sandbox.shell_sessions import ShellSessionManager
from disco.tools.sandbox.shell_sessions_parts import exec_dispatch

_PROMPT = "__DISCO_PS1__0__$ "
_COMMAND = "PORT=8000 node server.js"


class _Shell:
    def __init__(self):
        self.manager = ShellSessionManager(self.no_instance)
        self.commands = []
        self.output = _PROMPT
        self.capture = self.current_capture
        self.send = self.current_send
        self.manager._run_tmux = self.tmux
        self.manager._run_tmux_safe = self.tmux

    async def no_instance(self):
        raise AssertionError("Unit test must not access a runtime")

    async def current_capture(self):
        return 0, self.output

    async def current_send(self, command):
        if command.endswith(" Enter"):
            self.output = "service-desk listening on port 8000"
        elif command.endswith(" C-c"):
            self.output = _PROMPT
        elif "send-keys" in command:
            self.output = _PROMPT + "pending command text"
        return 0, ""

    async def tmux(self, command):
        self.commands.append(command)
        if "capture-pane" in command:
            return await self.capture()
        return await self.send(command)

    async def dispatch(self):
        return await exec_dispatch._dispatch(self.manager, "preview", "disco-preview", _COMMAND)


async def _delay_view(shell, snapshot=(0, _PROMPT)):
    captured = asyncio.Event()
    release = asyncio.Event()
    original = shell.capture

    async def delayed():
        shell.capture = original
        captured.set()
        await release.wait()
        return snapshot

    shell.capture = delayed
    task = asyncio.create_task(shell.manager.view("preview"))
    await captured.wait()
    return task, release


@pytest.mark.asyncio
async def test_delayed_prompt_cannot_skip_interrupt_in_real_preview_stop_chain(monkeypatch):
    """G41: stale idle metadata must not bypass the selected foreground stop."""
    monkeypatch.setattr(shell_sessions, "_POLL_S", 0)
    shell = _Shell()
    view, release = await _delay_view(shell)
    await shell.dispatch()
    release.set()
    assert (await view).running
    assert shell.manager._foreground_state["preview"] == "busy"
    assert (await shell.manager.view("preview")).running

    previews = PreviewManager(SimpleNamespace(sessions=shell.manager), port_pool=[8000])
    preview = PreviewSession(
        name="preview",
        port=8000,
        command=_COMMAND,
        exec_dir="/workspace",
        intent={"command": "node server.js"},
        status=PreviewStatus.RUNNING,
    )
    previews._sessions["preview"] = preview
    assert await previews.stop("preview") == ["preview"]
    assert any(command == "send-keys -t disco-preview C-c" for command in shell.commands)
    assert not any("kill-session" in command for command in shell.commands)
    assert preview.status is PreviewStatus.STOPPED


@pytest.mark.asyncio
@pytest.mark.parametrize("redispatch", [False, True])
@pytest.mark.parametrize("snapshot", [(0, _PROMPT), (1, "no such old session")])
async def test_recreation_invalidates_inflight_view_even_without_prior_dispatch(
    redispatch, snapshot
):
    shell = _Shell()
    view, release = await _delay_view(shell, snapshot)
    shell.manager.reset_known_sessions()
    if redispatch:
        await shell.dispatch()
    release.set()
    assert (await view).running  # old instance's idle/missing reply is unavailable evidence
    assert shell.manager._foreground_state.get("preview") != "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("press_enter", [False, True])
async def test_delayed_prompt_cannot_undo_explicit_shell_write(press_enter):
    shell = _Shell()
    shell.manager._foreground_state["preview"] = "idle"
    view, release = await _delay_view(shell)
    await shell.manager.write("preview", "echo changed", press_enter)
    release.set()
    assert (await view).running
    assert shell.manager._foreground_state.get("preview") != "idle"


@pytest.mark.asyncio
async def test_same_command_busy_idle_busy_cycle_does_not_reuse_view_authority():
    shell = _Shell()
    await shell.dispatch()
    shell.output = _PROMPT  # the first process exits while its view reply is delayed
    view, release = await _delay_view(shell)
    assert not (await shell.manager.view("preview")).running
    await shell.dispatch()  # identical command/name, distinct operation identity
    release.set()
    assert (await view).running
    assert shell.manager._foreground_state["preview"] == "busy"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["dispatch", "write-enter", "write-no-enter"])
async def test_poll_during_pending_delivery_does_not_publish_old_prompt_as_idle(operation):
    shell = _Shell()
    pending = asyncio.Event()
    release = asyncio.Event()
    original_send = shell.send

    async def held_send(command):
        if "send-keys" in command and " -l " in command:
            pending.set()
            await release.wait()
        return await original_send(command)

    shell.send = held_send
    action = asyncio.create_task(
        shell.dispatch()
        if operation == "dispatch"
        else shell.manager.write("preview", "echo changed", operation == "write-enter")
    )
    await pending.wait()
    assert (await shell.manager.view("preview")).running
    assert shell.manager._foreground_state.get("preview") != "idle"
    release.set()
    await action


@pytest.mark.asyncio
async def test_old_private_completion_cannot_mark_later_write_idle(monkeypatch):
    shell = _Shell()
    poll_started = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(exec_dispatch.secrets, "token_hex", lambda _: "oldtask")

    async def prepared(*_):
        return None

    async def unchanged(command):
        return command

    async def held_completion(*_):
        poll_started.set()
        await release.wait()
        return "done\n__DISCO_DONE_oldtask__0__\n" + _PROMPT

    monkeypatch.setattr(shell.manager, "ensure", prepared)
    monkeypatch.setattr(shell.manager, "_remap_reserved_preview_serve", unchanged)
    monkeypatch.setattr(exec_dispatch, "_poll_until_done", held_completion)
    execution = asyncio.create_task(shell.manager.exec("preview", "echo old", None))
    await poll_started.wait()
    await shell.manager.write("preview", "echo next", True)
    release.set()
    assert (await execution).exit_code == 0
    assert shell.manager._foreground_state["preview"] == "busy"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["dispatch", "write"])
async def test_cancelled_delivery_retires_pending_read_barrier(operation):
    shell = _Shell()
    pending = asyncio.Event()

    async def held_send(command):
        if "send-keys" in command:
            pending.set()
            await asyncio.Event().wait()
        return 0, ""

    shell.send = held_send
    action = asyncio.create_task(
        shell.dispatch()
        if operation == "dispatch"
        else shell.manager.write("preview", "echo changed", True)
    )
    await pending.wait()
    action.cancel()
    with pytest.raises(asyncio.CancelledError):
        await action
    assert "preview" not in shell.manager._foreground_state
    assert not shell.manager._foreground_epochs["preview"].pending
    # A new successful read remains possible; cancellation cannot leave a
    # permanent 'delivery pending' barrier after the transport is gone.
    assert not (await shell.manager.view("preview")).running


@pytest.mark.asyncio
async def test_stale_busy_read_preserves_newly_proven_background_idle(monkeypatch):
    shell = _Shell()
    view, release = await _delay_view(shell, (0, "old foreground output"))
    monkeypatch.setattr(exec_dispatch.secrets, "token_hex", lambda _: "background")

    async def prepared(*_):
        return None

    async def unchanged(command):
        return command

    async def completed(*_):
        return "command echo\n__DISCO_DONE_background__0__\n" + _PROMPT + "background stderr"

    monkeypatch.setattr(shell.manager, "ensure", prepared)
    monkeypatch.setattr(shell.manager, "_remap_reserved_preview_serve", unchanged)
    monkeypatch.setattr(exec_dispatch, "_poll_until_done", completed)
    outcome = await shell.manager.exec("preview", "sleep 10 &", None)
    assert outcome.exit_code == 0
    assert not outcome.running
    release.set()
    assert not (await view).running
    assert "already idle" in await shell.manager.kill_foreground("preview")
    assert not any(" C-c" in command or "kill-session" in command for command in shell.commands)
