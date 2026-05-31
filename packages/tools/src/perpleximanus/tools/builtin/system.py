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

from ..anatomy import ToolContext, ToolDef, ToolOutcome


class ShellArgs(BaseModel):
    command: str = Field(description="Shell command to run in the sandbox.")


class ShellTool:
    definition = ToolDef(
        name="shell",
        description="Run a shell command inside the sandbox and return its output.",
        args_model=ShellArgs,
        base_risk=SecurityRisk.MEDIUM,  # inherently riskier than read-only tools
        runs_in="sandbox",
    )

    async def run(self, args: ShellArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None  # sandbox tools always receive an instance
        res = await ctx.sandbox.exec_shell(args.command, timeout_s=ctx.timeout_s)
        ok = res.exit_code == 0
        body = res.stdout if ok else f"{res.stdout}\n{res.stderr}".strip()
        return ToolOutcome(
            success=ok,
            content=body,
            structured={"exit_code": res.exit_code, "stdout": res.stdout, "stderr": res.stderr},
            error=None if ok else f"command exited {res.exit_code}",
        )


class CodeExecArgs(BaseModel):
    language: Literal["python", "node"] = Field(description="Interpreter to use.")
    code: str = Field(description="Source code to execute.")


class CodeExecTool:
    definition = ToolDef(
        name="code_exec",
        description="Execute a Python or Node snippet in the sandbox (CodeAct).",
        args_model=CodeExecArgs,
        base_risk=SecurityRisk.MEDIUM,
        runs_in="sandbox",
    )

    async def run(self, args: CodeExecArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        ext, interp = ("py", "python3") if args.language == "python" else ("js", "node")
        fname = f"_codeact.{ext}"
        await ctx.sandbox.write_file(fname, args.code.encode("utf-8"))
        res = await ctx.sandbox.exec_shell(f"{interp} {fname}", timeout_s=ctx.timeout_s)
        ok = res.exit_code == 0
        body = res.stdout if ok else f"{res.stdout}\n{res.stderr}".strip()
        return ToolOutcome(
            success=ok,
            content=body,
            structured={"exit_code": res.exit_code, "stdout": res.stdout, "stderr": res.stderr},
            error=None if ok else f"{interp} exited {res.exit_code}",
        )
