"""``file_read`` — the paginated, grounding-recording workspace file reader.

``FileReadTool.run`` is decomposed into: the not-found/decode-failure/offset-
past-end early outcomes, the F7 pressure-aware head-only gate + its outcome,
and the normal paged-read outcome. Each stays independently under the
per-callable complexity budget; `run` itself only sequences them.
"""

from __future__ import annotations

import hashlib

from disco.core.effects import (
    CoverageSpan,
    CoverageUnit,
    EffectCapability,
    ObservationReceipt,
    ResourceCoverage,
    ResourceKey,
    ResourceRevision,
    ToolBehavior,
)
from pydantic import BaseModel, Field

from ...anatomy import ToolContext, ToolDef, ToolOutcome
from ._canonical import _canonical
from ._constants import (
    _FS,
    _PRESSURE_DIRECTIVE,
    _PRESSURE_FILE_THRESHOLD,
    _PRESSURE_HEAD_BUDGET,
    _READ_CHAR_BUDGET,
)
from ._read_state import _conv_state, record_read


class FileReadArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to read.")
    # C-1 (filesystem-as-memory): line-range reads so a large file can be
    # navigated by path + selective read instead of dumped whole into context.
    offset: int | None = Field(
        default=None, description="1-based line to start at (omit to read from the top)."
    )
    limit: int | None = Field(
        default=None, description="Max number of lines to read (omit for the rest of the file)."
    )


def _file_read_receipt(
    path: str,
    *,
    sha256: str,
    total_lines: int,
    start_line_index: int,
    end_line_index: int,
    complete: bool,
    raw_size_bytes: int,
    rendered_content: str,
) -> ObservationReceipt:
    """Typed exact-coverage receipt for bytes physically returned by file_read."""

    rendered_bytes = rendered_content.encode("utf-8")
    spans = (
        (CoverageSpan(start=start_line_index, end=end_line_index),)
        if end_line_index > start_line_index
        else ()
    )
    return ObservationReceipt(
        capability=EffectCapability.WORKSPACE_CONTENT_READ,
        revision=ResourceRevision(
            resource=ResourceKey(
                namespace="workspace.file",
                identifier=_canonical(path),
            ),
            digest=sha256,
        ),
        coverage=ResourceCoverage(unit=CoverageUnit.LINES, spans=spans, total=total_lines),
        complete=complete,
        raw_size_bytes=raw_size_bytes,
        rendered_size_bytes=len(rendered_bytes),
        rendered_sha256=hashlib.sha256(rendered_bytes).hexdigest(),
    )


def _file_read_not_found_outcome(path: str) -> ToolOutcome:
    # Process and container sandboxes expose the same typed base here
    # (SandboxFileNotFoundError also subclasses FileNotFoundError).  Keep
    # the canonical error stable while putting concrete recovery guidance
    # in content, which the loop carries as AgentErrorEvent.detail.
    return ToolOutcome(
        success=False,
        error="file_not_found",
        content=(
            f"File read failed: {path!r} does not exist in the current "
            "workspace. Use file_list on its parent to inspect current paths; "
            "if the file is required, create it before reading it."
        ),
    )


def _file_read_invalid_utf8_outcome(path: str) -> ToolOutcome:
    return ToolOutcome(
        success=False,
        error="invalid_utf8_text",
        content=(
            f"{path} is not valid UTF-8 text. No replacement-decoded content "
            "was returned because it would silently change the bytes. The file "
            "remains untouched; use a binary-safe asset workflow or replace it only "
            "with deliberate source bytes."
        ),
    )


def _file_read_offset_past_end_outcome(start: int, total: int) -> ToolOutcome:
    # An offset PAST end-of-file shows NO content — it must NOT grant grounding
    # (read_since_write) or the CD-TOOLS-1/2 fresh-edit guard could be bypassed by a
    # `file_read(offset=huge)` that read nothing (Codex CD-TOOLS-2 round-2). A prior real
    # read's grounding is untouched (we simply don't ADD here).
    return ToolOutcome(
        success=True,
        content=f"[lines {start + 1}-{total} of {total} — offset past end of file]",
    )


