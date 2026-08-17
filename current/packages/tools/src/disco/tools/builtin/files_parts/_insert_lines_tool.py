"""``file_insert_lines`` — insert text after a given line without replacing
anything, the clean way to ADD a block to a large file by line number."""

from __future__ import annotations

from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ...anatomy import ToolContext, ToolDef, ToolOutcome
from ...behavior import declares
from ._constants import _FS, _MODEL_WRITE_CHUNK_MAX_CHARS
from ._fresh_edit_guard import guard_fresh_edit
from ._governed import _governed_guard
from ._mutation import _gated_write, _write_artifact_structured
from ._refusal_views import _no_op_edit_refusal
from ._success_view import _updated_region_success_content
from ._text_norm import _strip_line_numbers


class FileInsertLinesArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to edit.")
    after_line: int = Field(
        description="Insert AFTER this 1-based line (0 = at the very top of the file)."
    )
    text: str = Field(
        description="Text to insert (can be multi-line; keep this call under 12,000 characters).",
        json_schema_extra={"maxLength": _MODEL_WRITE_CHUNK_MAX_CHARS},
    )


class FileInsertLinesTool:
    """Insert text after a given line WITHOUT replacing anything — the clean way to
    ADD a block (a new section/app) to a large file by line number."""

    definition = ToolDef(
        name="file_insert_lines",
        description=(
            "Insert `text` AFTER line `after_line` (1-based; 0 = top) of a file, without "
            "replacing anything. Read the file for line numbers first. The reliable way "
            "to ADD a block to a large file. Keep each inserted text argument under "
            "12,000 characters; use additional bounded insertions for a larger block."
        ),
        args_model=FileInsertLinesArgs,
        needs=_FS,
        runs_in="sandbox",
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: FileInsertLinesArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (
            g := await _governed_guard(
                ctx.sandbox, args.path, allowed_tools=ctx.scope_allowed_tools
            )
        ) is not None:
            return g
        _raw = await ctx.sandbox.read_file(args.path)
        text = _raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        n = len(lines)
        # CD-TOOLS-1 guard: the insert point relies on current line numbers — require fresh
        # grounding + reject an elision marker in the inserted text (no region coverage needed).
        _blocked = guard_fresh_edit(
            ctx.conversation_id,
            args.path,
            current_bytes=_raw,
            new=args.text,
            edit_lines=(
                max(args.after_line, 1),
                max(1, min(args.after_line + 1, n)),
            ),
            # [REL-RC-D] line-based: a prior edit can stale this insert point.
            anchored=False,
            attempted_lines=(max(args.after_line, 1), max(args.after_line, 1)),
            line_refusal_read=True,
        )
        if _blocked is not None:
            return _blocked
        if args.after_line < 0 or args.after_line > n:
            return ToolOutcome(
                success=False,
                content=f"after_line {args.after_line} out of range for {args.path} (0..{n}).",
                error="bad_line",
            )
        ins = _strip_line_numbers(args.text).split("\n")
        result = lines[: args.after_line] + ins + lines[args.after_line :]
        out = "\n".join(result)
        if text.endswith("\n"):
            out += "\n"
        if out.encode("utf-8") == _raw:
            return _no_op_edit_refusal(
                ctx.conversation_id,
                args.path,
                base_content=(
                    "file_insert_lines refused: the insertion leaves "
                    f"{args.path} byte-identical; nothing changed."
                ),
                current_bytes=_raw,
                attempted_lines=(max(args.after_line, 1), max(args.after_line, 1)),
            )
        # W3 — syntax gate: validate the result before committing it.
        gated = await _gated_write(
            ctx,
            args.path,
            out.encode("utf-8"),
            text,
            expected_before=_raw,
        )
        if isinstance(gated, ToolOutcome):
            return gated
        changed_start = args.after_line + 1
        changed_hi = changed_start + max(len(ins), 1) - 1
        out_bytes = out.encode("utf-8")
        return ToolOutcome(
            success=True,
            content=_updated_region_success_content(
                ctx.conversation_id,
                args.path,
                out_bytes,
                changed_lines=(changed_start, changed_hi),
                prefix=f"inserted {len(ins)} lines after line {args.after_line} of {args.path}",
            ),
            artifacts=[args.path],
            structured=_write_artifact_structured(args.path, out_bytes),
            effect_receipts=(gated,),
        )
