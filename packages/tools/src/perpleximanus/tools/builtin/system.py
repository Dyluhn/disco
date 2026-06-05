"""Shell + code execution — tool-sandbox-contract.md §9.

Both run inside the sandbox instance. `shell` surfaces the raw command (it lives
in `ShellArgs.command` → the ActionEvent → the SecurityAnalyzer can score it,
§6.1). `code_exec` is the CodeAct path (BoD §10.2): write the snippet to the
workspace and run it in the instance.
"""

from __future__ import annotations

from typing import Literal

from perpleximanus.core import SecurityRisk
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..sandbox.base import ExecResult

# The executor enforces a HARD ceiling (`wait_for(ctx.timeout_s)`). A graceful tool
# gives its in-container `timeout` a little less, so that fires FIRST and we return a
# clean `timed_out` outcome (partial output preserved) instead of being preempted into
# the executor's bare `timeout` failure. The executor's ceiling stays a pure backstop.
_GRACE_S = 5


def _inner_timeout(ceiling_s: int) -> int:
    return max(1, ceiling_s - _GRACE_S)


def _exec_outcome(res: ExecResult, *, what: str, timeout_s: int) -> ToolOutcome:
    """Map an ExecResult to a ToolOutcome. A timeout is a DISTINCT, legible signal
    (not just a nonzero exit): the partial output is preserved and `timed_out` is
    surfaced in `structured`, so the loop can tell 'killed for running too long' from
    'the command failed'."""
    ok = res.exit_code == 0 and not res.timed_out
    body = res.stdout if (ok or not res.stderr) else f"{res.stdout}\n{res.stderr}".strip()
    if res.timed_out:
        error: str | None = f"{what} timed out after {timeout_s}s (partial output preserved)"
    elif not ok:
        error = f"{what} exited {res.exit_code}"
    else:
        error = None
    return ToolOutcome(
        success=ok,
        content=body,
        structured={
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "timed_out": res.timed_out,
        },
        error=error,
    )


class ShellArgs(BaseModel):
    command: str = Field(description="Shell command to run in the sandbox.")


class ShellTool:
    definition = ToolDef(
        name="shell",
        description="Run a shell command inside the sandbox and return its output.",
        args_model=ShellArgs,
        needs=frozenset({Capability.SHELL}),
        base_risk=SecurityRisk.MEDIUM,  # inherently riskier than read-only tools
        runs_in="sandbox",
    )

    async def run(self, args: ShellArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None  # sandbox tools always receive an instance
        inner = _inner_timeout(ctx.timeout_s)
        res = await ctx.sandbox.exec_shell(args.command, timeout_s=inner)
        return _exec_outcome(res, what="command", timeout_s=inner)


class CodeExecArgs(BaseModel):
    language: Literal["python", "node"] = Field(description="Interpreter to use.")
    code: str = Field(description="Source code to execute.")


class CodeExecTool:
    definition = ToolDef(
        name="code_exec",
        description="Execute a Python or Node snippet in the sandbox (CodeAct).",
        args_model=CodeExecArgs,
        needs=frozenset({Capability.CODE_EXEC}),
        base_risk=SecurityRisk.MEDIUM,
        runs_in="sandbox",
    )

    async def run(self, args: CodeExecArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        ext, interp = ("py", "python3") if args.language == "python" else ("js", "node")
        fname = f"_codeact.{ext}"
        await ctx.sandbox.write_file(fname, args.code.encode("utf-8"))
        inner = _inner_timeout(ctx.timeout_s)
        res = await ctx.sandbox.exec_shell(f"{interp} {fname}", timeout_s=inner)
        return _exec_outcome(res, what=interp, timeout_s=inner)
