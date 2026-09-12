"""Persistent named shell sessions (tmux-backed) — BP-01."""

from __future__ import annotations

from typing import Any

from disco.core import SecurityRisk
from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from ..sandbox.shell_sessions import SessionBusy
from ._outcomes import fail_outcome as _fail
from ._shell_caps import cap_shell_observation, sanitize_execution_output


def _reject_reserved_session(session: str) -> ToolOutcome | None:
    """W3 C-5: `__`-prefixed session names are RESERVED for the platform's
    internal sessions (e.g. `__kernel`, whose pane holds the kernel-gateway
    launch line and its auth token). A model-driven shell tool must never reach
    them — otherwise `shell_view(session="__kernel")` exfiltrates the token and
    `shell_exec`/`_kill` could hijack or tear down the gateway. Returns a refusal
    outcome for a reserved name, else None."""
    if session.startswith("__"):
        return _fail(
            f"session {session!r} is reserved for internal platform use and "
            "cannot be accessed. Use an ordinary session name (e.g. 'main')."
        )
    return None


class ShellExecArgs(BaseModel):
    session: str = Field(default="main", description="Name of the shell session.")
    exec_dir: str = Field(default="", description="Directory to run the command in.")
    command: str = Field(..., description="The shell command to execute.")


class ShellExecTool:
    definition = ToolDef(
        name="shell_exec",
        description=(
            "Session-based: use ONLY when you need a process that outlives the call "
            "(watchers, daemons) or stdin interaction — otherwise use `shell`. "
            "Execute a shell command in a persistent session. One foreground "
            "process per session — it FAILS with 'session busy' if a previous "
            "command is still running. So ALWAYS background a long-running process "
            "(a file watcher, a build daemon) by appending ` &` — e.g. "
            "`npm run watch &` — so the session stays free for follow-up commands. "
            "To SERVE a preview of your app, use the `preview_start` tool (the platform "
            "owns serving + the port + health checks) — do NOT run your own web server "
            "here (`python -m http.server`, `http-server`, …); it collides with the "
            "platform preview. Never start a non-exiting process in the foreground; it "
            "wedges the session."
        ),
        args_model=ShellExecArgs,
        needs=frozenset({Capability.SHELL}),
        runs_in="sandbox",
        read_only=False,
        behavior=declares(
            EffectCapability.OPAQUE_EXECUTE,
            EffectCapability.PROCESS_CONTROL,
            EffectCapability.PROCESS_OUTPUT_READ,
            EffectCapability.WORKSPACE_MUTATE,
            planner_safe=False,
        ),
        base_risk=SecurityRisk.MEDIUM,
    )

    async def run(self, args: ShellExecArgs, ctx: ToolContext) -> ToolOutcome:
        if not ctx.sessions:
            return _fail("Session manager not available.")
        if (reserved := _reject_reserved_session(args.session)) is not None:
            return reserved
        try:
            outcome = await ctx.sessions.exec(
                args.session, args.command, args.exec_dir if args.exec_dir else None
            )
            if outcome.running:
                header = f"session '{args.session}' — still running"
            else:
                header = f"session '{args.session}' — exit {outcome.exit_code}"

            output, output_sanitized = sanitize_execution_output(
                outcome.output, stream="session output"
            )
            content = f"{header}\n{output}"
            if outcome.note:
                content += f"\nNote: {outcome.note}"
            content, cap_meta = cap_shell_observation(content.strip())
            structured: dict[str, Any] = dict(cap_meta) if cap_meta is not None else {}
            structured["running"] = bool(outcome.running)
            if not outcome.running:
                structured["exit_code"] = outcome.exit_code
            if output_sanitized is not None:
                structured.update(
                    {
                        "binary_output_sanitized": True,
                        "sanitized_streams": {"session_output": output_sanitized},
                    }
                )
            return ToolOutcome(success=True, content=content, structured=structured)
        except SessionBusy as e:
            message, _ = sanitize_execution_output(str(e), stream="session error")
            return _fail(message)
        except Exception as e:
            message, _ = sanitize_execution_output(str(e), stream="session error")
            return _fail(f"Error: {message}")


class ShellViewArgs(BaseModel):
    session: str = Field(..., description="Name of the shell session to view.")


