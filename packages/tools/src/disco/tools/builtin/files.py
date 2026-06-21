"""Dedicated file tools — tool-sandbox-contract.md §9 [OH: file_rules].

Dedicated file tools, NOT shell redirection — this sidesteps the string-escaping
failures of piping model output through bash. All operate within the sandbox
instance's jailed workspace (the instance rejects path escapes).
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..sandbox.base import strip_redundant_workspace_prefix

_FS = frozenset({Capability.FILESYSTEM})

# A read result longer than the observation snip cap (events._OBS_SNIP_CHARS=8000)
# gets destructively snipped (head+tail) at context ingestion, leaving the model a
# corrupted middle — so it re-reads forever and never commits an edit (observed
# live). So file_read PAGES by a CHARACTER budget kept safely under that cap: each
# read returns intact, line-numbered lines + an explicit "read more with offset=…".
_READ_CHAR_BUDGET = 7_000

# F7 — pressure-aware head-only.
#
# LIMITATION: ToolContext exposes `assist` but not the condenser's token_count
# pressure signal (that lives in engine.py / view.py and isn't threaded into the
# tool call). So we gate on `assist=True` + a LARGE-FILE SIZE HEURISTIC: files
# whose total content exceeds twice the read char budget are the ones that
# today's paging has to split across multiple pages anyway — exactly the
# payload size that gets most-corrupted by the snip pass and most-likely to
# push the model into the next condenser tick. For those, we return only a
# small HEAD slice and a prescriptive directive (grep, then file_read a
# targeted line range) so the model stops dumping whole files into context
# when it's already close to the limit. Assist-OFF behavior is unchanged.
_PRESSURE_HEAD_BUDGET = 2_000  # head-only slice when the gate fires (well under the snip cap)
# 14_000 — files that would otherwise span multiple pages
_PRESSURE_FILE_THRESHOLD = _READ_CHAR_BUDGET * 2
# Prescriptive directive: "the file is big; don't read it whole, do this instead."
# Static so a unit test can assert on it; worded so the weak-model tier picks
# the cheap, deterministic path (grep → targeted read) over a full dump.
_PRESSURE_DIRECTIVE = (
    "file is large; the full page is suppressed. Do NOT re-read this file "
    "whole — pick the symbol/class/section you need, then either:\n"
    "  (a) run a grep/ripgrep tool to locate it (e.g. `rg -n 'Symbol' <path>`), "
    "then file_read a targeted line range (`offset=<n>, limit=<m>`); or\n"
    "  (b) file_read a small line range directly to scan structure.\n"
    "Reading this file whole again will just be truncated the same way."
)

# A line-number prefix the model may have copied out of a numbered file_read
# ("  123\t<code>"). file_edit strips it defensively so a paste-back still matches.
_LINENO_PREFIX = re.compile(r"(?m)^\s*\d+\t")

# Per-conversation read-before-rewrite tracker (F1).
# Structure: {conv_id: {"read_since_write": set[str]}}
#   read_since_write: canonical paths (workspace-prefix-stripped) for which a
#     successful FileReadTool.run has occurred since the path's last successful
#     mutation (file_write / file_append / file_edit / file_replace_lines /
#     file_insert_lines / file_str_replace) in this conversation.
#     A write to a path that (a) EXISTS on disk AND (b) is NOT in this set is
#     REFUSED — the model must file_read the file first.
#     A NEW file (does not exist yet) is always allowed.
# Module-level so it's per-process; the per-conversation key keeps state
# isolated between agents/sessions. Applies to ALL model tiers (not gated on
# ctx.assist) — the thrash root-cause hits capable models too.
_read_state: dict[str, dict[str, set[str]]] = {}


def reset_read_tracker() -> None:
    """Clear all per-conversation read-since-write state. Tests only; not part
    of the tool API."""
    _read_state.clear()


def clear_conversation_read_state(conv_id: str) -> None:
    """Remove the F1 tracker entry for a single conversation.

    Called by DefaultToolExecutor.kill() so the module-level dict does not
    grow unbounded in long-running processes (each killed executor cleans up
    its own per-conversation bucket).  The per-conversation-id key design
    already prevents cross-conversation leaks; this call prevents the dict
    from accumulating dead entries indefinitely.

    Tests should use reset_read_tracker() for a full clear between runs."""
    _read_state.pop(conv_id, None)


def _conv_state(conv_id: str) -> dict[str, set[str]]:
    s = _read_state.get(conv_id)
    if s is None:
        s = {"read_since_write": set()}
        _read_state[conv_id] = s
    return s


def _canonical(path: str) -> str:
    """Canonical tracker key: strip leading workspace prefixes so 'workspace/foo'
    and '/workspace/foo' collapse to the same entry as 'foo'. One level only —
    'src/workspace/x' is untouched."""
    return strip_redundant_workspace_prefix(path)


def _number_lines(text: str, start: int = 1) -> str:
    """Render text with right-aligned 1-based line numbers + a tab, so the model
    can target precise ranges with file_replace_lines / file_insert_lines — the
    robust way to edit a large file without reproducing its exact bytes."""
    lines = text.splitlines()
    if not lines:
        return ""
    width = len(str(start + len(lines) - 1))
    return "\n".join(f"{start + i:>{width}}\t{ln}" for i, ln in enumerate(lines))


def _strip_line_numbers(s: str) -> str:
    """Remove accidental `N\\t` line-number prefixes the model copied from a read."""
    return _LINENO_PREFIX.sub("", s)


def _norm_ws(s: str) -> str:
    """Whitespace-normalized form for forgiving matching: strip BOTH ends of each
    line + drop blank leading/trailing lines. Tolerates the #1 cause of failed exact
    matches — indentation and trailing-space drift — which small models get wrong
    constantly. The replacement still uses the caller's `new` verbatim, so the edit's
    own indentation is whatever the model intended."""
    return "\n".join(ln.strip() for ln in s.strip("\n").splitlines())


# ---------------------------------------------------------------------------
# W3 — syntax gate helpers
# ---------------------------------------------------------------------------


def _syntax_errors(path: str, text: str) -> list[str]:
    """Return error-kind tokens for `text` parsed as `path`'s type.

    Returns one string per distinct error kind (e.g. "SyntaxError",
    "JSONDecodeError"). Line numbers and messages are deliberately EXCLUDED
    from the returned strings so the diff-filter (`introduced = post - pre`)
    is stable across whole-file rewrites: a pre-existing SyntaxError at line
    5 stays "SyntaxError" regardless of whether the rewrite moves it to line 1.
    This prevents false positives where a pre-existing messy file is punished
    every time it is written.

    Supported: .py (compile), .json (json.loads).
    Unsupported (tree-sitter not installed): .html/.css/.js/.ts/.tsx/.jsx
    — those return [] so unsupported-format files are never blocked.
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext == "py":
        try:
            compile(text, path, "exec")
        except SyntaxError:
            return ["SyntaxError"]
        return []
    if ext == "json":
        try:
            json.loads(text)
        except json.JSONDecodeError:
            return ["JSONDecodeError"]
        return []
    # tree-sitter unavailable: html/css/js/ts/tsx/jsx/yaml/yml unsupported → []
    return []