def _file_read_pressure_gate(ctx: ToolContext, args: FileReadArgs, text: str) -> bool:
    # F7 — pressure-aware head-only. Fires ONLY when:
    #   - ctx.assist is on (the weak-model tier the gate exists to protect),
    #   - the caller did NOT pass an explicit offset/limit (a targeted read
    #     is already cheap; the gate would just break a working flow), and
    #   - the file is large enough that today's paging would have to split
    #     it across multiple pages (the size proxy for "this is going to
    #     cost real context budget"). When all three hold, return a small
    #     head slice + a directive telling the model to grep, then read a
    #     line range — not the full page. The assist-OFF branch and the
    #     no-pressure (small file / explicit range) branch are untouched:
    #     they fall through to today's paging below.
    return (
        ctx.assist
        and args.offset is None
        and args.limit is None
        and len(text) > _PRESSURE_FILE_THRESHOLD
    )


def _file_read_pressure_outcome(
    ctx: ToolContext,
    args: FileReadArgs,
    *,
    text: str,
    lines: list[str],
    total: int,
    disk_sha: str,
    data: bytes,
) -> ToolOutcome:
    # Number a HEAD slice kept under the pressure head budget. Same
    # width/numbering convention as the regular page so a follow-up
    # file_read(offset=K+1, limit=N) is byte-consistent.
    width = len(str(total)) or 1
    head: list[str] = []
    used = 0
    for i, ln in enumerate(lines):
        line = f"{i + 1:>{width}}\t{ln}"
        if head and used + len(line) + 1 > _PRESSURE_HEAD_BUDGET:
            break
        head.append(line)
        used += len(line) + 1
    head_to = len(head)
    record_read(
        ctx.conversation_id,
        args.path,
        sha=disk_sha,
        start_line=1,
        end_line=max(head_to, 1),
        full=(head_to >= total),
    )
    header = (
        f"[lines 1-{head_to} of {total} (file: {len(text)} chars) — "
        f"HEAD-ONLY under context pressure]\n"
    )
    content = header + "\n".join(head) + "\n\n" + _PRESSURE_DIRECTIVE
    return ToolOutcome(
        success=True,
        content=content,
        effect_receipts=(
            _file_read_receipt(
                args.path,
                sha256=disk_sha,
                total_lines=total,
                start_line_index=0,
                end_line_index=head_to,
                complete=head_to >= total,
                raw_size_bytes=len(data),
                rendered_content=content,
            ),
        ),
    )


def _file_read_page_outcome(
    ctx: ToolContext,
    args: FileReadArgs,
    *,
    text: str,
    lines: list[str],
    total: int,
    start: int,
    disk_sha: str,
    data: bytes,
) -> ToolOutcome:
    # A page that fits under the snip cap, line-numbered from the absolute start
    # so line numbers are correct. If `limit` is given, respect it but still cap
    # by chars so a huge limit can't corrupt the result.
    #
    # CW-6: the page budget is capability-derived. The runtime stamps
    # ctx.read_char_budget from derive_context_caps(assist, driver_context_window)
    # so an assist-OFF (capable) model reads a file that fits the snapshot pin in
    # ONE shot — and the matching _OBS_SNIP_CHARS override keeps that large
    # observation from being render-snipped to a corrupted head/tail. Unset
    # (assist-ON / executors that don't thread it) ⇒ the static 7k default.
    read_budget = ctx.read_char_budget or _READ_CHAR_BUDGET
    # CW P1-b (round-2): assist-OFF budgets the page on RAW file chars (the SAME unit
    # the snapshot pin uses), so a file whose RAW size fits the per-file pin reads in
    # ONE shot; line numbers are applied to the selected slice AFTER this check, and
    # their render overhead is absorbed by the assist-OFF observation-snip cap (raised
    # in tandem — see context_budget). assist-ON keeps the obs-snip at the 8k baseline,
    # so its page must stay under it: that tier keeps charging the LINE-NUMBERED render
    # (byte-identical to today) so a short-line page can't render past the snip cap.
    budget_raw = not ctx.assist
    out: list[str] = []
    width = len(str(total)) or 1
    used = 0
    i = start
    cap = start + args.limit if args.limit is not None else total
    # The raw charge must equal the file's true byte size: the FINAL line carries no
    # trailing newline unless `text` ends in one, so charging +1 for it would
    # over-count by 1 and page a file that exactly fits the pin (codex round-3).
    ends_nl = text.endswith("\n")
    while i < min(cap, total):
        line = f"{i + 1:>{width}}\t{lines[i]}"
        raw_nl = 1 if (i < total - 1 or ends_nl) else 0
        charge = (len(lines[i]) + raw_nl) if budget_raw else (len(line) + 1)
        if out and used + charge > read_budget:
            break
        out.append(line)
        used += charge
        i += 1
    shown_to = i
    # CD-TOOLS-1: record the lines the model saw un-elided. full iff this single page
    # covered the WHOLE file (from line 1 to the last line).
    record_read(
        ctx.conversation_id,
        args.path,
        sha=disk_sha,
        start_line=start + 1,
        end_line=max(shown_to, start + 1),
        full=(start == 0 and shown_to >= total),
    )
    # there's more file to read below if we didn't reach the end (whether we
    # stopped on the char budget or the caller's limit)
    more = f"; read more with offset={shown_to + 1}" if shown_to < total else ""
    header = f"[lines {start + 1}-{shown_to} of {total}{more}]\n"
    content = header + "\n".join(out)
    return ToolOutcome(
        success=True,
        content=content,
        effect_receipts=(
            _file_read_receipt(
                args.path,
                sha256=disk_sha,
                total_lines=total,
                start_line_index=start,
                end_line_index=shown_to,
                complete=start == 0 and shown_to >= total,
                raw_size_bytes=len(data),
                rendered_content=content,
            ),
        ),
    )


