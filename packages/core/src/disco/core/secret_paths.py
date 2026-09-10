"""Runtime-secret path classifier — the ONE predicate that decides whether a
workspace-relative path can carry live credentials.

This lives in `disco.core` (the leaf package) so BOTH the tools-layer archive/
store code (snapshots, versions, imports, rehydration, downloads) AND the release
validation plane (`disco.core.release.validate`) can share a single source of
truth. `disco.tools.projects.archive` re-exports it unchanged, so every existing
`from disco.tools.projects import is_runtime_secret_path` import keeps working —
`tools` importing from `core` is a legal downward dependency.

Pure stdlib (`pathlib`): no pydantic, no I/O, no subprocess. A path classifier
must never touch a container or the network, only reason about the string.
"""

from __future__ import annotations

from pathlib import Path

# Explicit template/example suffixes stay exportable: a `.env.example` /
# `.dev.vars.sample` documents the shape without carrying real secret material.
_SAFE_TEMPLATE_SUFFIXES = frozenset({"example", "sample", "dist", "template"})

# The dotenv / dev-var stems we treat as secret-bearing. A bare match (`.env`,
# `.dev.vars`) or a match with any suffix that is NOT an explicit template
# (`.env.local`, `.dev.vars.production`) is secret; a template suffix is not.
_SECRET_STEMS = (".dev.vars", ".env")


def is_runtime_secret_path(relative_path: str) -> bool:
    """Whether a workspace path can carry runtime credentials.

    Real dotenv/dev-var files never enter snapshots, versions, imports, manifests,
    rehydration, or downloads. Explicit template suffixes remain exportable.
    """
    parts = tuple(part.lower() for part in Path(relative_path).parts)
    for name in parts:
        for stem in _SECRET_STEMS:
            if name == stem:
                return True
            if name.startswith(stem + "."):
                suffix = name.rsplit(".", 1)[-1]
                if suffix not in _SAFE_TEMPLATE_SUFFIXES:
                    return True
    return False


__all__ = ["is_runtime_secret_path"]
