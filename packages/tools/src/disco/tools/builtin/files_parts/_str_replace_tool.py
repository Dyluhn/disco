"""``file_str_replace`` — W4 anchored str-replace for capable models.

``FileStrReplaceTool.run`` is decomposed into a zero-match handler (the
whitespace-strip retry + terminal not-found refusal) and a shared apply/commit
helper (used by both the whitespace-retry success path and the single-match
path), so the orchestrator itself stays short.
"""

from __future__ import annotations

from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ...anatomy import ToolContext, ToolDef, ToolOutcome
from ...behavior import declares
from ._constants import _FS
from ._fresh_edit_guard import guard_fresh_edit
from ._fuzzy_match import _best_fuzzy_old_match_lines, _matched_old_lines
from ._governed import _governed_guard
from ._mutation import _gated_write, _write_artifact_structured
from ._refusal_views import _no_op_edit_refusal, _old_text_not_found_refusal
from ._success_view import _post_change_line_span, _updated_region_success_content


def _occurrence_lines(text: str, needle: str) -> list[int]:
    """Return 1-based line numbers of every occurrence of needle in text."""
    results: list[int] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            break
        results.append(text[:idx].count("\n") + 1)
        start = idx + max(len(needle), 1)
    return results


class FileStrReplaceArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to edit.")
    old_str: str = Field(description="Exact text to find (must appear EXACTLY ONCE in the file).")
    new_str: str = Field(description="Replacement text.")


async def _str_replace_commit(
    ctx: ToolContext,
    args: FileStrReplaceArgs,
    text: str,
    raw: bytes,
    *,
    old_for_match: str,
    new_text: str,
    prefix: str,
) -> ToolOutcome:
    if new_text == text:
        return _no_op_edit_refusal(
            ctx.conversation_id,
            args.path,
            base_content=(
                f"file_str_replace refused: {args.path} already contains the "
                "requested replacement; nothing changed."
            ),
            current_bytes=raw,
            attempted_lines=_matched_old_lines(text, old_for_match),
        )
    gated = await _gated_write(
        ctx,
        args.path,
        new_text.encode("utf-8"),
        text,
        expected_before=raw,
    )
    if isinstance(gated, ToolOutcome):
        return gated
    new_bytes = new_text.encode("utf-8")
    return ToolOutcome(
        success=True,
        content=_updated_region_success_content(
            ctx.conversation_id,
            args.path,
            new_bytes,
            prefix=prefix,
            changed_lines=_post_change_line_span(text, new_text),
        ),
        artifacts=[args.path],
        structured=_write_artifact_structured(args.path, new_bytes),
        effect_receipts=(gated,),
    )


async def _str_replace_zero_match(
    ctx: ToolContext, args: FileStrReplaceArgs, text: str, raw: bytes
) -> ToolOutcome:
    """Whitespace-strip retry: one chance with leading/trailing whitespace removed."""
    stripped = args.old_str.strip()
    if stripped and stripped != args.old_str:
        retry_count = text.count(stripped)
        if retry_count == 1:
            new_text = text.replace(stripped, args.new_str, 1)
            return await _str_replace_commit(
                ctx,
                args,
                text,
                raw,
                old_for_match=stripped,
                new_text=new_text,
                prefix=f"replaced in {args.path} (whitespace-stripped match)",
            )
    return _old_text_not_found_refusal(
        ctx.conversation_id,
        args.path,
        current_bytes=raw,
        attempted_old=args.old_str,
        attempted_new=args.new_str,
        error="old_str_not_found",
        tool_name="file_str_replace",
        base_content=(
            f"`old_str` did not appear verbatim in {args.path}. Copy the exact "
            "current region you want to replace."
        ),
    )


class FileStrReplaceTool:
    """W4 — anchored str-replace for capable models (Requirement.ANCHORED_EDIT).

    Clean-room of OpenHands str_replace: reads the whole file, locates ALL
    occurrences of old_str, requires exactly one (multiple → error with line
    numbers; zero → whitespace-strip retry, then 'did not appear verbatim').
    No forgiving normalization — anchored on live disk text.
    Registry withholds this tool from the weak tier via advertised_tools.
    """

    definition = ToolDef(
        name="file_str_replace",
        description=(
            "Replace text appearing EXACTLY ONCE in a workspace file. "
            "Reads the current file and finds ALL occurrences of `old_str`: "
            "multiple matches → error with line numbers (make `old_str` unique first); "
            "zero matches (after a whitespace-strip retry) → 'did not appear verbatim'. "
            "Anchored on live disk text — no forgiving normalization. "
            "Offered only to capable models (Requirement.ANCHORED_EDIT); "
            "weak tier should use file_write or file_edit."
        ),
        args_model=FileStrReplaceArgs,
        needs=_FS,
        runs_in="sandbox",
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: FileStrReplaceArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (
            g := await _governed_guard(
                ctx.sandbox, args.path, allowed_tools=ctx.scope_allowed_tools
            )
        ) is not None:
            return g
        _raw = await ctx.sandbox.read_file(args.path)
        text = _raw.decode("utf-8", errors="replace")
        # CD-TOOLS-1 fresh-edit guard (anchored exact replace) — region from old_str's location.
        _idx = text.find(args.old_str)
        _elines = (
            (
                text.count("\n", 0, _idx) + 1,
                text.count("\n", 0, _idx) + 1 + args.old_str.count("\n"),
            )
            if _idx >= 0
            else _best_fuzzy_old_match_lines(text, args.old_str)
        )
        _blocked = guard_fresh_edit(
            ctx.conversation_id,
            args.path,
            current_bytes=_raw,
            old=args.old_str,
            new=args.new_str,
            edit_lines=_elines,
            line_refusal_read=True,
        )
        if _blocked is not None:
            return _blocked

        count = text.count(args.old_str)
        if count > 1:
            lines = _occurrence_lines(text, args.old_str)
            return ToolOutcome(
                success=False,
                error="old_str_not_unique",
                content=(
                    f"Multiple occurrences ({count}) of `old_str` found in {args.path} "
                    f"at lines {lines}. Please ensure it is unique before applying."
                ),
            )

        if count == 0:
            return await _str_replace_zero_match(ctx, args, text, _raw)

        # Exactly one occurrence — apply and pass through W3 gate.
        new_text = text.replace(args.old_str, args.new_str, 1)
        return await _str_replace_commit(
            ctx,
            args,
            text,
            _raw,
            old_for_match=args.old_str,
            new_text=new_text,
            prefix=f"replaced in {args.path}",
        )