async def _gated_write(
    ctx: ToolContext,
    path: str,
    new_bytes: bytes,
    old_text: str | None,
) -> ToolOutcome | None:
    """Write new_bytes to path; auto-revert to old_text if new content introduces
    syntax errors that were not already present (W3 diff-filter).

    Returns a failure ToolOutcome when new errors are introduced, None otherwise.
    Callers proceed to their own success outcome when None is returned.
    The diff-filter compares error-KIND tokens (not messages/line numbers) so
    pre-existing messy files aren't punished by whole-file rewrites that shift
    line numbers without changing the nature of the breakage.
    """
    assert ctx.sandbox is not None
    new_text = new_bytes.decode("utf-8", errors="replace")
    pre = _syntax_errors(path, old_text) if old_text is not None else []
    post = _syntax_errors(path, new_text)
    introduced = [e for e in post if e not in pre]
    await ctx.sandbox.write_file(path, new_bytes)
    if not introduced:
        return None
    if old_text is not None:
        # AUTO-REVERT: restore previous content so the workspace stays consistent.
        await ctx.sandbox.write_file(path, old_text.encode("utf-8"))
        kept = "it was NOT applied (previous content kept)"
    else:
        kept = "it was applied (new file; no prior content to revert)"
    return ToolOutcome(
        success=False,
        error="syntax_gate_reverted",
        content=(
            f"Your edit to {path} introduced syntax error(s); {kept}: "
            f"{'; '.join(introduced)}. "
            "Fix the snippet and try a DIFFERENT edit. "
            "DO NOT re-run the same failed edit — it will fail identically."
        ),
    )


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


