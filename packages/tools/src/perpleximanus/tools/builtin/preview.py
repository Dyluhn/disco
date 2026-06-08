"""Preview lifecycle tools — the safe, bounded way to observe and recover the live
preview server, instead of the raw `pkill`+`&` pattern that broke it (the "stuck
build" wall). See universal-readiness-plan §E.

The workspace is auto-served on PREVIEW_PORT (8000) by the container's supervised
keepalive; a static site needs no server. These tools give the agent (and, via the
UI button, the user) read-only health context and a bounded restart — without the
ability to kill the user's preview window (that raw pattern is hard-denied at the
gate, §E6).

  - `preview_status`  — READ-ONLY. Is something serving on :8000? Are there files?
  - `restart_preview` — CONTROLLED. Re-establish the static serve idempotently
                        (kill any stale server; the supervisor respawns, or we start
                        one detached if there's no supervisor). Never leaves it down.
  - `run_server`      — start a real dev server detached on :8000 (survives across
                        tool calls), for non-static builds. Bind 0.0.0.0 so the
                        preview proxy can reach it. (Named `run_server`, not `serve`
                        — `serve` is the deliverable-handoff tool.)
"""

from __future__ import annotations

from perpleximanus.core import SecurityRisk
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..sandbox._container import PREVIEW_PORT

# A dependency-free port probe (python3 is always in the sandbox image): exit 0 if
# something is listening on PREVIEW_PORT, else 1. Used by status + restart.
_LISTEN_PROBE = (
    "python3 -c \"import socket,sys; "
    f"sys.exit(0 if socket.socket().connect_ex(('127.0.0.1',{PREVIEW_PORT}))==0 else 1)\""
)


class _NoArgs(BaseModel):
    pass


