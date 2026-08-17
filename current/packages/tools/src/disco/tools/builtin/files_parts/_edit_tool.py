"""``file_edit`` — forgiving anchored replace-first-occurrence, the SAFEST
targeted edit (it anchors on text the model gives, so it can't hit the wrong
place)."""

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
from ._text_norm import _norm_ws, _strip_line_numbers


class FileEditArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to edit.")
    old: str = Field(description="Text to replace (the first occurrence).")
    new: str = Field(description="Replacement text.")


def _forgiving_replace(text: str, old: str, new: str) -> tuple[str | None, str]:
    """Replace the first occurrence of `old` with `new`, FORGIVINGLY — small models
    (and large ones) rarely reproduce a long substring byte-perfectly. Tries, in
    order: exact match; with accidental line-number prefixes stripped from `old`;
    whitespace-normalized match (per-line rstrip, drop blank edges) located back in
    the original text. Returns (updated_text_or_None, note). None ⇒ not found."""
    if old in text:
        return text.replace(old, new, 1), "exact"
    old2 = _strip_line_numbers(old)
    if old2 != old and old2 in text:
        return text.replace(old2, new, 1), "stripped line numbers"
    # whitespace-normalized: find the contiguous line span whose rstrip'd form
    # equals the rstrip'd `old`, then splice the ORIGINAL lines out.
    target = _norm_ws(old2)
    if target:
        doc = text.splitlines(keepends=True)
        norm = [x.strip() for x in doc]
        tgt = target.split("\n")
        for i in range(0, len(norm) - len(tgt) + 1):
            if norm[i : i + len(tgt)] == tgt:
                updated = (
                    "".join(doc[:i])
                    + new
                    + ("" if new.endswith("\n") else "\n")
                    + "".join(doc[i + len(tgt) :])
                )
                return updated, "whitespace-normalized"
    return None, "not found"


def _nearest_anchor(text: str, old: str) -> str:
    """A short hint for a failed edit: the line in the file most similar to the
    first non-blank line of `old`, so the model can re-aim."""
    first = next((ln.strip() for ln in _strip_line_numbers(old).splitlines() if ln.strip()), "")
    if not first:
        return ""
    token = first[:24]
    for n, ln in enumerate(text.splitlines(), 1):
        if token and token in ln:
            return f" (similar text near line {n}: {ln.strip()[:60]!r})"
    return ""


class FileEditTool:
    definition = ToolDef(
        name="file_edit",
        description=(
            "Replace the first occurrence of `old` with `new` in a workspace file. "
            "Matching is forgiving (tolerates indentation / trailing-space drift and "
            "pasted-in line numbers). This is the SAFEST targeted edit — it anchors on "
            "the text you give, so it can't hit the wrong place. For a large file, read "
            "the relevant section first (file_read with offset/limit) and pass that exact "
            "snippet as `old`. Your previous edit's observation shows the CURRENT "
            "numbering for that region — use it; re-read only if you edited elsewhere since."
        ),
        args_model=FileEditArgs,
        needs=_FS,
        runs_in="sandbox",
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: FileEditArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (
            g := await _governed_guard(
                ctx.sandbox, args.path, allowed_tools=ctx.scope_allowed_tools
            )
        ) is not None:
            return g
        # Intent no-op: old and new are LITERALLY identical (after stripping any
        # line-number prefixes the model copied). This is distinct from a
        # whitespace-only edit (old≠new, which must apply) — here the model asked
        # for no change at all, so refuse before touching the file regardless of how
        # the file's own whitespace happens to differ. The ground-truth guard below
        # still catches the "applied result is unchanged" case.
        if _strip_line_numbers(args.old) == _strip_line_numbers(args.new):
            current_bytes: bytes | None = None
            attempted_lines: tuple[int, int] | None = None
            try:
                current_bytes = await ctx.sandbox.read_file(args.path)
                if current_bytes is not None:
                    attempted_lines = _matched_old_lines(
                        current_bytes.decode("utf-8", errors="replace"), args.old
                    )
            except Exception:  # noqa: BLE001 — keep the original no-op refusal if unreadable
                current_bytes = None
            return _no_op_edit_refusal(
                ctx.conversation_id,
                args.path,
                base_content=(
                    f"file_edit refused: `old` and `new` are identical — this asks for "
                    f"no change to {args.path}. If you already applied this edit, move "
                    "on; otherwise give the NEW content you want."
                ),
                current_bytes=current_bytes,
                attempted_lines=attempted_lines,
            )
        _raw = await ctx.sandbox.read_file(args.path)
        text = _raw.decode("utf-8", errors="replace")
        # CD-TOOLS-1 fresh-edit guard: refuse (no mutation) when the model lacks fresh/complete/
        # un-elided grounding of the edit region. Compute the region from `old`'s exact location.
        _idx = text.find(args.old)
        _elines = (
            (text.count("\n", 0, _idx) + 1, text.count("\n", 0, _idx) + 1 + args.old.count("\n"))
            if _idx >= 0
            else _best_fuzzy_old_match_lines(text, args.old)
        )
        _blocked = guard_fresh_edit(
            ctx.conversation_id,
            args.path,
            current_bytes=_raw,
            old=args.old,
            new=args.new,
            edit_lines=_elines,
            line_refusal_read=True,
        )
        if _blocked is not None:
            return _blocked
        updated, how = _forgiving_replace(text, args.old, _strip_line_numbers(args.new))
        if updated is None:
            return _old_text_not_found_refusal(
                ctx.conversation_id,
                args.path,
                current_bytes=_raw,
                attempted_old=args.old,
                attempted_new=args.new,
                error="old_text_not_found",
                base_content=(
                    f"`old` not found in {args.path} (tried exact + whitespace-tolerant)."
                    + _nearest_anchor(text, args.old)
                    + " Tip: copy the exact current region you want to replace, or use "
                    "file_replace_lines with the current line numbers."
                ),
            )
        # No-op guard, on GROUND TRUTH: refuse only when the replacement leaves the
        # file byte-identical. Checking the *applied* result (not an abstract
        # old-vs-new compare) lets a legitimate whitespace-only edit through — a
        # re-indent or trailing-space cleanup matches ws-tolerantly but writes
        # `new` verbatim, so `updated != text` and it applies — while still catching
        # the real no-op: an already-applied edit (or old≈new) that changes nothing.
        if updated == text:
            return _no_op_edit_refusal(
                ctx.conversation_id,
                args.path,
                base_content=(
                    f"file_edit refused: this edit leaves {args.path} unchanged — the "
                    "new content already matches what's on disk (it may have been "
                    "applied on an earlier turn). No further action is needed; move on."
                ),
                current_bytes=_raw,
                attempted_lines=_matched_old_lines(text, args.old),
            )
        # W3 — syntax gate: validate updated content before committing it.
        gated = await _gated_write(
            ctx,
            args.path,
            updated.encode("utf-8"),
            text,
            expected_before=_raw,
        )
        if isinstance(gated, ToolOutcome):
            return gated
        updated_bytes = updated.encode("utf-8")
        return ToolOutcome(
            success=True,
            content=_updated_region_success_content(
                ctx.conversation_id,
                args.path,
                updated_bytes,
                prefix=f"edited {args.path} ({how})",
                changed_lines=_post_change_line_span(text, updated),
            ),
            artifacts=[args.path],
            structured=_write_artifact_structured(args.path, updated_bytes),
            effect_receipts=(gated,),
        )
