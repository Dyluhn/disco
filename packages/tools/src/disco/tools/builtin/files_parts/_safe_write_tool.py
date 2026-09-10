"""``safe_write_file`` — CD-TOOLS-3, the SAFER whole-file writer for
governed/large artifacts.

``SafeWriteFileTool.run`` is decomposed into: the elision check, the existing-
file read, the existing-file validation bundle (sha/binary/grounding/shrink —
all only meaningful when a prior version exists, so they share one helper),
and the syntax pre-check — so the orchestrator itself stays short.
"""

from __future__ import annotations

import hashlib
from typing import Any

from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ...anatomy import ToolContext, ToolDef, ToolOutcome
from ...behavior import declares
from ._canonical import _canonical
from ._constants import _BINARY_DELIVERABLE_EXTS, _FS
from ._elision import _has_elision_marker
from ._governed import _governed_guard
from ._mutation import (
    _commit_file_mutation,
    _resolved_workspace_file,
    _syntax_errors,
    _write_artifact_structured,
)
from ._read_state import _clear_grounding, _read_state
from ._refusal_views import _fresh_read_required, _no_op_write_refusal


class SafeWriteFileArgs(BaseModel):
    path: str = Field(description="Workspace-relative file to write (create or overwrite).")
    content: str = Field(description="Full UTF-8 content to write.")
    allow_shrink: bool = Field(
        default=False,
        description=(
            "Set true to permit a write that shrinks an existing file by >50% (otherwise refused)."
        ),
    )
    expected_sha256: str | None = Field(
        default=None,
        description=(
            "If set, refuse unless the file's CURRENT sha256 equals this (also grounds the write)."
        ),
    )


def _safe_write_elision_outcome(path: str, content: str) -> ToolOutcome | None:
    """(1) never let a render elision placeholder become file content."""
    if not _has_elision_marker(content):
        return None
    return ToolOutcome(
        success=False,
        error="ELISION_MARKER_REJECTED",
        content=(
            f"safe_write_file refused — content for {path} contains an "
            "internal elision placeholder (e.g. '[[DISCO-ELIDED: ...]]'); "
            "read the file and write the real text."
        ),
        structured={
            "kind": "elision_marker_rejected",
            "path": path,
            "next_required_action": "file_read",
        },
    )


async def _safe_write_read_existing(sandbox: Any, mutation_path: str) -> tuple[str | None, bytes]:
    """(3) inspect the existing file (None = new)."""
    old_bytes = b""
    try:
        old_bytes = await sandbox.read_file(mutation_path)
        old_text = old_bytes.decode("utf-8", errors="replace")
    except FileNotFoundError:  # absent file → new write; other read failures must surface
        old_text = None
    return old_text, old_bytes


def _safe_write_existing_checks(
    args: SafeWriteFileArgs,
    ctx: ToolContext,
    old_text: str | None,
    old_bytes: bytes,
) -> tuple[ToolOutcome | None, bool]:
    """The checks that only apply when a prior version of the file exists: the
    expected_sha256 optimistic-concurrency check, the binary-deliverable clobber
    guard, the F1-parity read-before-rewrite guard, and the >50% shrink guard.

    Returns (blocking_outcome_or_None, matching_sha) — matching_sha is True when
    expected_sha256 was supplied and matched, which also satisfies grounding and
    the shrink guard (an explicitly confirmed revision needs no separate read)."""
    if old_text is None:
        return None, False
    matching_sha = False
    cur_sha = hashlib.sha256(old_bytes).hexdigest()
    if args.expected_sha256 is not None:
        if args.expected_sha256 != cur_sha:
            return (
                ToolOutcome(
                    success=False,
                    error="STALE_FILE_CONTEXT",
                    content=(
                        f"safe_write_file refused — {args.path} now hashes to "
                        f"{cur_sha[:12]}…, not the expected "
                        f"{args.expected_sha256[:12]}…; it changed since you read it."
                    ),
                    structured={
                        "kind": "stale_file_context",
                        "path": args.path,
                        "next_required_action": "file_read",
                    },
                ),
                matching_sha,
            )
        matching_sha = True
    # binary-deliverable clobber (parity with file_write) — a text write corrupts a binary.
    ext = args.path.rsplit(".", 1)[-1].lower() if "." in args.path else ""
    if ext in _BINARY_DELIVERABLE_EXTS:
        return (
            ToolOutcome(
                success=False,
                error="binary_deliverable_clobber",
                content=(
                    f"safe_write_file refused — {args.path} is an existing {ext} "
                    "(a generated binary); "
                    "a text write would corrupt it. It is already delivered."
                ),
            ),
            matching_sha,
        )
    grounded = _canonical(args.path) in (
        (_read_state.get(ctx.conversation_id) or {}).get("read_since_write") or set()
    )
    # read-before-rewrite (F1 parity): overwrite an existing file only if grounded (read
    # since last write) OR proven current via a matching expected_sha256.
    if not grounded and not matching_sha:
        return (
            _fresh_read_required(
                args.path, "you have not read this file's current content since it last changed"
            ),
            matching_sha,
        )
    # SHRINK guard — a >50% char shrink truncates a built file (the weak-model clobber).
    if (
        len(args.content) < 0.5 * len(old_text)
        and not args.allow_shrink
        and not matching_sha
    ):
        return (
            ToolOutcome(
                success=False,
                error="SAFE_WRITE_SHRINK_REJECTED",
                content=(
                    f"safe_write_file refused — this would shrink {args.path} from "
                    f"{len(old_text)} to {len(args.content)} chars (>50% smaller), "
                    "which usually means an accidental truncation/clobber. Pass "
                    "allow_shrink=true (or expected_sha256) if intended."
                ),
                structured={
                    "kind": "safe_write_shrink_rejected",
                    "path": args.path,
                    "old_chars": len(old_text),
                    "new_chars": len(args.content),
                },
            ),
            matching_sha,
        )
    return None, matching_sha


