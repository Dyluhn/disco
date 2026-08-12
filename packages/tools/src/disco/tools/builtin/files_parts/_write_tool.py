"""``file_write`` — whole-file create/overwrite with the F1 read-before-rewrite
guard, the binary-deliverable clobber guard, and the W3 syntax gate.

``FileWriteTool.run`` is decomposed into: elision check, the existing-file
read, the existing-file guard (binary-deliverable + read-before-write), and
the shrink guard — each an independent, narrowly-branching helper so the
orchestrator itself stays small.
"""

from __future__ import annotations

from typing import Any

from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ...anatomy import ToolContext, ToolDef, ToolOutcome
from ...behavior import declares
from ._canonical import _canonical
from ._constants import _BINARY_DELIVERABLE_EXTS, _FS, _MODEL_WRITE_CHUNK_MAX_CHARS
from ._elision import _has_elision_marker
from ._governed import _governed_guard
from ._mutation import (
    _gated_write,
    _resolved_workspace_file,
    _write_artifact_structured,
)
from ._read_state import _conv_state
from ._refusal_views import _no_op_write_refusal
from ._success_view import _updated_region_success_content


class FileWriteArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to write.")
    content: str = Field(
        description=(
            "Full UTF-8 content to write. Keep this call under 12,000 characters; "
            "create larger files with bounded file_append chunks."
        ),
        json_schema_extra={"maxLength": _MODEL_WRITE_CHUNK_MAX_CHARS},
    )
    allow_shrink: bool = Field(
        default=False,
        description=(
            "Set true to permit a write that shrinks an existing file by >50% (otherwise refused)."
        ),
    )


def _file_write_elision_outcome(path: str, content: str) -> ToolOutcome | None:
    if not _has_elision_marker(content):
        return None
    return ToolOutcome(
        success=False,
        error="ELISION_MARKER_REJECTED",
        content=(
            f"file_write refused — content for {path} contains an internal elision "
            "placeholder (for example '[[DISCO-ELIDED: ...]]'). That marker is only a "
            "rendered transcript placeholder, not file content. Recipe: call file_read "
            "for the current file or source region, then retry file_write with the real "
            "complete text."
        ),
        structured={
            "kind": "elision_marker_rejected",
            "path": path,
            "next_required_action": "file_read",
            "suggested_args": {"path": path},
        },
    )


async def _file_write_read_existing(
    sandbox: Any, mutation_path: str
) -> tuple[bytes | None, str | None]:
    """Read existing content once — used by both the F1 guard and the W3 syntax gate."""
    try:
        old_bytes: bytes | None = await sandbox.read_file(mutation_path)
        old_text = old_bytes.decode("utf-8", errors="replace") if old_bytes is not None else None
    except FileNotFoundError:  # absent file is fine; every other read failure is real
        old_bytes = None
        old_text = None
    return old_bytes, old_text


def _file_write_existing_guard(
    args: FileWriteArgs, ctx: ToolContext, old_text: str | None
) -> ToolOutcome | None:
    """F1 — read-before-rewrite guard (ALL tiers, no assist gate). Refuse a
    file_write to an EXISTING file if neither a successful file_read nor a
    success-observation-bearing mutator has grounded the current path in
    this conversation. A NEW (nonexistent) file is always allowed — there is no
    prior content to ground on."""
    if old_text is None:  # absent file: no prior content to ground on, always allowed
        return None
    # ROOT-2 — binary-deliverable clobber guard. The target is an EXISTING file
    # of a generated-binary type; a text file_write would corrupt it. Refuse
    # with a terminal message so the agent stops trying to "fix"/overwrite a
    # finished deck/doc and recognises it is already delivered. (Only EXISTING
    # binaries are blocked — a new text file of any name is still allowed.)
    ext = args.path.rsplit(".", 1)[-1].lower() if "." in args.path else ""
    if ext in _BINARY_DELIVERABLE_EXTS:
        return ToolOutcome(
            success=False,
            error="binary_deliverable_clobber",
            content=(
                f"{args.path} is a generated binary deliverable; do not overwrite "
                f"it with text. It is already produced and delivered. If it truly "
                f"needs to change, regenerate it with the tool that produced it "
                f"(e.g. slides_generate) — never file_write into it."
            ),
        )
    canonical = _canonical(args.path)
    if canonical not in _conv_state(ctx.conversation_id)["read_since_write"]:
        return ToolOutcome(
            success=False,
            error="read_before_write",
            content=(
                f"file_write refused: {args.path} already exists and has not been "
                f"read since the last write to it. Read it first "
                f"(file_read) to ground your edit in the current content, or use a "
                f"targeted edit (file_replace_lines / file_insert_lines / file_edit) "
                f"instead of rewriting the whole file from memory."
            ),
        )
    return None


