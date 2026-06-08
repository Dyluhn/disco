"""Dedicated file tools — tool-sandbox-contract.md §9 [OH: file_rules].

Dedicated file tools, NOT shell redirection — this sidesteps the string-escaping
failures of piping model output through bash. All operate within the sandbox
instance's jailed workspace (the instance rejects path escapes).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

_FS = frozenset({Capability.FILESYSTEM})


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
            "Read a UTF-8 text file from the workspace. For large files, pass "
            "`offset` (1-based start line) and `limit` (line count) to read a "
            "slice — navigate by path + selective read rather than dumping the "
            "whole file into context."
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
        if args.offset is None and args.limit is None:
            return ToolOutcome(success=True, content=text)
        # Line-range slice. offset is 1-based; clamp to bounds.
        lines = text.splitlines()
        start = max((args.offset or 1) - 1, 0)
        end = start + args.limit if args.limit is not None else len(lines)
        sliced = lines[start:end]
        shown_to = start + len(sliced)
        note = (
            f"[lines {start + 1}-{shown_to} of {len(lines)}"
            + ("; more below — increase offset" if shown_to < len(lines) else "")
            + "]\n"
        )
        return ToolOutcome(success=True, content=note + "\n".join(sliced))


class FileWriteArgs(BaseModel):
    path: str = Field(description="Workspace-relative path to write.")
    content: str = Field(description="Full UTF-8 content to write.")


class FileWriteTool:
    definition = ToolDef(
        name="file_write",
        description="Write (create/overwrite) a UTF-8 text file in the workspace.",
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
    old: str = Field(description="Exact text to replace (first occurrence).")
    new: str = Field(description="Replacement text.")


class FileEditTool:
    definition = ToolDef(
        name="file_edit",
        description="Replace the first occurrence of `old` with `new` in a workspace file.",
        args_model=FileEditArgs,
        needs=_FS,
        runs_in="sandbox",
    )

    async def run(self, args: FileEditArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        text = (await ctx.sandbox.read_file(args.path)).decode("utf-8", errors="replace")
        if args.old not in text:
            return ToolOutcome(
                success=False,
                content=f"`old` text not found in {args.path}; no change made",
                error="old_text_not_found",
            )
        updated = text.replace(args.old, args.new, 1)
        await ctx.sandbox.write_file(args.path, updated.encode("utf-8"))
        return ToolOutcome(success=True, content=f"edited {args.path}", artifacts=[args.path])
