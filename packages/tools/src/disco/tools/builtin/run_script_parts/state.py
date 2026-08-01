"""`_ScriptState` — the REAL-path-keyed scratch buffer `RunProjectScriptTool.run`
builds once per call.

EVERYTHING is keyed by the REAL (symlink-followed) workspace-relative path so
an alias (a symlink or a `..` path) can never make the checks validate one
file while the commit writes another (codex CD-TOOLS-7 round-1). The same key
is used for the buffer, the grounding `seen` set, AND the atomic_write commit
path — they can never diverge.

Bundling the batch's mutable dicts/sets into one object (instead of passing
seven separate parameters to every op handler) is what lets `ops.py`'s
handlers be ordinary top-level functions instead of nested closures: a nested
closure would still count its lines against `run()`'s own logical-LOC budget,
since the AST-McCabe walk skips nested `def`s for complexity but the logical
line count is a plain line-range sum that does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...sandbox.base import SandboxFileNotFoundError
from ..files import _canonical


@dataclass
class _ScriptState:
    """One buffered batch's REAL-path-keyed working set."""

    sbx: Any
    buffer: dict[str, str] = field(default_factory=dict)  # REAL relpath -> mutated content
    original: dict[str, str | None] = field(default_factory=dict)  # -> disk text at first load
    original_bytes: dict[str, bytes | None] = field(default_factory=dict)
    mutated: set[str] = field(default_factory=set)  # REAL relpaths the buffer changed
    seen: set[str] = field(default_factory=set)  # GROUNDED (read or transformed this batch)
    reads_out: dict[str, Any] = field(default_factory=dict)
    rsw: set[str] = field(default_factory=set)  # read_since_write, from a prior call

    async def key(self, path: str) -> str:
        """The REAL (symlink-followed) workspace-relative path; raises
        SandboxPermissionError on a jail escape so the caller aborts with
        SCRIPT_PATH_ESCAPE."""
        rr = getattr(self.sbx, "resolve_relpath", None)
        if rr is not None:
            return str(await rr(path)).replace("\\", "/")
        return _canonical(path)

    async def disk_read(self, path: str) -> tuple[str | None, bytes]:
        """Return (text or None-if-absent, raw bytes)."""
        try:
            raw = await self.sbx.read_file(path)
        except (SandboxFileNotFoundError, FileNotFoundError):
            return None, b""
        return raw.decode("utf-8", errors="replace"), raw

    async def load(self, canon: str, path: str) -> str | None:
        if canon in self.buffer:
            return self.buffer[canon]
        text, raw = await self.disk_read(path)
        self.original[canon] = text
        self.original_bytes[canon] = raw if text is not None else None
        if text is not None:
            self.buffer[canon] = text
        return text
