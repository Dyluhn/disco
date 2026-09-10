"""``exact_replace`` — CD-TOOLS-2, the atomic exact-replacement primitive.

``ExactReplaceTool.run`` is decomposed into the eight numbered steps its
docstring/comments already called out (elision check, optimistic-concurrency
check, per-edit span collection, overlap check, fresh-edit guard, splice,
syntax pre-check, commit) as separate helpers; `run` itself only sequences
them and returns on the first failure.
"""

from __future__ import annotations

import hashlib
from typing import Any

from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ...anatomy import ToolContext, ToolDef, ToolOutcome
from ...behavior import declares
from ._constants import _FS
from ._elision import _has_elision_marker
from ._fresh_edit_guard import guard_fresh_edit
from ._governed import _governed_guard
from ._mutation import _commit_file_mutation, _syntax_errors, _write_artifact_structured
from ._refusal_views import _no_op_edit_refusal, _old_text_not_found_refusal
from ._success_view import _post_change_line_span, _updated_region_success_content


class ExactReplaceEdit(BaseModel):
    old_string: str = Field(description="Exact text to find — must occur once unless multi=true.")
    new_string: str = Field(
        description="Replacement, written LITERALLY (no regex/backref/template expansion)."
    )


class ExactReplaceArgs(BaseModel):
    path: str = Field(description="Workspace-relative file to edit.")
    edits: list[ExactReplaceEdit] = Field(
        description="One or more exact replacements; applied ALL-or-NOTHING (atomic)."
    )
    # NOTE: there is deliberately NO `require_fresh_read` toggle — the fresh-read/coverage guard
    # is MANDATORY and non-bypassable (a model opt-out would re-open the Mode-B large-file thrash;
    # Codex CD-TOOLS-2 round-1). It is size-gated, so small files never need a separate read.
    expected_sha256: str | None = Field(
        default=None,
        description=(
            "If set, refuse unless the file's CURRENT sha256 equals this (optimistic concurrency)."
        ),
    )
    multi: bool = Field(
        default=False,
        description="Allow an old_string to match more than once and replace EVERY occurrence.",
    )


def _all_occurrences(text: str, sub: str) -> list[int]:
    """Non-overlapping start offsets of `sub` in `text` (advances by len(sub))."""
    out: list[int] = []
    if not sub:
        return out
    i = text.find(sub)
    while i != -1:
        out.append(i)
        i = text.find(sub, i + len(sub))
    return out


def _exact_replace_reject_elision(path: str, edits: list[ExactReplaceEdit]) -> ToolOutcome | None:
    """(1) elision marker in any old/new — never let a render placeholder enter source."""
    for e in edits:
        if _has_elision_marker(e.old_string, e.new_string):
            return ToolOutcome(
                success=False,
                error="ELISION_MARKER_REJECTED",
                content=(
                    f"exact_replace refused — an edit for {path} contains an "
                    "internal elision placeholder (e.g. '[[DISCO-ELIDED: ...]]'); "
                    "read the file and use the real text."
                ),
                structured={
                    "kind": "elision_marker_rejected",
                    "path": path,
                    "next_required_action": "file_read",
                    "suggested_args": {"path": path},
                },
            )
    return None


def _exact_replace_check_sha(
    path: str, raw: bytes, expected_sha256: str | None
) -> ToolOutcome | None:
    """(2) optimistic concurrency: caller-supplied expected sha must match the current
    disk bytes."""
    cur_sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and expected_sha256 != cur_sha:
        return ToolOutcome(
            success=False,
            error="STALE_FILE_CONTEXT",
            content=(
                f"exact_replace refused — {path} now hashes to {cur_sha[:12]}…, "
                f"not the expected {expected_sha256[:12]}…; it changed since "
                "you read it. Read it again, then edit."
            ),
            structured={
                "kind": "stale_file_context",
                "path": path,
                "next_required_action": "file_read",
                "suggested_args": {"path": path},
            },
        )
    return None


