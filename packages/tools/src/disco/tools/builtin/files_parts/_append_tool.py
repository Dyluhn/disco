"""``file_append`` — the dedicated replacement for shell `>>` (which corrupts on
quotes/`$`/backticks)."""

from __future__ import annotations

from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ...anatomy import ToolContext, ToolDef, ToolOutcome
from ...behavior import declares
from ._constants import _FS
from ._governed import _governed_guard
from ._mutation import (
    _gated_write,
    _resolved_workspace_file,
    _write_artifact_structured,
)
from ._refusal_views import _no_op_write_refusal
from ._success_view import _post_change_line_span, _updated_region_success_content


class FileAppendArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to append to.")
    content: str = Field(description="UTF-8 content to append (created if absent).")


class FileAppendTool:
    """Append to a file via the file API — the dedicated replacement for shell
    `>>` (which corrupts on quotes/`$`/backticks). Closes the one legitimate
    reason a model reaches for shell redirection (Cluster 9 <file_rules>)."""

    definition = ToolDef(
        name="file_append",
        description=(
            "Append UTF-8 content to a workspace file (creating it if absent). Use "
            "this instead of shell `>>` — raw-shell append corrupts on special "
            "characters. Large source files are supported; prefer a targeted edit "
            "when changing an existing interior region."
        ),
        args_model=FileAppendArgs,
        needs=_FS,
        runs_in="sandbox",
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: FileAppendArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (
            g := await _governed_guard(
                ctx.sandbox, args.path, allowed_tools=ctx.scope_allowed_tools
            )
        ) is not None:
            return g
        mutation_path = await _resolved_workspace_file(ctx.sandbox, args.path)
        old_text: str | None = None
        existing = b""
        try:
            existing = await ctx.sandbox.read_file(mutation_path)
            old_text = existing.decode("utf-8", errors="replace")
        except FileNotFoundError:  # absent file → start empty; other failures must surface
            existing = b""
            old_text = None
        append_bytes = args.content.encode("utf-8")
        combined = existing + append_bytes
        if old_text is not None and combined == existing:
            return _no_op_write_refusal(
                ctx.conversation_id,
                args.path,
                tool_name="file_append",
                current_bytes=existing,
            )
        # W3 — syntax gate: validate combined content before committing it.
        before = existing if old_text is not None else None
        gated = await _gated_write(
            ctx,
            mutation_path,
            combined,
            old_text,
            expected_before=before,
        )
        if isinstance(gated, ToolOutcome):
            return gated
        changed = _post_change_line_span(old_text or "", combined.decode("utf-8", errors="replace"))
        return ToolOutcome(
            success=True,
            content=_updated_region_success_content(
                ctx.conversation_id,
                args.path,
                combined,
                prefix=f"appended {len(append_bytes)} bytes to {args.path}",
                changed_lines=changed,
            ),
            artifacts=[args.path],
            structured=_write_artifact_structured(args.path, combined),
            effect_receipts=(gated,),
        )
