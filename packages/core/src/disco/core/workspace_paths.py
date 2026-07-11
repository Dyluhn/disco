"""Workspace-relative path normalization shared across layers.

ROOT-2 (runthru-v2): the sandbox cwd IS the workspace, but the build/agent
prompts say files live in '/workspace', so the model often writes paths like
'workspace/index.html' (a redundant relative prefix). Joining that onto the
workspace root produced '/workspace/workspace/index.html' — which broke the
live preview (the server serves the workspace ROOT, so it 404'd on the doubled
path) AND plan-step done-conditions / file_exists (they check the root). An
ABSOLUTE '/workspace/foo' already resolves correctly in the container but
ESCAPES in the process backend, so normalize BOTH forms to a path relative to
the workspace root. Only strips ONE leading 'workspace/' or '/workspace/' — a
nested 'src/workspace/x' is untouched.
"""

from __future__ import annotations


def strip_redundant_workspace_prefix(path: str) -> str:
    """Return ``path`` with a single leading ``workspace/`` or ``/workspace/``
    removed. The workspace root itself becomes the empty string."""
    for prefix in ("/workspace/", "workspace/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    if path in ("/workspace", "workspace"):
        return ""  # the workspace root itself
    return path
