"""Per-operation handlers for `RunProjectScriptTool.run`'s buffered batch.

Each handler mutates the shared `_ScriptState` and returns `ToolOutcome | None`
— `None` to continue the batch, an outcome to abort `run()` immediately with
it (mirroring the pre-extraction inline `return _fail(...)` from inside the
loop body). `_dispatch_op` is what `run()` calls per operation; it is the only
symbol here `run_script.py` needs directly.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from ...anatomy import ToolOutcome
from ...sandbox.base import SandboxFileNotFoundError
from ..files import _BINARY_DELIVERABLE_EXTS, _has_elision_marker
from .outcomes import _fail
from .state import _ScriptState

if TYPE_CHECKING:
    from ..run_script import RunScriptOp

_READ_RESULT_CAP = 4_000  # per-read content returned to the model (capped)


async def _handle_ls(state: _ScriptState, op: RunScriptOp) -> None:
    try:
        state.reads_out[op.path] = await state.sbx.list_dir(op.path)
    except (SandboxFileNotFoundError, FileNotFoundError):
        state.reads_out[op.path] = []


async def _handle_read(state: _ScriptState, op: RunScriptOp, canon: str) -> None:
    text = await state.load(canon, op.path)
    state.seen.add(canon)
    state.reads_out[op.path] = (text or "")[:_READ_RESULT_CAP]


async def _handle_replace_text(
    state: _ScriptState, i: int, op: RunScriptOp, canon: str
) -> ToolOutcome | None:
    if op.old is None or op.new is None:
        return _fail(i, "SCRIPT_OP_INVALID", "replace_text needs `old` and `new`.")
    if _has_elision_marker(op.old, op.new):
        return _fail(i, "ELISION_MARKER_REJECTED", "edit text contains an elision placeholder.")
    cur = await state.load(canon, op.path)
    if cur is None:
        return _fail(i, "SCRIPT_NO_MATCH", f"{op.path} does not exist.")
    if op.old not in cur:
        return _fail(i, "SCRIPT_NO_MATCH", f"`old` not found in {op.path}.")
    state.seen.add(canon)  # matched the file's REAL current content → grounded
    state.buffer[canon] = (
        cur.replace(op.old, op.new) if op.replace_all else cur.replace(op.old, op.new, 1)
    )
    state.mutated.add(canon)
    return None


def _validate_save_over_existing(
    i: int,
    op: RunScriptOp,
    canon: str,
    content: str,
    existed_text: str,
    raw: bytes,
    seen: set[str],
    rsw: set[str],
) -> ToolOutcome | None:
    """Guard a `save` over a file that already exists on disk: require grounding
    (a prior read/transform or a matching `expected_sha256`), refuse clobbering
    an existing binary deliverable, and refuse an ungrounded >50% shrink."""
    # EXISTING file → require grounding (no blind clobber of an unread file).
    grounded = (canon in seen) or (canon in rsw)
    if op.expected_sha256 is not None:
        if op.expected_sha256 != hashlib.sha256(raw).hexdigest():
            return _fail(i, "STALE_FILE_CONTEXT", f"{op.path} changed since you read it.")
        grounded = True
    if not grounded:
        return _fail(
            i,
            "FRESH_READ_REQUIRED",
            f"cannot save over the existing {op.path} without reading it first "
            "(add a 'read' op on it, transform it with replace_text, "
            "or pass expected_sha256).",
        )
    # extension from the REAL resolved path (canon), not the alias the model
    # typed — so alias.txt -> deck.pptx can't validate one ext + clobber another.
    ext = canon.rsplit(".", 1)[-1].lower() if "." in canon else ""
    if ext in _BINARY_DELIVERABLE_EXTS:
        return _fail(i, "binary_deliverable_clobber", f"{op.path} is an existing {ext} binary.")
    matching_sha = op.expected_sha256 is not None
    if len(content) < 0.5 * len(existed_text) and not op.allow_shrink and not matching_sha:
        return _fail(
            i,
            "SAFE_WRITE_SHRINK_REJECTED",
            f"save would shrink {op.path} from {len(existed_text)} "
            f"to {len(content)} chars "
            "(>50%); pass allow_shrink=true if intended.",
        )
    return None


async def _handle_save(
    state: _ScriptState, i: int, op: RunScriptOp, canon: str
) -> ToolOutcome | None:
    if op.content is None:
        return _fail(i, "SCRIPT_OP_INVALID", "save needs `content`.")
    if _has_elision_marker(op.content):
        return _fail(i, "ELISION_MARKER_REJECTED", "content contains an elision placeholder.")
    existed_text, raw = await state.disk_read(op.path)
    if canon not in state.original:
        state.original[canon] = existed_text
        state.original_bytes[canon] = raw if existed_text is not None else None
    if existed_text is not None:
        outcome = _validate_save_over_existing(
            i, op, canon, op.content, existed_text, raw, state.seen, state.rsw
        )
        if outcome is not None:
            return outcome
    state.buffer[canon] = op.content
    state.seen.add(canon)
    state.mutated.add(canon)
    return None


async def _dispatch_op(
    state: _ScriptState, i: int, op: RunScriptOp, canon: str
) -> ToolOutcome | None:
    """Route one operation to its handler; `None` means continue the batch."""
    if op.op == "ls":
        await _handle_ls(state, op)
        return None
    if op.op == "read":
        await _handle_read(state, op, canon)
        return None
    if op.op == "replace_text":
        return await _handle_replace_text(state, i, op, canon)
    return await _handle_save(state, i, op, canon)