def _exact_replace_collect_spans(
    conv_id: str,
    path: str,
    raw: bytes,
    text: str,
    edits: list[ExactReplaceEdit],
    multi: bool,
) -> tuple[list[tuple[int, int, str]], list[dict[str, Any]]] | ToolOutcome:
    """(3) per-edit match counts + collect ALL match spans on the ORIGINAL text."""
    spans: list[tuple[int, int, str]] = []  # (start, end, new_string)
    applied: list[dict[str, Any]] = []
    for e in edits:
        occ = _all_occurrences(text, e.old_string)
        if len(occ) == 0:
            return _old_text_not_found_refusal(
                conv_id,
                path,
                current_bytes=raw,
                attempted_old=e.old_string,
                attempted_new=e.new_string,
                error="EXACT_REPLACE_NO_MATCH",
                tool_name="exact_replace",
                base_content=(
                    f"exact_replace: old_string not found in {path}: {e.old_string[:60]!r}."
                ),
            )
        if len(occ) > 1 and not multi:
            return ToolOutcome(
                success=False,
                error="EXACT_REPLACE_DUPLICATE_MATCH",
                content=(
                    f"exact_replace: old_string occurs {len(occ)}× in {path} — "
                    "make it unique or "
                    f"set multi=true to replace all: {e.old_string[:60]!r}."
                ),
            )
        targets = occ if multi else occ[:1]
        for s in targets:
            spans.append((s, s + len(e.old_string), e.new_string))
        applied.append(
            {"old_string": e.old_string[:60], "occurrences": len(occ), "replaced": len(targets)}
        )
    return spans, applied


def _exact_replace_check_overlap(
    path: str, spans: list[tuple[int, int, str]]
) -> ToolOutcome | None:
    """(4) overlap: two matched regions intersecting on the ORIGINAL text → ambiguous, reject."""
    spans.sort(key=lambda t: t[0])
    for i in range(1, len(spans)):
        if spans[i][0] < spans[i - 1][1]:
            return ToolOutcome(
                success=False,
                error="EXACT_REPLACE_BATCH_FAILED",
                content=(
                    f"exact_replace: edits overlap in {path} (matched regions intersect) — "
                    "split or de-duplicate them."
                ),
            )
    return None


def _exact_replace_check_fresh_edit(
    conv_id: str, path: str, raw: bytes, text: str, spans: list[tuple[int, int, str]]
) -> ToolOutcome | None:
    """(5) fresh-read/grounding/coverage — MANDATORY, per edit region (reuse the CD-TOOLS-1
    guard; size-gated so small files pass freely). Non-bypassable: no opt-out arg exists."""
    for s, end, _repl in spans:
        lo_line = text.count("\n", 0, s) + 1
        hi_line = text.count("\n", 0, end) + 1
        blocked = guard_fresh_edit(
            conv_id,
            path,
            current_bytes=raw,
            edit_lines=(lo_line, hi_line),
            line_refusal_read=True,
        )
        if blocked is not None:
            return blocked
    return None


def _exact_replace_splice(text: str, spans: list[tuple[int, int, str]]) -> str:
    """(6) splice by position (descending so earlier indices stay valid) —
    str slicing, NOT re.sub, so '$', '${x}', backrefs in new_string are literal."""
    new_text = text
    for s, end, repl in sorted(spans, key=lambda t: t[0], reverse=True):
        new_text = new_text[:s] + repl + new_text[end:]
    return new_text


def _exact_replace_check_syntax(path: str, text: str, new_text: str) -> ToolOutcome | None:
    """(7) syntax pre-check IN MEMORY (no write yet): the batch must not INTRODUCE new errors."""
    pre = _syntax_errors(path, text)
    introduced = [er for er in _syntax_errors(path, new_text) if er not in pre]
    if introduced:
        return ToolOutcome(
            success=False,
            error="EXACT_REPLACE_BATCH_FAILED",
            content=(
                f"exact_replace: the batch would introduce syntax error(s) in {path}: "
                f"{'; '.join(introduced)} — NOT applied. Fix the snippet and retry."
            ),
        )
    return None


