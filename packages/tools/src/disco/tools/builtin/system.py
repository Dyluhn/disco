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
from ._shell_caps import cap_shell_observation, sanitize_execution_output

# The executor enforces a HARD ceiling (`wait_for(ctx.timeout_s)`). A graceful tool
# gives its in-container `timeout` a little less, so that fires FIRST and we return a
# clean `timed_out` outcome (partial output preserved) instead of being preempted into
# the executor's bare `timeout` failure. The executor's ceiling stays a pure backstop.
_GRACE_S = 5


def _inner_timeout(ceiling_s: int) -> int:
    return max(1, ceiling_s - _GRACE_S)


async def _spill_full_output(ctx, body: str) -> str | None:
    """Best-effort save of over-cap shell output to a workspace file so evidence
    survives the observation cap (a rerun is not always reproducible). Returns
    the workspace-relative path, or None when saving isn't possible."""
    if ctx is None or ctx.sandbox is None:
        return None
    import uuid

    path = f".disco-spill-{uuid.uuid4().hex[:8]}.log"
    try:
        await ctx.sandbox.write_file(path, body.encode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — spill is best-effort; fall back to the rerun hint
        return None
    return path


async def _exec_outcome(res: ExecResult, *, what: str, timeout_s: int, ctx=None) -> ToolOutcome:
    """Map an ExecResult to a ToolOutcome. A timeout is a DISTINCT, legible signal
    (not just a nonzero exit): the partial output is preserved and `timed_out` is
    surfaced in `structured`, so the loop can tell 'killed for running too long' from
    'the command failed'."""
    # H205: sanitize each raw decoded stream BEFORE constructing content,
    # structured values, or a spill file.  Once a stream proves binary-like, no
    # excerpt of it is safe to persist; the bounded replacement carries only
    # counts and an actionable redirect-to-artifact hint.
    stdout, stdout_sanitized = sanitize_execution_output(res.stdout, stream="stdout")
    stderr, stderr_sanitized = sanitize_execution_output(res.stderr, stream="stderr")
    sanitized_streams = {
        name: metadata
        for name, metadata in (("stdout", stdout_sanitized), ("stderr", stderr_sanitized))
        if metadata is not None
    }

    ok = res.exit_code == 0 and not res.timed_out
    body = stdout if (ok or not stderr) else f"{stdout}\n{stderr}".strip()
    if res.timed_out:
        error: str | None = f"{what} timed out after {timeout_s}s (partial output preserved)"
    elif not ok:
        # Bug 16 — surface a containment REFUSAL (ExecResult(126, stderr="refused: ...")
        # — e.g. a reserved-port bind/kill) as the error TEXT, not a bare "exited 126".
        # The loop emits AgentErrorEvent(error=result.error) and DROPS content, so
        # without this the model never sees the actionable "serve on a non-reserved port
        # such as N" guidance and re-tries the same reserved port → STUCK. Other nonzero
        # exits keep the concise summary.
        refusal_stderr = stderr.strip()
        if res.exit_code == 126 and refusal_stderr.startswith("refused:"):
            error = refusal_stderr
        else:
            error = f"{what} exited {res.exit_code}"
    else:
        error = None
    spill_path = None
    if len(body) > 4_000:  # matches the cap threshold in _shell_caps
        spill_path = await _spill_full_output(ctx, body)
    content, cap_meta = cap_shell_observation(body, spill_path=spill_path)
    structured = {
        "exit_code": res.exit_code,
        "timed_out": res.timed_out,
    }
    if cap_meta is None:
        structured.update(
            {
                "stdout": stdout,
                "stderr": stderr,
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
    if sanitized_streams:
        structured.update(
            {
                "binary_output_sanitized": True,
                "sanitized_streams": sanitized_streams,
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
        return await _exec_outcome(res, what="command", timeout_s=inner, ctx=ctx)


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
            stdout, stdout_sanitized = sanitize_execution_output(res.stdout, stream="stdout")
            stderr, stderr_sanitized = sanitize_execution_output(res.stderr, stream="stderr")
            result_repr, result_sanitized = sanitize_execution_output(
                res.result_repr or "", stream="result"
            )
            traceback, traceback_sanitized = sanitize_execution_output(
                res.error_traceback or "", stream="traceback"
            )
            content_parts = [part for part in (stdout, stderr) if part]
            if result_repr:
                content_parts.append(f"→ {result_repr}")
            if traceback:
                content_parts.append(traceback)
            content_parts.extend(f"plot saved: {image}" for image in res.images)
            structured = {
                "ok": res.ok,
                "stdout": stdout,
                "stderr": stderr,
                "result_repr": result_repr or None,
                "error_traceback": traceback or None,
                "images": list(res.images),
                "timed_out": res.timed_out,
                "restarted": res.restarted,
                "interrupt_attempted": res.interrupt_attempted,
                "interrupt_failed": res.interrupt_failed,
                "restart_attempted": res.restart_attempted,
                "restart_failed": res.restart_failed,
                "protocol_failed": res.protocol_failed,
            }
            sanitized_streams = {
                name: metadata
                for name, metadata in (
                    ("stdout", stdout_sanitized),
                    ("stderr", stderr_sanitized),
                    ("result", result_sanitized),
                    ("traceback", traceback_sanitized),
                )
                if metadata is not None
            }
            if sanitized_streams:
                structured.update(
                    {
                        "binary_output_sanitized": True,
                        "sanitized_streams": sanitized_streams,
                    }
                )
            return ToolOutcome(
                success=res.ok,
                content="\n".join(content_parts),
                structured=structured,
                artifacts=res.images,
            )

        # Node: one-shot (no cross-cell state — documented).
        fname = "_codeact.js"
        await ctx.sandbox.write_file(fname, args.code.encode("utf-8"))
        res = await ctx.sandbox.exec_shell(f"node {fname}", timeout_s=inner)
        return await _exec_outcome(res, what="node", timeout_s=inner, ctx=ctx)