class ShellViewTool:
    definition = ToolDef(
        name="shell_view",
        description=(
            "View the recent output and status of a persistent shell session. Can view anytime."
        ),
        args_model=ShellViewArgs,
        needs=frozenset({Capability.SHELL}),
        runs_in="sandbox",
        read_only=True,
        behavior=declares(EffectCapability.PROCESS_OUTPUT_READ, planner_safe=True),
        base_risk=SecurityRisk.LOW,
    )

    async def run(self, args: ShellViewArgs, ctx: ToolContext) -> ToolOutcome:
        if not ctx.sessions:
            return _fail("Session manager not available.")
        if (reserved := _reject_reserved_session(args.session)) is not None:
            return reserved
        try:
            view = await ctx.sessions.view(args.session)
            state = "running" if view.running else "idle"
            output, output_sanitized = sanitize_execution_output(
                view.output, stream="session output"
            )
            content = f"session '{args.session}' — {state}\n{output}"
            structured = (
                {
                    "binary_output_sanitized": True,
                    "sanitized_streams": {"session_output": output_sanitized},
                }
                if output_sanitized is not None
                else None
            )
            return ToolOutcome(success=True, content=content.strip(), structured=structured)
        except Exception as e:
            message, _ = sanitize_execution_output(str(e), stream="session error")
            return _fail(f"Error: {message}")


class ShellWaitArgs(BaseModel):
    session: str = Field(..., description="Name of the shell session.")
    seconds: int = Field(default=30, description="Max seconds to wait.")


class ShellWaitTool:
    definition = ToolDef(
        name="shell_wait",
        description="Wait for a running command in a session to finish, up to 'seconds' max.",
        args_model=ShellWaitArgs,
        needs=frozenset({Capability.SHELL}),
        runs_in="sandbox",
        read_only=True,
        behavior=declares(EffectCapability.PROCESS_OUTPUT_READ, planner_safe=True),
        base_risk=SecurityRisk.LOW,
    )

    async def run(self, args: ShellWaitArgs, ctx: ToolContext) -> ToolOutcome:
        if not ctx.sessions:
            return _fail("Session manager not available.")
        if (reserved := _reject_reserved_session(args.session)) is not None:
            return reserved
        try:
            view = await ctx.sessions.wait(args.session, args.seconds)
            state = "running" if view.running else "idle"
            output, output_sanitized = sanitize_execution_output(
                view.output, stream="session output"
            )
            content = f"session '{args.session}' — {state}\n{output}"
            structured = (
                {
                    "binary_output_sanitized": True,
                    "sanitized_streams": {"session_output": output_sanitized},
                }
                if output_sanitized is not None
                else None
            )
            return ToolOutcome(success=True, content=content.strip(), structured=structured)
        except Exception as e:
            message, _ = sanitize_execution_output(str(e), stream="session error")
            return _fail(f"Error: {message}")


class ShellWriteArgs(BaseModel):
    session: str = Field(..., description="Name of the shell session.")
    input: str = Field(..., description="Text to write to the session.")
    press_enter: bool = Field(default=True, description="Whether to press Enter after writing.")


class ShellWriteTool:
    definition = ToolDef(
        name="shell_write_to_process",
        description="Write stdin to a running process in a session.",
        args_model=ShellWriteArgs,
        needs=frozenset({Capability.SHELL}),
        runs_in="sandbox",
        read_only=False,
        behavior=declares(
            EffectCapability.OPAQUE_EXECUTE,
            EffectCapability.PROCESS_CONTROL,
            EffectCapability.WORKSPACE_MUTATE,
            planner_safe=False,
        ),
        base_risk=SecurityRisk.MEDIUM,
    )

    async def run(self, args: ShellWriteArgs, ctx: ToolContext) -> ToolOutcome:
        if not ctx.sessions:
            return _fail("Session manager not available.")
        if (reserved := _reject_reserved_session(args.session)) is not None:
            return reserved
        try:
            await ctx.sessions.write(args.session, args.input, args.press_enter)
            return ToolOutcome(success=True, content=f"Wrote to session '{args.session}'.")
        except Exception as e:
            return _fail(f"Error: {e}")


class ShellKillArgs(BaseModel):
    session: str = Field(..., description="Name of the shell session.")


class ShellKillTool:
    definition = ToolDef(
        name="shell_kill_process",
        description=(
            "Kill the foreground process in a session using C-c, or recreate it "
            "if stuck. This is sanctioned."
        ),
        args_model=ShellKillArgs,
        needs=frozenset({Capability.SHELL}),
        runs_in="sandbox",
        read_only=False,
        behavior=declares(EffectCapability.PROCESS_CONTROL, planner_safe=False),
        base_risk=SecurityRisk.MEDIUM,
    )

    async def run(self, args: ShellKillArgs, ctx: ToolContext) -> ToolOutcome:
        if not ctx.sessions:
            return _fail("Session manager not available.")
        if (reserved := _reject_reserved_session(args.session)) is not None:
            return reserved
        try:
            res = await ctx.sessions.kill_foreground(args.session)
            return ToolOutcome(success=True, content=res)
        except Exception as e:
            return _fail(f"Error: {e}")
