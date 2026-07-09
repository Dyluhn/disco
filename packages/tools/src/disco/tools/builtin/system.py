"""Shell + code execution — tool-sandbox-contract.md §9.

Both run inside the sandbox instance. `shell` surfaces the raw command (it lives
in `ShellArgs.command` → the ActionEvent → the SecurityAnalyzer can score it,
§6.1). `code_exec` is the CodeAct path (BoD §10.2): write the snippet to the
workspace and run it in the instance.
"""

from __future__ import annotations

from typing import Literal

from disco.core import SecurityRisk
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..sandbox.base import ExecResult
from ._shell_caps import cap_shell_observation

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
        # Bug 16 — surface a containment REFUSAL (ExecResult(126, stderr="refused: ...")
        # — e.g. a reserved-port bind/kill) as the error TEXT, not a bare "exited 126".
        # The loop emits AgentErrorEvent(error=result.error) and DROPS content, so
        # without this the model never sees the actionable "serve on a non-reserved port
        # such as N" guidance and re-tries the same reserved port → STUCK. Other nonzero
        # exits keep the concise summary.
        stderr = (res.stderr or "").strip()
        if res.exit_code == 126 and stderr.startswith("refused:"):
            error = stderr
        else:
            error = f"{what} exited {res.exit_code}"
    else:
        error = None
    content, cap_meta = cap_shell_observation(body)
    structured = {
        "exit_code": res.exit_code,
        "timed_out": res.timed_out,
    }
    if cap_meta is None:
        structured.update(
            {
                "stdout": res.stdout,
                "stderr": res.stderr,
                "output_truncated": False,
            }
        )
    else:
        structured.update(
            {
                "stdout_chars": len(res.stdout),
                "stderr_chars": len(res.stderr),
                **cap_meta,
            }
        )
    return ToolOutcome(
        success=ok,
        content=content,
        structured=structured,
        error=error,
    )


class ShellArgs(BaseModel):
    command: str = Field(description="Shell command to run in the sandbox.")


class ShellTool:
    definition = ToolDef(
        name="shell",
        description=(
            "One-shot: runs to completion and returns output — the default for "
            "installs, builds, tests, git. Run a shell command inside the sandbox "
            "and return its output. NOT for creating or editing files — use "
            "file_write / file_edit / file_append for that, never `>`, `>>`, `sed`, "
            "`tee`, or a here-doc."
        ),
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
        description=(
            "Execute a Python or Node snippet in the sandbox (CodeAct). Python cells "
            "run in a persistent IPython kernel: variables, imports, sockets, and "
            "open files persist across calls. A timeout interrupts the cell but "
            "keeps state."
        ),
        args_model=CodeExecArgs,
        needs=frozenset({Capability.CODE_EXEC}),
        base_risk=SecurityRisk.MEDIUM,
        runs_in="sandbox",
    )

    async def run(self, args: CodeExecArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        inner = _inner_timeout(ctx.timeout_s)
        if args.language == "python":
            # Persistent IPython kernel (BP-08)
            assert ctx.kernel is not None
            # Cap the kernel timeout to slightly less than the tool timeout
            # (floored: a tiny tool timeout must not go zero/negative on the kernel)
            kernel_timeout = max(5, min(ctx.timeout_s - 5, 120))
            res = await ctx.kernel.execute(args.code, timeout_s=kernel_timeout)
            
            return ToolOutcome(
                success=res.ok,
                content=str(res),
                structured=res.__dict__,
                artifacts=res.images
            )

        # Node: one-shot (no cross-cell state — documented).
        fname = "_codeact.js"
        await ctx.sandbox.write_file(fname, args.code.encode("utf-8"))
        res = await ctx.sandbox.exec_shell(f"node {fname}", timeout_s=inner)
        return _exec_outcome(res, what="node", timeout_s=inner)
