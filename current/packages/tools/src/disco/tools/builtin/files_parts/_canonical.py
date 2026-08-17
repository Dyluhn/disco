"""Canonical path key shared by every file-tool guard and tracker."""

from __future__ import annotations

from ...sandbox.base import strip_redundant_workspace_prefix


def _canonical(path: str) -> str:
    """Canonical tracker key so the read-before-rewrite guard can't be bypassed by
    spelling the same file differently. Strips the redundant workspace prefix
    ('workspace/foo' == '/workspace/foo' == 'foo') AND normalizes './', '//' and
    '../' segments ('./x.py' == 'x.py') via posixpath.normpath, so a read of one
    spelling and a mutate of another resolve to ONE key."""
    import posixpath

    return posixpath.normpath(strip_redundant_workspace_prefix(path))
