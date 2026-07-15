"""Preview tools — the model declares INTENT; the platform owns the port/serving/health.

EPIC F. These four tools (`preview_start`, `preview_status`, `preview_logs`,
`preview_stop`) are the model-facing surface of the `PreviewManager`. The defining
property — the whole point of the epic — is what `preview_start` does NOT have: a
port argument. The model says WHAT to serve (a build-output dir, a framework, or a
bare start command) and the platform decides WHERE (the port), starts + supervises
the process, polls health, and hands back the platform-assigned URL. A model can
neither pick nor override a port through these tools.

The `PreviewManager` lives in the agent-server layer (it's a runtime collaborator,
bound to the conversation's sandbox). To respect the package layering (tools must not
statically depend on agent_server), the tool reaches the manager off the live sandbox
session — the runtime attaches it there — and lazily constructs one on first use via a
function-local import (the same dodge `audio_overview` uses for its agent_server reach).
"""

from __future__ import annotations

from typing import Any

from disco.core import SecurityRisk
from pydantic import BaseModel, ConfigDict, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome


def _manager(ctx: ToolContext) -> Any:
    """The conversation's PreviewManager, bound to the live sandbox session.

    Cached on the sandbox session (which is task-scoped and survives across tool
    calls) so supervision + the preview registry persist between `preview_*` calls.
    Lazily constructed on first use — a function-local import keeps the tools layer
    free of a static agent_server dependency (mirrors `audio_overview`)."""
    sandbox: Any = ctx.sandbox
    mgr = getattr(sandbox, "_preview_manager", None)
    if mgr is None:
        from disco.agent_server.preview_manager import PreviewManager

        mgr = PreviewManager(sandbox)
        try:
            sandbox._preview_manager = mgr  # noqa: SLF001 — caching on the session we own
        except Exception:  # noqa: BLE001 — non-settable fake in a test → use the fresh one
            pass
    return mgr


def _render(session: Any) -> str:
    url = session.url or "(no URL — see status)"
    running = getattr(session.status, "value", "") == "running"
    # BAKE-OFF #7: `url` is the HOST-published browser URL — NOT reachable from inside the
    # sandbox. A model that curls it from its shell gets connection-refused (000) and wrongly
    # concludes the build is broken. The IN-SANDBOX url is on the PLATFORM-assigned port
    # (NEVER a fixed :8000 — the platform chooses it; see PreviewManager), so ALWAYS surface
    # it: it is the one URL the model can curl from its shell. Showing it unconditionally —
    # even before the preview is health-verified — stops the model falling back to guessing
    # :8000 (which serves nothing) while the server is still coming up.
    insandbox = f"http://localhost:{session.port}/"
    if running:
        # `running` means the platform already health-probed it (HTTP 200, in-sandbox) — so
        # say it's verified and hand over the in-sandbox URL for any further check.
        verify = (
            f"\n  status: ALREADY platform-health-verified (HTTP 200, probed in-sandbox) — it IS "
            f"serving; you do NOT need to curl it.\n"
            f"  in-sandbox url (curl THIS from your shell, NOT the browser url): {insandbox}"
        )
    else:
        # Not yet health-verified — but still give the correct in-sandbox URL so the model
        # checks the RIGHT port once it's up, never a guessed :8000.
        verify = (
            f"\n  in-sandbox url (once it's up, curl THIS from your shell, NOT the browser url): "
            f"{insandbox}"
        )
    return (
        f"preview '{session.name}': {session.status.value}\n"
        f"  browser url: {url}  (for the USER's browser — NOT reachable from inside the sandbox)\n"
        f"  port:   {session.port}  (platform-assigned)\n"
        f"  detail: {session.detail or 'ok'}"
        f"{verify}"
    )


# --------------------------------------------------------------------------- start


class PreviewStartArgs(BaseModel):
    """Intent for a preview — note there is NO `port` field, by design. The platform
    owns port allocation; a model declares only WHAT to serve."""

    # P2 #5: reject unknown keys (e.g. a model-invented `port`) instead of silently
    # dropping them — a swallowed `port=3000` looked accepted but did nothing.
    model_config = ConfigDict(extra="forbid")

    serve_dir: str | None = Field(
        default=None,
        description=(
            "Directory to serve statically (e.g. the build output dir like 'dist' or "
            "'build'). The platform serves it on a port IT chooses."
        ),
    )
    command: str | None = Field(
        default=None,
        description=(
            "A start command for a dev server (e.g. 'npm run dev'). Do NOT include a "
            "port — the platform injects its chosen port (any port you put here is "
            "ignored). Use this when serve_dir/framework don't fit."
        ),
    )
    framework: str | None = Field(
        default=None,
        description=(
            "Framework hint so the platform picks the right start command "
            "(static, vite, next, react/cra, astro, svelte, node/express)."
        ),
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory to launch the command in (defaults to workspace).",
    )
    name: str | None = Field(
        default=None,
        description="Optional name to address this preview in status/logs/stop.",
    )


