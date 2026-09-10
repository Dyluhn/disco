"""ROOT-1 (slides spiral) — make a literal ``/workspace`` resolve in the shell.

`ProcessSandboxInstance._rewrite_workspace_paths` delegates here. The build/agent
prompts tell the model files live in ``/workspace``, and the FILE tools honor
that, but this backend's shell runs with ``cwd`` = the real per-instance
``/tmp/disco-sbx-.../sbx_.../`` dir, which has no literal ``/workspace`` — so a
model command like ``ls /workspace/deck.pptx`` would exit 2 without this
rewrite.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from ..base import SandboxPermissionError

if TYPE_CHECKING:
    from ..process import ProcessSandboxInstance

# ROOT-1 (slides spiral): a genuine `/workspace`-rooted path token in a shell
# command — the leading `/workspace` AND the rest of the path up to the next token
# boundary (unescaped whitespace / quote / shell operator / end). Capturing the WHOLE token
# (not just the `/workspace` prefix) lets the rewrite RESOLVE + JAIL it via the same
# helper the file tools use, so a `..` traversal can't escape the jail.
# The lookbehind keeps it from matching a mid-path occurrence ('/foo/workspace') or a
# substring ('myworkspace'); the lookahead right after `/workspace` keeps it from
# matching a longer name ('/workspaces') — it only fires when `/workspace` is followed
# by a path separator, a token boundary, or end-of-string.
# Shell expansion inside a token: $VAR, ${VAR}, $(cmd), `cmd`.
_SHELL_EXPANSION_RE = re.compile(r"[$`]")

_WORKSPACE_TOKEN_RE = re.compile(
    r"(?<![\w/.])/workspace(?=/|$|[\s'\";|&<>()`])"
    r"(?:/(?:\\[^\n]|[^\s\\'\";|&<>()`])*)?"
)


def rewrite_workspace_paths(instance: ProcessSandboxInstance, cmd: str) -> str:
    """ROOT-1 (slides spiral): make a literal ``/workspace`` resolve in the shell.

    The build/agent prompts tell the model files live in ``/workspace``, and the
    FILE tools honor that (``strip_redundant_workspace_prefix`` maps
    ``workspace/foo`` → ``foo``, jailed to the real dir). But this backend's shell
    runs with ``cwd`` = the real per-instance ``/tmp/disco-sbx-.../sbx_.../`` dir,
    which has NO literal ``/workspace`` — so a model command like
    ``ls /workspace/deck.pptx`` exits 2 and the agent hunts around (``find /`` …).
    On the container backends ``/workspace`` genuinely exists, so this only bites
    the process (dev) backend.

    Rewrite each genuine ``/workspace``-rooted path TOKEN (word-boundary — never
    ``/workspaces`` and never a mid-substring) to its ``shlex.quote``-d absolute
    real-workspace path. Relative paths already resolve via ``cwd``, so a NEW-file
    relative path (``echo hi > out.txt``) is untouched — only a ``/workspace``
    prefix is translated.

    [SECURITY — P1] The rewrite RESOLVES + JAILS each token through ``_resolve`` —
    the SAME jail the file tools use — so the shell ``/workspace`` semantics match
    the file-tool ``/workspace`` semantics exactly (single source of truth). A
    token whose ``..`` traversal escapes the workspace (e.g.
    ``/workspace/../../etc/passwd``) makes ``_resolve`` raise
    ``SandboxPermissionError`` and the whole command is rejected (fail closed) —
    the rewrite must never itself manufacture an out-of-jail absolute path.
    """

    # A token whose tail the SHELL computes at run time ("/workspace/$f",
    # "/workspace/${name}", "/workspace/`basename x`"). It cannot be resolved
    # here, and shlex.quote-ing it is actively WRONG: unquoted it suppresses
    # the expansion the model wrote, and inside existing quotes it injects
    # literal quote characters into the filename. Either way every path comes
    # back "missing" while a literal `ls` of the same file succeeds — the
    # environment contradicting itself, which is what sent counted seed 440023
    # into a read-loop it could not reason its way out of.
    def _sub(m: re.Match[str]) -> str:
        # _resolve strips the redundant /workspace prefix, joins onto the real
        # workspace root, resolves, and raises SandboxPermissionError on escape.
        # Parse shell escapes in this ONE matched token first.  In particular,
        # ``hero\ image.svg`` is one path; treating its space as a boundary
        # rewrote only ``hero\`` and silently manufactured a second argv token.
        token = m.group(0)
        if _SHELL_EXPANSION_RE.search(token):
            # Substitute ONLY the `/workspace` prefix and leave the shell's own
            # expansion — and the caller's quoting context — exactly as written.
            rest = token[len("/workspace") :]
            if ".." in rest:
                raise SandboxPermissionError(
                    f"/workspace path with shell expansion may not traverse: {token!r}"
                )
            root = str(instance._workspace)
            if shlex.quote(root) != root:
                # Bare substitution is only safe while the root needs no quoting;
                # refuse loudly rather than emit a path that silently mis-parses.
                raise SandboxPermissionError(
                    "cannot expand a /workspace path containing shell expansion "
                    f"because the workspace path requires quoting: {root!r}"
                )
            return root + rest
        try:
            parsed = shlex.split(token, posix=True)
        except ValueError as exc:
            raise SandboxPermissionError(f"invalid /workspace path token: {token!r}") from exc
        if len(parsed) != 1:
            raise SandboxPermissionError(f"invalid /workspace path token: {token!r}")
        return shlex.quote(str(instance._resolve(parsed[0])))

    return _WORKSPACE_TOKEN_RE.sub(_sub, cmd)
