"""Commit primitives shared by every mutating file tool: the exact-byte mutation
receipt, the optimistic-concurrency commit/delete, the W3 syntax gate, and the
best-effort atomic write."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from disco.core.effects import MutationReceipt, ResourceKey, ResourceRevision

from ...anatomy import ToolContext, ToolOutcome
from ._canonical import _canonical


def _write_artifact_structured(
    path: str,
    post_write_bytes: bytes,
    structured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """REL-2a step3: durable artifact evidence for generic file mutators."""
    out = dict(structured or {})
    out["path"] = _canonical(path)
    out["sha256"] = hashlib.sha256(post_write_bytes).hexdigest()
    return out


def _workspace_file_revision(path: str, content: bytes) -> ResourceRevision:
    """Return the exact byte revision for one canonical workspace file."""
    resource = ResourceKey(namespace="workspace.file", identifier=_canonical(path))
    return ResourceRevision(resource=resource, digest=hashlib.sha256(content).hexdigest())


def _file_mutation_receipt(
    path: str,
    *,
    before: bytes | None,
    after: bytes | None,
) -> MutationReceipt:
    """Bind a successful deterministic file mutation to its exact byte transition.

    ``None`` means the resource was absent. Empty files remain real revisions, so
    callers must preserve the absence sentinel instead of relying on truthiness.
    """
    resource = ResourceKey(namespace="workspace.file", identifier=_canonical(path))
    return MutationReceipt(
        resource=resource,
        before=_workspace_file_revision(path, before) if before is not None else None,
        after=_workspace_file_revision(path, after) if after is not None else None,
        after_size_bytes=len(after) if after is not None else None,
    )


async def _resolved_workspace_file(sandbox: Any, path: str) -> str:
    """Resolve aliases in the sandbox's namespace before naming an exact receipt."""
    resolver = getattr(sandbox, "resolve_relpath", None)
    if resolver is None:
        return _canonical(path)
    return _canonical(await resolver(path))


async def _atomic_write(sandbox: Any, path: str, data: bytes) -> None:
    """CD-TOOLS-3: commit `data` to `path` atomically where the backend supports it (a sibling
    tmp + os.replace — ProcessSandbox.atomic_write), else use one best-effort ``write_file``.
    In-memory prevalidation prevents logic failures before dispatch; it does not manufacture
    filesystem atomicity in a compatibility backend that lacks an atomic primitive.
    """
    aw = getattr(sandbox, "atomic_write", None)
    if aw is None:
        await sandbox.write_file(path, data)
    else:
        await aw(path, data)


async def _commit_file_mutation(
    ctx: ToolContext,
    path: str,
    new_bytes: bytes,
    *,
    expected_before: bytes | None,
) -> MutationReceipt | ToolOutcome:
    """Optimistically compare current bytes, then invoke the backend commit primitive.

    The second read is intentionally adjacent to the backend replacement:
    a change since the mutator computed ``new_bytes`` fails as stale context instead
    of being silently clobbered or attributed to the older revision. The resolved
    path is used for both the commit and receipt, so an in-workspace alias cannot
    split one resource into two identities. This is not a portable filesystem CAS;
    the receipt captures the host-observed expectation and backend-acknowledged bytes,
    while the final workspace seal proves the delivered state.
    """
    assert ctx.sandbox is not None
    resolved = await _resolved_workspace_file(ctx.sandbox, path)
    try:
        current: bytes | None = await ctx.sandbox.read_file(resolved)
    except FileNotFoundError:
        current = None
    if current != expected_before:
        expected = (
            "absent"
            if expected_before is None
            else hashlib.sha256(expected_before).hexdigest()[:12] + "…"
        )
        actual = "absent" if current is None else hashlib.sha256(current).hexdigest()[:12] + "…"
        return ToolOutcome(
            success=False,
            error="STALE_FILE_CONTEXT",
            content=(
                f"Write refused — {path} changed while this edit was being prepared "
                f"(expected {expected}, now {actual}). Nothing was written. Read the "
                "current file, then re-apply the intended change to that revision."
            ),
            structured={
                "kind": "stale_file_context",
                "path": resolved,
                "next_required_action": "file_read",
                "suggested_args": {"path": resolved},
            },
        )
    # Build and validate the receipt before entering the mutation boundary. A
    # successful backend call acknowledges new_bytes; the final seal independently
    # proves which bytes were ultimately delivered.
    receipt = _file_mutation_receipt(
        resolved,
        before=current,
        after=new_bytes,
    )
    await _atomic_write(ctx.sandbox, resolved, new_bytes)
    return receipt


async def _commit_file_deletion(
    ctx: ToolContext,
    path: str,
    *,
    expected_before: bytes | None,
) -> MutationReceipt | ToolOutcome:
    """Optimistically compare, then no-follow delete one already-resolved file.

    Unlike writes, deletion must not resolve the path a second time: an entry that
    becomes a symlink after batch prevalidation must reach the sandbox's no-follow
    deletion primitive and be refused, never be translated into its new target.
    """
    assert ctx.sandbox is not None
    resolved = _canonical(path)
    try:
        current: bytes | None = await ctx.sandbox.read_file(resolved)
    except FileNotFoundError:
        current = None
    if current != expected_before:
        expected = (
            "absent"
            if expected_before is None
            else hashlib.sha256(expected_before).hexdigest()[:12] + "…"
        )
        actual = "absent" if current is None else hashlib.sha256(current).hexdigest()[:12] + "…"
        return ToolOutcome(
            success=False,
            error="STALE_FILE_CONTEXT",
            content=(
                f"Delete refused — {path} changed while this operation was being prepared "
                f"(expected {expected}, now {actual}). Nothing was written. Read the "
                "current file, then re-apply the intended change to that revision."
            ),
            structured={
                "kind": "stale_file_context",
                "path": resolved,
                "next_required_action": "file_read",
                "suggested_args": {"path": resolved},
            },
        )
    if current is None:
        raise ValueError("cannot emit a deletion receipt for an absent resource")
    receipt = _file_mutation_receipt(resolved, before=current, after=None)
    await ctx.sandbox.delete_file(resolved)
    return receipt


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
    *,
    expected_before: bytes | None,
) -> MutationReceipt | ToolOutcome:
    """Validate and write ``new_bytes`` without a write-then-revert interval.

    Returns a failure ToolOutcome when new errors are introduced or the file
    changed concurrently; otherwise returns the exact committed mutation receipt.
    The diff-filter compares error-KIND tokens (not messages/line numbers) so
    pre-existing messy files aren't punished by whole-file rewrites that shift
    line numbers without changing the nature of the breakage. A rejected write
    never changes the workspace, including when the target did not exist before.
    """
    assert ctx.sandbox is not None
    new_text = new_bytes.decode("utf-8", errors="replace")
    pre = _syntax_errors(path, old_text) if old_text is not None else []
    post = _syntax_errors(path, new_text)
    introduced = [e for e in post if e not in pre]
    if introduced:
        return ToolOutcome(
            success=False,
            error="syntax_gate_reverted",
            content=(
                f"Your edit to {path} introduced syntax error(s); it was NOT applied "
                f"(workspace unchanged): {'; '.join(introduced)}. "
                "Fix the snippet and try a DIFFERENT edit. "
                "DO NOT re-run the same failed edit — it will fail identically."
            ),
        )
    return await _commit_file_mutation(
        ctx,
        path,
        new_bytes,
        expected_before=expected_before,
    )
