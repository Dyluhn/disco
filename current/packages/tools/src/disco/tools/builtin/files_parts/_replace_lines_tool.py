"""``file_replace_lines`` — surgical, large-file-friendly line-range replace."""

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


class FileReplaceLinesArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to edit.")
    start_line: int = Field(description="First line to replace (1-based, inclusive).")
    end_line: int = Field(description="Last line to replace (1-based, inclusive).")
    new_text: str = Field(
        description=(
            "Replacement text for that line range (can be multi-line; keep this call "
            "under 12,000 characters)."
        ),
        json_schema_extra={"maxLength": _MODEL_WRITE_CHUNK_MAX_CHARS},
    )


class FileReplaceLinesTool:
    """Surgical, large-file-friendly edit: replace an inclusive 1-based LINE RANGE
    with new text. The model reads the numbered file, picks the range, and writes
    the replacement without reproducing the old bytes."""

    definition = ToolDef(
        name="file_replace_lines",
        description=(
            "Replace lines [start_line, end_line] (1-based, inclusive) of a file with "
            "`new_text`. Good for large files where reproducing exact text is hard. "
            "IMPORTANT: your previous edit's observation shows the CURRENT numbering "
            "for that region — use it; re-read only if you edited elsewhere since. "
            "Keep each new_text argument under 12,000 characters. Use file_insert_lines "
            "to insert without replacing or to split a larger addition into bounded calls."
        ),
        args_model=FileReplaceLinesArgs,
        needs=_FS,
        runs_in="sandbox",
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: FileReplaceLinesArgs, ctx: ToolContext) -> ToolOutcome:
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
        if args.start_line < 1 or args.end_line < args.start_line or args.start_line > n + 1:
            return ToolOutcome(
                success=False,
                content=(
                    f"bad range [{args.start_line},{args.end_line}] for {args.path} "
                    f"({n} lines). start_line must be 1..{n + 1}, end_line >= start_line."
                ),
                error="bad_range",
            )
        # CD-TOOLS-1 fresh-edit guard: line numbers shift after any edit, so a stale/elided
        # range silently overwrites the wrong lines — require fresh, covering grounding first.
        _blocked = guard_fresh_edit(
            ctx.conversation_id,
            args.path,
            current_bytes=_raw,
            new=args.new_text,
            edit_lines=(args.start_line, min(args.end_line, max(n, 1))),
            # [REL-RC-D] line-based: a prior edit's line shift can stale these numbers.
            anchored=False,
            attempted_lines=(args.start_line, min(args.end_line, max(n, 1))),
            line_refusal_read=True,
        )
        if _blocked is not None:
            return _blocked
        # Deletion guard: empty new_text over a real range is the silent-data-loss path
        # (a miscounted range replaced with nothing — the exact gpt-oss-120b failure).
        if _strip_line_numbers(args.new_text).strip() == "":
            end_g = min(args.end_line, n)
            doomed = max(0, end_g - args.start_line + 1)
            return ToolOutcome(
                success=False,
                content=(
                    f"file_replace_lines refused: new_text is empty — this would DELETE "
                    f"lines {args.start_line}-{end_g} ({doomed} lines) of {args.path} with "
                    "no replacement, the usual symptom of a miscounted range. To remove "
                    "code, use file_write to rewrite the file without those lines; to clear "
                    "a block on purpose, replace it with a placeholder comment."
                ),
                error="empty_replacement_refused",
            )
        new_lines = _strip_line_numbers(args.new_text).split("\n")
        end = min(args.end_line, n)
        result = lines[: args.start_line - 1] + new_lines + lines[end:]
        out = "\n".join(result)
        if text.endswith("\n"):
            out += "\n"
        if out.encode("utf-8") == _raw:
            return _no_op_edit_refusal(
                ctx.conversation_id,
                args.path,
                base_content=(
                    "file_replace_lines refused: the replacement is byte-identical to "
                    f"the current lines in {args.path}; nothing changed."
                ),
                current_bytes=_raw,
                attempted_lines=(args.start_line, min(args.end_line, max(n, 1))),
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
        replaced = max(0, end - args.start_line + 1)
        changed_hi = args.start_line + max(len(new_lines), 1) - 1
        out_bytes = out.encode("utf-8")
        return ToolOutcome(
            success=True,
            content=_updated_region_success_content(
                ctx.conversation_id,
                args.path,
                out_bytes,
                changed_lines=(args.start_line, changed_hi),
                prefix=(
                    f"replaced lines {args.start_line}-{end} of {args.path} "
                    f"({replaced}→{len(new_lines)} lines)"
                ),
            ),
            artifacts=[args.path],
            structured=_write_artifact_structured(args.path, out_bytes),
            effect_receipts=(gated,),
        )
