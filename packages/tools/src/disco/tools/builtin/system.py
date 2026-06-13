"""Shell + code execution — tool-sandbox-contract.md §9.

Both run inside the sandbox instance. `shell` surfaces the raw command (it lives
in `ShellArgs.command` → the ActionEvent → the SecurityAnalyzer can score it,
§6.1). `code_exec` is the CodeAct path (BoD §10.2): write the snippet to the
workspace and run it in the instance.
"""

from __future__ import annotations

import os
import uuid
from typing import Literal

from disco.core import SecurityRisk
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..sandbox.base import ExecResult

# The executor enforces a HARD ceiling (`wait_for(ctx.timeout_s)`). A graceful tool
# gives its in-container `timeout` a little less, so that fires FIRST and we return a
# clean `timed_out` outcome (partial output preserved) instead of being preempted into
# the executor's bare `timeout` failure. The executor's ceiling stays a pure backstop.
_GRACE_S = 5

# HS-01: when a weak-model driver is being helped (`ctx.assist`), redirect a
# runaway stdout to a workspace file so the loop can still see *head*+*tail* of
# what ran (and grep the rest later) instead of getting a single wall of text
# that overflows the model's context. Capable-model path is unaffected because
# the gate is `ctx.assist` (capable models don't trigger the spill).
_SPILL_HEAD_BYTES = 2048  # leading 2KB
_SPILL_TAIL_BYTES = 2048  # trailing 2KB (often the error / final state)
_SPILL_FILENAME = ".disco-spill-{uuid}.log"  # T8 also skips these in the snapshot


def _spill_threshold_bytes() -> int:
    """HS-01 threshold in bytes. Default 50KB, overridable via
    `DISCO_SHELL_SPILL_KB`. Read at call-time so tests can monkeypatch the env
    var per-case without process-level state."""
    try:
        kb = int(os.environ.get("DISCO_SHELL_SPILL_KB", "50"))
    except (TypeError, ValueError):
        kb = 50
    if kb <= 0:
        kb = 50
    return kb * 1024


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
        description=(
            "Run a shell command inside the sandbox (installs, builds, tests, git) "
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
        outcome = _exec_outcome(res, what="command", timeout_s=inner)
        # HS-01: spill huge stdout to a workspace file, GATED by ctx.assist
        # (added by T1). When the gate is OFF (capable-model path) this is a
        # no-op and the outcome is byte-identical to what `_exec_outcome`
        # produced — the only difference is the extra `await write_file` and
        # the conditional that follows it, both of which short-circuit.
        if ctx.assist and len(res.stdout) > _spill_threshold_bytes():
            spill_name = _SPILL_FILENAME.format(uuid=uuid.uuid4().hex)
            spill_path = os.path.join(ctx.workspace_path or "", spill_name)
            await ctx.sandbox.write_file(spill_path, res.stdout.encode("utf-8"))
            head = res.stdout[:_SPILL_HEAD_BYTES]
            tail = res.stdout[-_SPILL_TAIL_BYTES:]
            marker = (
                f"\n\u2026[truncated; full output at {spill_path} \u2014 "
                "file_read or grep it]\n"
            )
            new_content = head + marker + tail
            # Preserve all structured fields; add a machine-readable spill path.
            new_structured = dict(outcome.structured) if outcome.structured else {}
            new_structured["spill_path"] = spill_path
            outcome = outcome.model_copy(
                update={"content": new_content, "structured": new_structured}
            )
        return outcome


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
