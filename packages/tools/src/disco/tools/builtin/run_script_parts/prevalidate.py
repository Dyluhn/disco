"""`RunProjectScriptTool.run`'s pre-commit gates on the batch's buffered state.

Runs entirely in memory, after every operation has been applied to the
buffer: keep only byte-changing paths (a requested mutation is not an
effective mutation merely because it matched and entered the buffer — a new
empty file still changes filesystem state because its original is `None`),
cap total committed size, and refuse a batch that would introduce a syntax
error. Nothing is written to the sandbox until every gate here passes.
"""

from __future__ import annotations

from ...anatomy import ToolOutcome
from ..files import _syntax_errors
from .outcomes import _fail

_MAX_TOTAL_WRITE_BYTES = 2_000_000  # cap committed content (anti-runaway / context-poison)


def _prevalidate_effective_mutations(
    buffer: dict[str, str],
    original: dict[str, str | None],
    original_bytes: dict[str, bytes | None],
    mutated: set[str],
    last_op_index: int,
) -> tuple[dict[str, bytes], set[str]] | ToolOutcome:
    """Return `(after_bytes, effective_mutated)`, or the refusal `ToolOutcome`
    for the first gate the buffer fails."""
    after_bytes = {canon: buffer[canon].encode("utf-8") for canon in mutated}
    effective_mutated = {
        canon for canon in mutated if after_bytes[canon] != original_bytes.get(canon)
    }
    if not effective_mutated:
        return _fail(
            last_op_index,
            "SCRIPT_NO_CHANGES",
            "all requested mutations were byte-identical to current files.",
        )

    total = sum(len(after_bytes[c]) for c in effective_mutated)
    if total > _MAX_TOTAL_WRITE_BYTES:
        return _fail(
            last_op_index,
            "SCRIPT_TOO_LARGE",
            f"committed content {total}B exceeds {_MAX_TOTAL_WRITE_BYTES}B.",
        )

    for canon in effective_mutated:
        pre = _syntax_errors(canon, original.get(canon) or "")
        introduced = [e for e in _syntax_errors(canon, buffer[canon]) if e not in pre]
        if introduced:
            return _fail(
                last_op_index,
                "SCRIPT_BATCH_FAILED",
                f"the batch would introduce syntax error(s) in {canon}: "
                f"{'; '.join(introduced)}.",
            )
    return after_bytes, effective_mutated
