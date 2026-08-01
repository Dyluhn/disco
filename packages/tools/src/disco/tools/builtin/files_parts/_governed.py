"""CD-TOOLS-4 — routing generic-mutator writes away from the host-governed
``.disco/`` namespace and toward the semantic tool that owns each artifact."""

from __future__ import annotations

from typing import Any

from ...anatomy import ToolOutcome
from ._canonical import _canonical


async def _governed_relpath(sandbox: Any, path: str) -> str:
    """The REAL (symlink-followed) workspace-relative path, forward-slashed. Uses the sandbox's
    resolve_relpath (which follows symlinks via the guest realpath + rejects jail escapes) so a
    symlink can't forge a non-governed name — implemented on BOTH ProcessSandbox and the container
    backends. Falls back to the lexical canonical strip only if the method is absent or the real
    location can't be verified; in that case the mutator's own jailed write fails closed anyway."""
    rr = getattr(sandbox, "resolve_relpath", None)
    if rr is not None:
        try:
            return str(await rr(path)).replace("\\", "/")
        except Exception:  # noqa: BLE001 — an escaping/unverifiable path is rejected by the write
            pass
    return _canonical(path)


def _is_governed_artifact(relpath: str) -> bool:
    """True for the host-reserved `.disco/` namespace (appspec/tweaks/versions/context) — those
    are authored only by semantic tools, never the generic writer."""
    return relpath == ".disco" or relpath.startswith(".disco/")


# CD-TOOLS-4 — route a governed-artifact write to the SEMANTIC tool that owns it. (match-rule,
# tool, why); a rule ending in "/" matches a prefix, else an exact path. Order = most specific
# first. A governed path with NO match is host-managed with no generic editor (no false tool name).
_GOVERNED_ROUTING: tuple[tuple[str, str, str], ...] = (
    (".disco/appspec.json", "app_set_tweak", "it is the AppKit app spec"),
    (".disco/tweaks.json", "app_set_tweak", "it holds the app's tweak values"),
    (".disco/versions/", "app_snapshot_version", "it is a version snapshot"),
    (".disco/context/", "context_memory", "it is durable context memory"),
)


def _route_for_governed(relpath: str) -> tuple[str | None, str]:
    """The semantic tool (and reason) that owns `relpath`, or (None, '') if it is governed but
    has no generic editor."""
    for rule, tool, why in _GOVERNED_ROUTING:
        if rule.endswith("/"):
            if relpath.startswith(rule):
                return tool, why
        elif relpath == rule:
            return tool, why
    return None, ""


_HARNESS_BOOKKEEPING_MESSAGE = (
    ".disco/ files are harness-managed bookkeeping — you never need to edit them. "
    "Continue with the task's own deliverables."
)


def _governed_route_text(
    relpath: str,
    *,
    allowed_tools: frozenset[str] | None,
) -> tuple[str | None, str]:
    tool, why = _route_for_governed(relpath)
    if tool is None:
        return None, "It is host-managed; do not edit it with a generic write tool."
    if allowed_tools is not None and tool not in allowed_tools:
        if relpath.startswith(".disco/context/") and tool == "context_memory":
            return None, _HARNESS_BOOKKEEPING_MESSAGE
        return None, "It is host-managed; do not edit it with a generic write tool."
    return tool, f"Use {tool} to change it ({why})."


async def _governed_guard(
    sandbox: Any,
    path: str,
    *,
    error: str = "GOVERNED_ARTIFACT_REJECTED",
    allowed_tools: frozenset[str] | None = None,
) -> ToolOutcome | None:
    """CD-TOOLS-4 artifact-aware guard, shared by ALL generic mutators: refuse a write whose REAL
    (symlink-followed) path is in the host-governed `.disco/` namespace, ROUTING the model to the
    owning semantic tool (or saying plainly there is no generic editor). Returns a blocking
    ToolOutcome, or None if the path is not governed. None of the semantic tools route through the
    generic mutators (they write `.disco/` via the sandbox directly), so this never blocks them.
    `error` lets safe_write_file keep its campaign-named SAFE_WRITE_GOVERNED_ARTIFACT_REJECTED code
    while sharing one routing implementation.

    Short-circuit: a path already LEXICALLY under `.disco/` is governed without resolving (cheap +
    backend-agnostic); only a non-`.disco` lexical path needs the real-path resolve to catch a
    symlink that reaches INTO `.disco/`."""
    rel = _canonical(path)
    if not _is_governed_artifact(rel):
        rel = await _governed_relpath(sandbox, path)
    if not _is_governed_artifact(rel):
        return None
    tool, route = _governed_route_text(rel, allowed_tools=allowed_tools)
    return ToolOutcome(
        success=False,
        error=error,
        content=(
            f"Refused — {path} resolves to the host-managed .disco/ namespace ({rel}). {route}"
        ),
        structured={
            "kind": "governed_artifact_rejected",
            "path": path,
            "resolved": rel,
            "route_to": tool,
        },
    )
