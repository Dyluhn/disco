"""The workspace containment predicate and the write-target validator it shares
with the guarded writer.

Extracted from ``deploy.py`` to reduce module complexity; the public facade
re-imports ``workspace_contained`` unchanged.

NOTE: the guarded reader/writer entry points themselves (``read_workspace_file``,
``write_workspace_file``, ``_open_workspace_write_parent``, ``ensure_workspace_dir``)
stay defined directly on the ``deploy`` facade rather than here — a pre-existing
regression suite (``test_no_raw_workspace_reads_outside_guarded_reader`` /
``test_no_raw_workspace_writes_outside_guarded_writer``) asserts, via
``inspect.getsource`` on the ``deploy`` MODULE OBJECT itself, that every raw
``read_text``/``read_bytes``/``write_text``/``write_bytes``/``mkdir`` call in the
package lives textually inside those choke-point functions' own source — a
guarantee ``inspect.getsource`` can only see when the real implementation is
physically part of ``deploy.py``'s file, not merely re-exported from here.
"""

from __future__ import annotations

from pathlib import Path

from ..models import DeployRefused, RefusalReason


def workspace_contained(path: Path, workspace_root: Path) -> bool:
    """The ONE workspace-containment guard shared by BOTH the planner/tree-digest
    reads (:func:`_tree_digest`) and the host→sandbox push (``sandbox_build``), so
    the two can never drift.

    True ONLY when *path* stays STRICTLY inside the already-resolved
    *workspace_root* AFTER resolving symlinks. ``Path.resolve`` follows every
    symlink in the path, so a workspace entry that IS (or traverses) a symlink
    pointing at a host file resolves OUTSIDE the root and is rejected — the caller
    must NEVER ``read_bytes`` through it. A normal regular file inside the
    workspace resolves to a path under the root and is allowed. Fail CLOSED on any
    resolution error (a dangling/cyclic link is treated as not contained)."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == workspace_root or resolved.is_relative_to(workspace_root)


def _guarded_write_target(rel: Path | str, workspace_root: Path) -> Path:
    """Validate (and return) the absolute host path for WRITING
    ``workspace_root/rel``. The WRITE-class mirror of :func:`workspace_contained` /
    ``read_workspace_file`` — fail CLOSED with :class:`DeployRefused`
    (``WORKSPACE_SYMLINK_ESCAPE``) when:

      * *rel* is absolute or contains a ``..`` component, OR
      * ANY path component, from ``workspace_root`` down to the target, is a SYMLINK.

    Crucially it LSTATs each component (:meth:`Path.is_symlink`) rather than calling
    :meth:`Path.resolve` — ``resolve`` would FOLLOW an in-workspace symlink and MASK
    it. That masking is exactly the round-8 escape codex flagged for the
    digest-SKIPPED ``.disco`` record dir: a planted symlink at ``.disco`` /
    ``.disco/cloudflare`` / ``deployments`` (any component) could redirect the record
    WRITE outside the workspace, and the same for a symlinked ``dist`` root or a
    ``wrangler.toml`` parent. Because no component is a symlink once this passes, the
    lexical target equals its resolved form and is GUARANTEED strictly inside the
    resolved ``workspace_root``. Returns the absolute (lexical, unresolved) target;
    the caller writes ONLY after this passes."""
    rel = Path(rel)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            f"A workspace WRITE path is absolute or escapes via '..': {rel}. "
            "Refusing to write it — the deploy fails closed.",
        )
    cur = workspace_root
    if cur.is_symlink():  # the workspace root itself must be a real directory
        raise DeployRefused(
            RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
            "The workspace root is a symlink; refusing to write through it.",
        )
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise DeployRefused(
                RefusalReason.WORKSPACE_SYMLINK_ESCAPE,
                f"A workspace WRITE path component is a symlink: {part} (in {rel}). "
                "Refusing to write through it — the deploy fails closed.",
            )
    return cur
