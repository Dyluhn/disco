"""Persistent named shell sessions (tmux-backed) — BP-01."""

from __future__ import annotations

from disco.core import SecurityRisk
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..sandbox.shell_sessions import SessionBusy


def _fail(msg: str) -> ToolOutcome:
    # DEFECT-2: failure diagnostics must travel in error AND content so the
    # relay (executor → AgentErrorEvent) never collapses them to "tool failed".
    return ToolOutcome(success=False, content=msg, error=msg)


class ShellExecArgs(BaseModel):
    session: str = Field(default="main", description="Name of the shell session.")
    exec_dir: str = Field(default="", description="Directory to run the command in.")
    command: str = Field(..., description="The shell command to execute.")

class ShellExecTool:
    definition = ToolDef(
        name="shell_exec",
        description="Execute a shell command in a persistent session. One foreground process per session. Fails if busy.",
        args_model=ShellExecArgs,
        needs=frozenset({Capability.SHELL}),
        runs_in="sandbox",
        read_only=False,
        base_risk=SecurityRisk.MEDIUM,
    )

    async def run(self, args: ShellExecArgs, ctx: ToolContext) -> ToolOutcome:
        if not ctx.sessions:
            return _fail("Session manager not available.")
        try:
            outcome = await ctx.sessions.exec(args.session, args.command, args.exec_dir if args.exec_dir else None)
            if outcome.running:
                header = f"session '{args.session}' — still running"
            else:
                header = f"session '{args.session}' — exit {outcome.exit_code}"

            content = f"{header}\n{outcome.output}"
            if outcome.note:
                content += f"\nNote: {outcome.note}"

            return ToolOutcome(success=True, content=content.strip())
        except SessionBusy as e:
            return _fail(str(e))
        except Exception as e:
            return _fail(f"Error: {e}")


class ShellViewArgs(BaseModel):
    session: str = Field(..., description="Name of the shell session to view.")

class ShellViewTool:
    definition = ToolDef(
        name="shell_view",
        description="View the recent output and status of a persistent shell session. Can view anytime.",
        args_model=ShellViewArgs,
        needs=frozenset({Capability.SHELL}),
        runs_in="sandbox",
        read_only=True,
        base_risk=SecurityRisk.LOW,
    )

    async def run(self, args: ShellViewArgs, ctx: ToolContext) -> ToolOutcome:
        if not ctx.sessions:
            return _fail("Session manager not available.")
        try:
            view = await ctx.sessions.view(args.session)
            state = "running" if view.running else "idle"
            content = f"session '{args.session}' — {state}\n{view.output}"
            return ToolOutcome(success=True, content=content.strip())
        except Exception as e:
            return _fail(f"Error: {e}")


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
        base_risk=SecurityRisk.LOW,
    )

    async def run(self, args: ShellWaitArgs, ctx: ToolContext) -> ToolOutcome:
        if not ctx.sessions:
            return _fail("Session manager not available.")
        try:
            view = await ctx.sessions.wait(args.session, args.seconds)
            state = "running" if view.running else "idle"
            content = f"session '{args.session}' — {state}\n{view.output}"
            return ToolOutcome(success=True, content=content.strip())
        except Exception as e:
            return _fail(f"Error: {e}")


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
        base_risk=SecurityRisk.MEDIUM,
    )

    async def run(self, args: ShellWriteArgs, ctx: ToolContext) -> ToolOutcome:
        if not ctx.sessions:
            return _fail("Session manager not available.")
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
        description="Kill the foreground process in a session using C-c, or recreate it if stuck. This is sanctioned.",
        args_model=ShellKillArgs,
        needs=frozenset({Capability.SHELL}),
        runs_in="sandbox",
        read_only=False,
        base_risk=SecurityRisk.MEDIUM,
    )

    async def run(self, args: ShellKillArgs, ctx: ToolContext) -> ToolOutcome:
        if not ctx.sessions:
            return _fail("Session manager not available.")
        try:
            res = await ctx.sessions.kill_foreground(args.session)
            return ToolOutcome(success=True, content=res)
        except Exception as e:
            return _fail(f"Error: {e}")
