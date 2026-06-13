"""Dedicated file tools — tool-sandbox-contract.md §9 [OH: file_rules].

Dedicated file tools, NOT shell redirection — this sidesteps the string-escaping
failures of piping model output through bash. All operate within the sandbox
instance's jailed workspace (the instance rejects path escapes).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

_FS = frozenset({Capability.FILESYSTEM})

# A read result longer than the observation snip cap (events._OBS_SNIP_CHARS=8000)
# gets destructively snipped (head+tail) at context ingestion, leaving the model a
# corrupted middle — so it re-reads forever and never commits an edit (observed
# live). So file_read PAGES by a CHARACTER budget kept safely under that cap: each
# read returns intact, line-numbered lines + an explicit "read more with offset=…".
_READ_CHAR_BUDGET = 7_000

# A line-number prefix the model may have copied out of a numbered file_read
# ("  123\t<code>"). file_edit strips it defensively so a paste-back still matches.
_LINENO_PREFIX = re.compile(r"(?m)^\s*\d+\t")


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
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        start = max((args.offset or 1) - 1, 0)
        if start >= total and total > 0:
            return ToolOutcome(
                success=True,
                content=f"[lines {start + 1}-{total} of {total} — offset past end of file]",
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
        raw = args.content.encode("utf-8")
        await ctx.sandbox.write_file(args.path, raw)
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
        try:
            existing = await ctx.sandbox.read_file(args.path)
        except Exception:  # noqa: BLE001 — absent file → start empty
            existing = b""
        combined = existing + args.content.encode("utf-8")
        await ctx.sandbox.write_file(args.path, combined)
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
        await ctx.sandbox.write_file(args.path, updated.encode("utf-8"))
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
        await ctx.sandbox.write_file(args.path, out.encode("utf-8"))
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
        await ctx.sandbox.write_file(args.path, out.encode("utf-8"))
        return ToolOutcome(
            success=True,
            content=f"inserted {len(ins)} lines after line {args.after_line} of {args.path}",
            artifacts=[args.path],
        )