class ExactReplaceTool:
    """CD-TOOLS-2 — the atomic exact-replacement primitive (Claude Design dc_*_str_replace).

    Replaces fragile broad file_edit for targeted edits: each old_string must match EXACTLY
    (literal — no whitespace forgiving), the whole batch applies all-or-nothing, and EVERY check
    runs IN MEMORY before a single byte is written (so a failed batch never touches disk — no
    write-then-revert). Reuses the CD-TOOLS-1 fresh-edit guard for stale/elision/grounding."""

    definition = ToolDef(
        name="exact_replace",
        description=(
            "Apply one or more EXACT string replacements to a file, atomically. Each `old_string` "
            "must occur exactly once (set multi=true to replace all occurrences); `new_string` is "
            "written literally (no regex/backref expansion). The whole batch applies "
            "all-or-nothing "
            "— if ANY edit fails (no match, duplicate, overlap, or it would introduce a syntax "
            "error) NOTHING is written. Read the file first (the exact text you see is what to "
            "match). Pass expected_sha256 to refuse if the file changed under you."
        ),
        args_model=ExactReplaceArgs,
        needs=_FS,
        runs_in="sandbox",
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: ExactReplaceArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        if (
            g := await _governed_guard(
                ctx.sandbox, args.path, allowed_tools=ctx.scope_allowed_tools
            )
        ) is not None:  # CD-TOOLS-4: close the bypass
            return g
        if not args.edits:
            return ToolOutcome(
                success=False,
                error="EXACT_REPLACE_BATCH_FAILED",
                content="exact_replace: no edits supplied.",
            )
        raw = await ctx.sandbox.read_file(args.path)
        text = raw.decode("utf-8", errors="replace")

        if (outcome := _exact_replace_reject_elision(args.path, args.edits)) is not None:
            return outcome
        if (
            outcome := _exact_replace_check_sha(args.path, raw, args.expected_sha256)
        ) is not None:
            return outcome

        collected = _exact_replace_collect_spans(
            ctx.conversation_id, args.path, raw, text, args.edits, args.multi
        )
        if isinstance(collected, ToolOutcome):
            return collected
        spans, applied = collected

        if (outcome := _exact_replace_check_overlap(args.path, spans)) is not None:
            return outcome
        if (
            outcome := _exact_replace_check_fresh_edit(
                ctx.conversation_id, args.path, raw, text, spans
            )
        ) is not None:
            return outcome

        new_text = _exact_replace_splice(text, spans)
        if new_text == text:
            return _no_op_edit_refusal(
                ctx.conversation_id,
                args.path,
                base_content=(
                    f"exact_replace refused: the requested batch leaves {args.path} "
                    "byte-identical; nothing changed."
                ),
                current_bytes=raw,
                attempted_lines=None,
            )

        if (outcome := _exact_replace_check_syntax(args.path, text, new_text)) is not None:
            return outcome

        # (8) all checks passed → ONE atomic write (never write-then-revert). All-or-nothing.
        new_bytes = new_text.encode("utf-8")
        committed = await _commit_file_mutation(
            ctx,
            args.path,
            new_bytes,
            expected_before=raw,
        )
        if isinstance(committed, ToolOutcome):
            return committed
        return ToolOutcome(
            success=True,
            content=_updated_region_success_content(
                ctx.conversation_id,
                args.path,
                new_bytes,
                prefix=f"exact_replace applied {len(spans)} replacement(s) to {args.path}.",
                changed_lines=_post_change_line_span(text, new_text),
            ),
            artifacts=[args.path],
            structured=_write_artifact_structured(
                args.path,
                new_bytes,
                {"bytes": len(new_bytes), "applied": applied},
            ),
            effect_receipts=(committed,),
        )