def _file_write_shrink_outcome(args: FileWriteArgs, old_text: str | None) -> ToolOutcome | None:
    if old_text is None:
        return None
    if len(args.content) < 0.5 * len(old_text) and not args.allow_shrink:
        return ToolOutcome(
            success=False,
            error="FILE_WRITE_SHRINK_REJECTED",
            content=(
                f"file_write refused — this would shrink {args.path} from {len(old_text)} "
                f"to {len(args.content)} chars (>50% smaller), which usually means an "
                "accidental truncation or stale full-file rewrite. Recipe: if the shrink "
                "is intentional, retry with allow_shrink=true after confirming the current "
                "file content; otherwise use file_edit, file_replace_lines, or "
                "file_insert_lines for the targeted change."
            ),
            structured={
                "kind": "file_write_shrink_rejected",
                "path": args.path,
                "old_chars": len(old_text),
                "new_chars": len(args.content),
                "next_required_action": "file_write",
                "suggested_args": {
                    "path": args.path,
                    "content": args.content,
                    "allow_shrink": True,
                },
            },
        )
    return None


class FileWriteTool:
    definition = ToolDef(
        name="file_write",
        description=(
            "Create a NEW UTF-8 text file in the workspace with its full content "
            "(preferred over shell redirection for new files). To change an EXISTING "
            "file, make a targeted edit with file_edit / file_replace_lines instead — "
            "only rewrite a whole existing file when a targeted edit cannot express "
            "the change. Whole-file rewrites are guarded: read the file first, do not "
            "copy elision placeholders, and pass allow_shrink=true only when a >50% "
            "shrink is intentional. Writes to host-managed .disco/ artifacts are "
            "refused, and successful writes commit atomically. Large source files "
            "are supported through bounded calls; keep each content argument under "
            "12,000 characters, then use bounded file_append chunks. Use bounded file_read "
            "ranges and targeted edit tools "
            "for later changes instead of reproducing the whole file from memory."
        ),
        args_model=FileWriteArgs,
        needs=_FS,
        runs_in="sandbox",
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: FileWriteArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (
            g := await _governed_guard(
                ctx.sandbox, args.path, allowed_tools=ctx.scope_allowed_tools
            )
        ) is not None:
            return g
        mutation_path = await _resolved_workspace_file(ctx.sandbox, args.path)
        if (outcome := _file_write_elision_outcome(args.path, args.content)) is not None:
            return outcome
        old_bytes, old_text = await _file_write_read_existing(ctx.sandbox, mutation_path)
        if (outcome := _file_write_existing_guard(args, ctx, old_text)) is not None:
            return outcome
        # W3 — syntax gate: validate first, then commit only accepted bytes.
        raw = args.content.encode("utf-8")
        if old_bytes is not None and raw == old_bytes:
            return _no_op_write_refusal(
                ctx.conversation_id,
                args.path,
                tool_name="file_write",
                current_bytes=old_bytes,
            )
        if (outcome := _file_write_shrink_outcome(args, old_text)) is not None:
            return outcome
        gated = await _gated_write(
            ctx,
            mutation_path,
            raw,
            old_text,
            expected_before=old_bytes,
        )
        if isinstance(gated, ToolOutcome):
            return gated
        return ToolOutcome(
            success=True,
            content=_updated_region_success_content(
                ctx.conversation_id,
                args.path,
                raw,
                prefix=f"wrote {len(raw)} bytes to {args.path}",
                file_write_head=True,
            ),
            artifacts=[args.path],
            structured=_write_artifact_structured(args.path, raw),
            effect_receipts=(gated,),
        )