def _safe_write_syntax_outcome(
    path: str, old_text: str | None, content: str
) -> ToolOutcome | None:
    """(4) syntax pre-check IN MEMORY — never introduce a new syntax error (no
    write-then-revert)."""
    pre = _syntax_errors(path, old_text or "")
    introduced = [e for e in _syntax_errors(path, content) if e not in pre]
    if introduced:
        return ToolOutcome(
            success=False,
            error="syntax_gate_rejected",
            content=(
                f"safe_write_file refused — this content introduces syntax error(s) in {path}: "
                f"{'; '.join(introduced)} — NOT written. Fix it and retry."
            ),
        )
    return None


class SafeWriteFileTool:
    """CD-TOOLS-3 — the SAFER whole-file writer (a superset of file_write) for governed/large
    artifacts. Validates everything IN MEMORY, then commits atomically (tmp+rename). Guards the
    weak-model whole-file-clobber (a >50% shrink that truncates a built file), refuses writing a
    host-governed `.disco/` artifact via the generic path, and rejects an elision marker."""

    definition = ToolDef(
        name="safe_write_file",
        description=(
            "Write a whole file safely (create or overwrite). Like file_write but with guards: it "
            "refuses to shrink an existing file by >50% (pass allow_shrink=true if intended), "
            "refuses to clobber a host-managed .disco/ artifact (use the semantic tool), rejects "
            "elision placeholders, and writes atomically. Read the file first to overwrite it; "
            "pass expected_sha256 to confirm you have the current version."
        ),
        args_model=SafeWriteFileArgs,
        needs=_FS,
        runs_in="sandbox",
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: SafeWriteFileArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (outcome := _safe_write_elision_outcome(args.path, args.content)) is not None:
            return outcome
        # (2) governed-artifact guard (CD-TOOLS-4) — shared with all generic mutators; routes to
        # the owning semantic tool. Keeps the campaign-named code for safe_write_file.
        if (
            g := await _governed_guard(
                ctx.sandbox,
                args.path,
                error="SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED",
                allowed_tools=ctx.scope_allowed_tools,
            )
        ) is not None:
            return g
        mutation_path = await _resolved_workspace_file(ctx.sandbox, args.path)
        old_text, old_bytes = await _safe_write_read_existing(ctx.sandbox, mutation_path)
        outcome, _matching_sha = _safe_write_existing_checks(args, ctx, old_text, old_bytes)
        if outcome is not None:
            return outcome
        new_bytes = args.content.encode("utf-8")
        if old_text is not None and new_bytes == old_bytes:
            return _no_op_write_refusal(
                ctx.conversation_id,
                args.path,
                tool_name="safe_write_file",
                current_bytes=old_bytes,
            )
        if (
            outcome := _safe_write_syntax_outcome(args.path, old_text, args.content)
        ) is not None:
            return outcome
        # (5) all checks passed → ONE atomic commit (tmp+rename where supported).
        committed = await _commit_file_mutation(
            ctx,
            mutation_path,
            new_bytes,
            expected_before=old_bytes if old_text is not None else None,
        )
        if isinstance(committed, ToolOutcome):
            return committed
        _clear_grounding(ctx.conversation_id, args.path)
        return ToolOutcome(
            success=True,
            content=f"safe_write_file wrote {len(new_bytes)} bytes to {args.path}.",
            artifacts=[args.path],
            structured=_write_artifact_structured(
                args.path,
                new_bytes,
                {"bytes": len(new_bytes)},
            ),
            effect_receipts=(committed,),
        )
