"""`ToolOutcome` builders shared by `RunProjectScriptTool.run` and every
interior phase (`ops`, `prevalidate`, `commit`). Pure — no sandbox/ctx state.
"""

from __future__ import annotations

from typing import Any, Literal

from disco.core.effects import MutationReceipt, OpaqueEffectReceipt

from ...anatomy import ToolOutcome


def _fail(op_index: int, error: str, message: str, **extra: Any) -> ToolOutcome:
    return ToolOutcome(
        success=False,
        error=error,
        content=(
            f"run_project_script aborted at operation #{op_index} (nothing was written): {message}"
        ),
        structured={"kind": "run_script_aborted", "op_index": op_index, "error": error, **extra},
    )


def _success(
    content: str,
    applied: list[str],
    operations_run: int,
    reads: dict[str, Any],
    receipts: tuple[MutationReceipt, ...] = (),
) -> ToolOutcome:
    return ToolOutcome(
        success=True,
        content=content,
        artifacts=applied,
        structured={
            "ok": True,
            "applied": applied,
            "operations_run": operations_run,
            "reads": reads,
        },
        effect_receipts=receipts,
    )


def _commit_failure(
    *,
    applied: list[str],
    receipts: list[MutationReceipt | OpaqueEffectReceipt],
    failed_path: str,
    failed_path_state: Literal["unchanged", "committed", "unknown"],
    reason: str,
    operations_run: int,
    reads: dict[str, Any],
    underlying: dict[str, Any] | None = None,
) -> ToolOutcome:
    if failed_path_state == "unchanged":
        partial = bool(applied)
        error = "SCRIPT_PARTIAL_COMMIT" if partial else "SCRIPT_COMMIT_FAILED"
        kind = "run_script_partial_commit" if partial else "run_script_commit_failed"
        summary = (
            f"run_project_script stopped before committing {failed_path}; "
            f"{len(applied)} earlier file(s) remain committed"
            if partial
            else f"run_project_script could not commit {failed_path}; no file was committed"
        )
    elif failed_path_state == "committed":
        partial = True
        error = "SCRIPT_COMMIT_INTERRUPTED"
        kind = "run_script_commit_interrupted"
        summary = (
            f"the backend reported a failure for {failed_path}, but its exact intended bytes "
            f"were observed afterward; {len(applied)} file(s) are proven committed"
        )
    else:
        partial = True
        error = "SCRIPT_COMMIT_UNCERTAIN"
        kind = "run_script_commit_uncertain"
        summary = (
            f"the backend reported a failure for {failed_path} and its final state could not "
            f"be attributed; {len(applied)} other file(s) are proven committed"
        )
    structured: dict[str, Any] = {
        "kind": kind,
        "applied": applied,
        "failed_path": failed_path,
        "failed_path_state": failed_path_state,
        "operations_run": operations_run,
        "reads": reads,
    }
    if underlying is not None:
        structured["underlying"] = underlying
    return ToolOutcome(
        success=False,
        error=error,
        content=(
            f"{summary}. The result lists exact proven commits; do not assume any unlisted "
            f"path applied. Reason: {reason}"
        ),
        artifacts=applied,
        structured=structured,
        effect_receipts=tuple(receipts),
    )


def _exception_detail(exc: Exception) -> dict[str, str]:
    """Keep a bounded, single-line backend cause without repr/debug payloads."""
    message = " ".join(str(exc).split())[:500]
    return {"type": type(exc).__name__, "message": message or "no detail supplied"}