class PreviewStatusTool:
    """READ-ONLY health of the preview server — the context the agent was missing
    (it used to guess and `pkill`). Pure observation: no side effects."""

    definition = ToolDef(
        name="preview_status",
        description=(
            "Check the live preview on port 8000 (read-only): whether a server is "
            "responding and whether the workspace has files to serve. Use this to "
            "diagnose the preview instead of killing/restarting servers blindly."
        ),
        args_model=_NoArgs,
        needs=frozenset({Capability.SHELL}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=True,
    )

    async def run(self, args: _NoArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        # one probe: listening? + file inventory (index.html is what a static serve needs)
        script = (
            "L=down; " + _LISTEN_PROBE + " && L=serving; "
            "I=no; [ -f index.html ] && I=yes; "
            "N=$(ls -1 2>/dev/null | wc -l | tr -d ' '); "
            'echo "listening=$L index=$I files=$N"'
        )
        res = await ctx.sandbox.exec_shell(script, timeout_s=min(ctx.timeout_s, 15))
        out = res.stdout.strip()
        serving = "listening=serving" in out
        has_index = "index=yes" in out
        no_files = "files=0" in out
        if serving:
            state, msg = "serving", f"Preview is serving on port {PREVIEW_PORT}."
        elif no_files:
            state, msg = "no-files", "No files in the workspace yet — nothing to preview."
        else:
            state = "down"
            msg = (
                f"No server is responding on port {PREVIEW_PORT}. "
                + (
                    "There is an index.html — call restart_preview to serve it."
                    if has_index
                    else "Write an index.html (auto-served) or start a dev server with `serve`."
                )
            )
        return ToolOutcome(
            success=True,
            content=msg,
            structured={"state": state, "has_index": has_index, "raw": out},
        )


class RestartPreviewTool:
    """CONTROLLED, idempotent restart of the static preview serve. Replaces the raw
    `pkill http.server` + `cmd &` that the agent used (and that the gate now hard-
    denies). Kills any stale server, lets the keepalive supervisor respawn it, and
    starts a fresh detached server if nothing is supervising — so it never leaves
    the preview down."""

    definition = ToolDef(
        name="restart_preview",
        description=(
            "Safely restart the static preview server on port 8000 (idempotent). Use "
            "when preview_status reports 'down' but files exist. Do NOT use shell "
            "pkill/kill on the preview server — that breaks the user's window."
        ),
        args_model=_NoArgs,
        needs=frozenset({Capability.SHELL}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
    )

    async def run(self, args: _NoArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        ws = ctx.workspace_path
        # 1) clear any stale server; 2) give the supervisor a moment to respawn;
        # 3) if still nothing is listening, start one DETACHED (survives this exec
        #    via setsid+nohup). A double-start is harmless: the loser fails to bind.
        script = (
            "pkill -f 'http.server' 2>/dev/null || true; sleep 1; "
            f"if ! {_LISTEN_PROBE}; then "
            f"cd {ws} && setsid nohup python3 -m http.server {PREVIEW_PORT} "
            ">/tmp/pmx-preview.log 2>&1 & fi; sleep 1; "
            f"if {_LISTEN_PROBE}; then echo ok; else echo failed; fi"
        )
        res = await ctx.sandbox.exec_shell(script, timeout_s=min(ctx.timeout_s, 20))
        ok = res.stdout.strip().endswith("ok")
        return ToolOutcome(
            success=ok,
            content=(
                f"Preview restarted — serving on port {PREVIEW_PORT}."
                if ok
                else "Could not bring the preview server up (check that files exist)."
            ),
            structured={"serving": ok},
            error=None if ok else "preview restart did not establish a listener",
        )


class ServeArgs(BaseModel):
    command: str = Field(
        description=(
            "The dev-server command to run, e.g. 'npm run dev' or 'python app.py'. It "
            f"MUST bind 0.0.0.0:{PREVIEW_PORT} so the preview proxy can reach it."
        )
    )


class RunServerTool:
    """Start a real dev server DETACHED on PREVIEW_PORT so it survives across tool
    calls (a plain `cmd &` in `shell` is reaped when the exec returns). For non-
    static builds (Vite/Next/Flask/etc.). Static sites need no server — they're
    auto-served, so prefer just writing files. Named `run_server` to avoid colliding
    with the `serve` deliverable-handoff tool."""

    definition = ToolDef(
        name="run_server",
        description=(
            "Start a long-running dev server in the background on port 8000 (survives "
            "across steps). Bind 0.0.0.0:8000. For STATIC sites do NOT use this — just "
            "write files to the workspace; they are auto-served. Returns once the "
            "server is up (or reports if it didn't bind)."
        ),
        args_model=ServeArgs,
        needs=frozenset({Capability.SHELL, Capability.NETWORK}),
        base_risk=SecurityRisk.MEDIUM,
        runs_in="sandbox",
    )

    async def run(self, args: ServeArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        ws = ctx.workspace_path
        cmd = args.command.replace("'", "'\\''")  # safe single-quote embedding
        # free the port, then start the server fully detached (setsid + nohup) so it
        # outlives this exec; poll briefly for it to bind.
        script = (
            "pkill -f 'http.server' 2>/dev/null || true; "
            f"cd {ws} && setsid nohup sh -c '{cmd}' >/tmp/pmx-serve.log 2>&1 & "
            f"for i in 1 2 3 4 5 6 7 8; do sleep 1; "
            f"if {_LISTEN_PROBE}; then echo up; break; fi; done; "
            f"if {_LISTEN_PROBE}; then echo up; else echo notup; tail -n 20 /tmp/pmx-serve.log; fi"
        )
        res = await ctx.sandbox.exec_shell(script, timeout_s=min(ctx.timeout_s, 30))
        # the script prints a line "up" once the port binds; "notup" + a log tail else
        up = "up" in res.stdout.splitlines()
        return ToolOutcome(
            success=up,
            content=(
                f"Dev server is up on port {PREVIEW_PORT} (detached; survives across steps)."
                if up
                else f"Server did not bind port {PREVIEW_PORT}.\n{res.stdout.strip()}"
            ),
            structured={"serving": up, "log_tail": res.stdout.strip()},
            error=None if up else "dev server did not bind the preview port",
        )