class PreviewStartTool:
    definition = ToolDef(
        name="preview_start",
        description=(
            "Start a live preview of your build. You declare WHAT to serve (a directory, "
            "a framework, or a start command) — the PLATFORM picks the port, starts and "
            "supervises the server (restarting it if it crashes), and returns the URL. "
            "You never choose or pass a port. Call again to (idempotently) get the same "
            "preview's URL."
        ),
        args_model=PreviewStartArgs,
        needs=frozenset({Capability.SHELL, Capability.NETWORK}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=False,
    )

    async def run(self, args: PreviewStartArgs, ctx: ToolContext) -> ToolOutcome:
        if ctx.sandbox is None:
            return ToolOutcome(
                success=False,
                content="No sandbox available for a preview.",
                error="no_sandbox",
            )
        if not (args.serve_dir or args.command or args.framework):
            return ToolOutcome(
                success=False,
                content=(
                    "Specify what to serve: a serve_dir (e.g. 'dist'), a framework "
                    "(e.g. 'vite'), or a start command. (You never specify a port.)"
                ),
                error="no_intent",
            )
        mgr = _manager(ctx)
        from disco.agent_server.preview_manager import PreviewCommandError

        try:
            session = await mgr.start(
                serve_dir=args.serve_dir,
                command=args.command,
                framework=args.framework,
                cwd=args.cwd,
                name=args.name,
            )
        except PreviewCommandError as exc:
            # P1 #1: a raw command that binds a hardcoded port the platform can't own is
            # rejected with actionable guidance, never silently run on a model-chosen port.
            return ToolOutcome(success=False, content=str(exc), error="invalid_command")
        return ToolOutcome(
            success=session.status.value not in ("crashed",),
            content=_render(session),
            structured=session.to_dict(),
        )


# -------------------------------------------------------------------------- status


class PreviewStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")  # P2 #5

    name: str | None = Field(
        default=None, description="A specific preview's name; omit for all previews."
    )


class PreviewStatusTool:
    definition = ToolDef(
        name="preview_status",
        description=(
            "Show the status, health, and platform URL of running previews (all, or one by name)."
        ),
        args_model=PreviewStatusArgs,
        needs=frozenset({Capability.SHELL}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=True,
    )

    async def run(self, args: PreviewStatusArgs, ctx: ToolContext) -> ToolOutcome:
        if ctx.sandbox is None:
            return ToolOutcome(success=True, content="No previews (no sandbox).")
        mgr = _manager(ctx)
        sessions = await mgr.status(args.name)
        if not sessions:
            return ToolOutcome(success=True, content="No previews running.")
        return ToolOutcome(
            success=True,
            content="\n\n".join(_render(s) for s in sessions),
            structured={"previews": [s.to_dict() for s in sessions]},
        )


# ---------------------------------------------------------------------------- logs


class PreviewLogsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")  # P2 #5

    name: str | None = Field(
        default=None, description="A specific preview's name; omit for all previews."
    )
    tail_chars: int = Field(
        default=4000, ge=200, le=40000, description="How many trailing characters of log to show."
    )


class PreviewLogsTool:
    definition = ToolDef(
        name="preview_logs",
        description=(
            "Show recent server output (stdout/stderr) for a preview — useful when it won't start."
        ),
        args_model=PreviewLogsArgs,
        needs=frozenset({Capability.SHELL}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=True,
    )

    async def run(self, args: PreviewLogsArgs, ctx: ToolContext) -> ToolOutcome:
        if ctx.sandbox is None:
            return ToolOutcome(success=True, content="No previews (no sandbox).")
        mgr = _manager(ctx)
        logs = await mgr.logs(args.name, tail_chars=args.tail_chars)
        if not logs:
            return ToolOutcome(success=True, content="No previews to show logs for.")
        blocks = [f"=== {name} ===\n{out}" for name, out in logs.items()]
        return ToolOutcome(success=True, content="\n\n".join(blocks), structured={"logs": logs})


# ---------------------------------------------------------------------------- stop


class PreviewStopArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")  # P2 #5

    name: str | None = Field(
        default=None, description="A specific preview's name; omit to stop all previews."
    )


class PreviewStopTool:
    definition = ToolDef(
        name="preview_stop",
        description="Stop a running preview (or all of them).",
        args_model=PreviewStopArgs,
        needs=frozenset({Capability.SHELL}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=False,
    )

    async def run(self, args: PreviewStopArgs, ctx: ToolContext) -> ToolOutcome:
        if ctx.sandbox is None:
            return ToolOutcome(success=True, content="No previews to stop (no sandbox).")
        mgr = _manager(ctx)
        stopped = await mgr.stop(args.name)
        if not stopped:
            return ToolOutcome(success=True, content="No matching preview to stop.")
        return ToolOutcome(
            success=True,
            content="Stopped preview(s): " + ", ".join(stopped),
            structured={"stopped": stopped},
        )