class FileReadTool:
    definition = ToolDef(
        name="file_read",
        description=(
            "Read a UTF-8 text file from the workspace, with 1-based LINE NUMBERS. "
            "Files under the source-size cap fit in ONE whole read — omit "
            "offset/limit by default; slice only genuinely large files. Prefer "
            "`file_edit` (pass the exact text you see "
            "as `old`) for targeted changes; the line numbers also let you target "
            "`file_replace_lines`, but re-read the RANGE you are about to edit right "
            "before a line edit (numbers shift after every change)."
        ),
        args_model=FileReadArgs,
        needs=_FS,
        runs_in="sandbox",
        read_only=True,  # observes only — safe for the planner
        behavior=ToolBehavior(
            planner_safe=True,
            possible_capabilities=frozenset({EffectCapability.WORKSPACE_CONTENT_READ}),
        ),
    )

    async def run(self, args: FileReadArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None  # sandbox tools always receive an instance
        try:
            data = await ctx.sandbox.read_file(args.path)
        except FileNotFoundError:
            return _file_read_not_found_outcome(args.path)
        # F1 — set the read-since-write bit for this path so a subsequent
        # file_write is allowed (the happy path: read → write). Successful
        # mutators also set this via their returned numbered observations.
        # Internal ctx.sandbox.read_file() calls inside _gated_write and each
        # mutator's own preflight read do NOT set the bit by themselves.
        disk_sha = hashlib.sha256(data).hexdigest()  # CD-TOOLS-1 fresh-edit grounding
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return _file_read_invalid_utf8_outcome(args.path)
        lines = text.splitlines()
        total = len(lines)
        start = max((args.offset or 1) - 1, 0)
        if start >= total and total > 0:
            return _file_read_offset_past_end_outcome(start, total)
        # F1 — grant the read-since-write grounding bit ONLY now that we know content WILL be
        # shown (the branches below all render real lines, incl. the empty-file fall-through).
        # Set before file_write's read-before-write gate AND the CD-TOOLS-1 edit guard rely on it.
        _conv_state(ctx.conversation_id)["read_since_write"].add(_canonical(args.path))
        if _file_read_pressure_gate(ctx, args, text):
            return _file_read_pressure_outcome(
                ctx, args, text=text, lines=lines, total=total, disk_sha=disk_sha, data=data
            )
        return _file_read_page_outcome(
            ctx,
            args,
            text=text,
            lines=lines,
            total=total,
            start=start,
            disk_sha=disk_sha,
            data=data,
        )