class FileReadTool:
    definition = ToolDef(
        name="file_read",
        description=(
            "Read a UTF-8 text file from the workspace, with 1-based LINE NUMBERS. "
            "For large files pass `offset` (1-based start line) + `limit` (line "
            "count) to read a slice. Prefer `file_edit` (pass the exact text you see "
            "as `old`) for targeted changes; the line numbers also let you target "
            "`file_replace_lines`, but re-read right before each line edit since they "
            "shift after every change."
        ),
        args_model=FileReadArgs,
        needs=_FS,
        runs_in="sandbox",
        read_only=True,  # observes only — safe for the planner
    )

    async def run(self, args: FileReadArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None  # sandbox tools always receive an instance
        data = await ctx.sandbox.read_file(args.path)
        # F1 — set the read-since-write bit for this path so a subsequent
        # file_write is allowed (the happy path: read → write). Applies to ALL
        # tiers (not gated on ctx.assist) — the thrash root-cause hits capable
        # models too, and the guard must be symmetric. The internal
        # ctx.sandbox.read_file() calls inside _gated_write and each mutator's
        # own read do NOT go through this method, so they do NOT set the bit —
        # only an explicit model-issued file_read counts as grounding evidence.
        _conv_state(ctx.conversation_id)["read_since_write"].add(_canonical(args.path))
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        start = max((args.offset or 1) - 1, 0)
        if start >= total and total > 0:
            return ToolOutcome(
                success=True,
                content=f"[lines {start + 1}-{total} of {total} — offset past end of file]",
            )
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
        if (
            ctx.assist
            and args.offset is None
            and args.limit is None
            and len(text) > _PRESSURE_FILE_THRESHOLD
        ):
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
            header = (
                f"[lines 1-{head_to} of {total} (file: {len(text)} chars) — "
                f"HEAD-ONLY under context pressure]\n"
            )
            return ToolOutcome(
                success=True,
                content=header + "\n".join(head) + "\n\n" + _PRESSURE_DIRECTIVE,
            )
        # A page that fits under the snip cap, line-numbered from the absolute start
        # so line numbers are correct. If `limit` is given, respect it but still cap
        # by chars so a huge limit can't corrupt the result.
        out: list[str] = []
        width = len(str(total)) or 1
        used = 0
        i = start
        cap = start + args.limit if args.limit is not None else total
        while i < min(cap, total):
            line = f"{i + 1:>{width}}\t{lines[i]}"
            if out and used + len(line) + 1 > _READ_CHAR_BUDGET:
                break
            out.append(line)
            used += len(line) + 1
            i += 1
        shown_to = i
        # there's more file to read below if we didn't reach the end (whether we
        # stopped on the char budget or the caller's limit)
        more = f"; read more with offset={shown_to + 1}" if shown_to < total else ""
        header = f"[lines {start + 1}-{shown_to} of {total}{more}]\n"
        return ToolOutcome(success=True, content=header + "\n".join(out))


class FileWriteArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to write.")
    content: str = Field(description="Full UTF-8 content to write.")


class FileWriteTool:
    definition = ToolDef(
        name="file_write",
        description=(
            "The PREFERRED way to author file content: write (create/overwrite) a "
            "UTF-8 text file in the workspace with its full content. Use this instead "
            "of shell redirection. To change part of an existing file, use file_edit."
        ),
        args_model=FileWriteArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: FileWriteArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        # Read existing content once — used by both the F1 guard and the W3 syntax gate.
        old_text: str | None = None
        try:
            old_text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — absent file is fine, that just means "new"
            old_text = None
        # F1 — read-before-rewrite guard (ALL tiers, no assist gate). Refuse a
        # file_write to an EXISTING file if there has been no successful
        # file_read of it since the path's last successful mutation in this
        # conversation. A NEW (nonexistent) file is always allowed — there is no
        # prior content to ground on. The old F3 "second untracked write forces
        # replace" escape hatch is REMOVED; the only way past this refusal is an
        # actual file_read (which sets the read-since-write bit), or using a
        # targeted edit tool (file_replace_lines / file_insert_lines / file_edit)
        # which does not require a full-rewrite guard because it operates on
        # specific lines anchored to the current content.
        if old_text is not None:  # file exists (we read it above)
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
        # W3 — syntax gate: write new bytes; auto-revert if new content introduces errors.
        raw = args.content.encode("utf-8")
        gated = await _gated_write(ctx, args.path, raw, old_text)
        if gated is not None:
            return gated
        # F1 — clear the read-since-write bit: the file has been mutated, so the
        # next file_write must be preceded by another file_read.
        _conv_state(ctx.conversation_id)["read_since_write"].discard(_canonical(args.path))
        return ToolOutcome(
            success=True, content=f"wrote {len(raw)} bytes to {args.path}", artifacts=[args.path]
        )


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
            "characters."
        ),
        args_model=FileAppendArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: FileAppendArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        old_text: str | None = None
        existing = b""
        try:
            existing = await ctx.sandbox.read_file(args.path)
            old_text = existing.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — absent file → start empty
            existing = b""
            old_text = None
        combined = existing + args.content.encode("utf-8")
        # W3 — syntax gate: write combined; auto-revert to old if errors introduced.
        gated = await _gated_write(ctx, args.path, combined, old_text)
        if gated is not None:
            return gated
        # F1 — file_append is a successful mutation: clear the read-since-write bit.
        _conv_state(ctx.conversation_id)["read_since_write"].discard(_canonical(args.path))
        return ToolOutcome(
            success=True,
            content=f"appended {len(args.content.encode('utf-8'))} bytes to {args.path}",
            artifacts=[args.path],
        )


class FileListArgs(BaseModel):
    path: str = Field(default=".", description="Workspace-relative directory to list.")


class FileListTool:
    definition = ToolDef(
        name="file_list",
        description="List the entries of a directory in the workspace.",
        args_model=FileListArgs,
        needs=_FS,
        runs_in="sandbox",
        read_only=True,  # observes only — safe for the planner
    )

    async def run(self, args: FileListArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        entries = await ctx.sandbox.list_dir(args.path)
        return ToolOutcome(
            success=True,
            content="\n".join(entries) if entries else "(empty)",
            structured={"path": args.path, "entries": entries},
        )


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
                updated = "".join(doc[:i]) + new + ("" if new.endswith("\n") else "\n") + "".join(
                    doc[i + len(tgt) :]
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
            "snippet as `old`."
        ),
        args_model=FileEditArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: FileEditArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        # Intent no-op: old and new are LITERALLY identical (after stripping any
        # line-number prefixes the model copied). This is distinct from a
        # whitespace-only edit (old≠new, which must apply) — here the model asked
        # for no change at all, so refuse before touching the file regardless of how
        # the file's own whitespace happens to differ. The ground-truth guard below
        # still catches the "applied result is unchanged" case.
        if _strip_line_numbers(args.old) == _strip_line_numbers(args.new):
            return ToolOutcome(
                success=False,
                content=(
                    f"file_edit refused: `old` and `new` are identical — this asks for "
                    f"no change to {args.path}. If you already applied this edit, move "
                    "on; otherwise give the NEW content you want."
                ),
                error="no_op_edit",
            )
        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
        updated, how = _forgiving_replace(text, args.old, _strip_line_numbers(args.new))
        if updated is None:
            return ToolOutcome(
                success=False,
                content=(
                    f"`old` not found in {args.path} (tried exact + whitespace-tolerant)."
                    + _nearest_anchor(text, args.old)
                    + " Tip: read the file for line numbers, then use file_replace_lines."
                ),
                error="old_text_not_found",
            )
        # No-op guard, on GROUND TRUTH: refuse only when the replacement leaves the
        # file byte-identical. Checking the *applied* result (not an abstract
        # old-vs-new compare) lets a legitimate whitespace-only edit through — a
        # re-indent or trailing-space cleanup matches ws-tolerantly but writes
        # `new` verbatim, so `updated != text` and it applies — while still catching
        # the real no-op: an already-applied edit (or old≈new) that changes nothing.
        if updated == text:
            return ToolOutcome(
                success=False,
                content=(
                    f"file_edit refused: this edit leaves {args.path} unchanged — the "
                    "new content already matches what's on disk (it may have been "
                    "applied on an earlier turn). No further action is needed; move on."
                ),
                error="no_op_edit",
            )
        # W3 — syntax gate: write updated; auto-revert to text if errors introduced.
        gated = await _gated_write(ctx, args.path, updated.encode("utf-8"), text)
        if gated is not None:
            return gated
        # F1 — file_edit is a successful mutation: clear the read-since-write bit.
        _conv_state(ctx.conversation_id)["read_since_write"].discard(_canonical(args.path))
        return ToolOutcome(
            success=True, content=f"edited {args.path} ({how})", artifacts=[args.path]
        )


class FileReplaceLinesArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to edit.")
    start_line: int = Field(description="First line to replace (1-based, inclusive).")
    end_line: int = Field(description="Last line to replace (1-based, inclusive).")
    new_text: str = Field(description="Replacement text for that line range (can be multi-line).")


class FileReplaceLinesTool:
    """Surgical, large-file-friendly edit: replace an inclusive 1-based LINE RANGE
    with new text. The model reads the numbered file, picks the range, and writes
    the replacement — no need to reproduce the old bytes. Works for any model/size."""

    definition = ToolDef(
        name="file_replace_lines",
        description=(
            "Replace lines [start_line, end_line] (1-based, inclusive) of a file with "
            "`new_text`. Good for large files where reproducing exact text is hard. "
            "IMPORTANT: re-read the file IMMEDIATELY before each call — line numbers "
            "shift after any edit, and a stale range silently overwrites the wrong lines. "
            "Use file_insert_lines to insert without replacing."
        ),
        args_model=FileReplaceLinesArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: FileReplaceLinesArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
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
        # W3 — syntax gate: write result; auto-revert to text if errors introduced.
        gated = await _gated_write(ctx, args.path, out.encode("utf-8"), text)
        if gated is not None:
            return gated
        # F1 — file_replace_lines is a successful mutation: clear the read-since-write bit.
        _conv_state(ctx.conversation_id)["read_since_write"].discard(_canonical(args.path))
        replaced = max(0, end - args.start_line + 1)
        return ToolOutcome(
            success=True,
            content=f"replaced lines {args.start_line}-{end} of {args.path} "
            f"({replaced}→{len(new_lines)} lines)",
            artifacts=[args.path],
        )


class FileInsertLinesArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to edit.")
    after_line: int = Field(
        description="Insert AFTER this 1-based line (0 = at the very top of the file)."
    )
    text: str = Field(description="Text to insert (can be multi-line).")


class FileInsertLinesTool:
    """Insert text after a given line WITHOUT replacing anything — the clean way to
    ADD a block (a new section/app) to a large file by line number."""

    definition = ToolDef(
        name="file_insert_lines",
        description=(
            "Insert `text` AFTER line `after_line` (1-based; 0 = top) of a file, without "
            "replacing anything. Read the file for line numbers first. The reliable way "
            "to ADD a block to a large file."
        ),
        args_model=FileInsertLinesArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: FileInsertLinesArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
        lines = text.splitlines()
        n = len(lines)
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
        # W3 — syntax gate: write result; auto-revert to text if errors introduced.
        gated = await _gated_write(ctx, args.path, out.encode("utf-8"), text)
        if gated is not None:
            return gated
        # F1 — file_insert_lines is a successful mutation: clear the read-since-write bit.
        _conv_state(ctx.conversation_id)["read_since_write"].discard(_canonical(args.path))
        return ToolOutcome(
            success=True,
            content=f"inserted {len(ins)} lines after line {args.after_line} of {args.path}",
            artifacts=[args.path],
        )


# ---------------------------------------------------------------------------
# W4 — capability-gated anchored str-replace (offered to ANCHORED_EDIT models)
# ---------------------------------------------------------------------------


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
    old_str: str = Field(
        description="Exact text to find (must appear EXACTLY ONCE in the file)."
    )
    new_str: str = Field(description="Replacement text.")


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
    )

    async def run(self, args: FileStrReplaceArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")

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
            # Whitespace-strip retry: one chance with leading/trailing whitespace removed.
            stripped = args.old_str.strip()
            if stripped and stripped != args.old_str:
                retry_count = text.count(stripped)
                if retry_count == 1:
                    new_text = text.replace(stripped, args.new_str, 1)
                    gated = await _gated_write(ctx, args.path, new_text.encode("utf-8"), text)
                    if gated is not None:
                        return gated
                    # F1 — successful mutation: clear the read-since-write bit.
                    _conv_state(ctx.conversation_id)["read_since_write"].discard(
                        _canonical(args.path)
                    )
                    return ToolOutcome(
                        success=True,
                        content=f"replaced in {args.path} (whitespace-stripped match)",
                        artifacts=[args.path],
                    )
            return ToolOutcome(
                success=False,
                error="old_str_not_found",
                content=f"`old_str` did not appear verbatim in {args.path}.",
            )

        # Exactly one occurrence — apply and pass through W3 gate.
        new_text = text.replace(args.old_str, args.new_str, 1)
        gated = await _gated_write(ctx, args.path, new_text.encode("utf-8"), text)
        if gated is not None:
            return gated
        # F1 — successful mutation: clear the read-since-write bit.
        _conv_state(ctx.conversation_id)["read_since_write"].discard(_canonical(args.path))
        return ToolOutcome(
            success=True,
            content=f"replaced in {args.path}",
            artifacts=[args.path],
        )
