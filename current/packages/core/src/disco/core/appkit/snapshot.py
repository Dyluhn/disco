"""AppKit EPIC H — versioning + summary helpers over the structured specs.

Two pure concerns, shared by the manifest writer (H1), the project routes (H2),
and the `app_snapshot_version` tool (H3) so none of them re-derive the same shape:

* `summarize_specs(app, design)` — the compact, JSON-safe `app` summary the
  project manifest persists and the Projects list renders (name/kind, page /
  route / section / entity counts, the section-kind set, the design tokens, and
  a stable `spec_digest`). Validity / error wrapping is the caller's job.
* `spec_digest` / `tree_file_hashes` / `tree_digest` — content hashes that turn a
  pair of specs (and the deterministic tree they generate) into an immutable
  VERSION identity. `app_snapshot_version` records these so a later run can prove
  the on-disk generated tree still matches its specs (drift detection).

Layering: `disco.core.appkit` is a leaf — this imports ONLY the stdlib + the
sibling spec/generator models. No tools / agent-server / runtime imports.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .spec import AppSpec, DesignSpec

# All digests are namespaced with their algorithm so a future bump (e.g. a switch
# to blake2) is self-describing on disk and a reader can tell versions apart.
_ALGO = "sha256"


def _hash_text(text: str) -> str:
    return f"{_ALGO}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def spec_digest(app: AppSpec, design: DesignSpec) -> str:
    """A stable content digest over BOTH specs.

    Canonical JSON (sorted keys, no insignificant whitespace) so two specs that
    are equal as data hash identically regardless of field order / formatting.
    This is the version identity the manifest summary and the snapshot tool both
    record — single-source so they never disagree about whether the app changed.
    """
    payload = json.dumps(
        {
            "app": app.model_dump(mode="json"),
            "design": design.model_dump(mode="json"),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _hash_text(payload)


def tree_file_hashes(tree: dict[str, str]) -> dict[str, str]:
    """Per-file content hash for a generated tree (`{path: contents}`), sorted by
    path. Lets a snapshot record the exact bytes each generated file SHOULD have,
    so drift is a per-file diff, not just an all-or-nothing tree mismatch."""
    return {path: _hash_text(tree[path]) for path in sorted(tree)}


def tree_digest(tree: dict[str, str]) -> str:
    """A single digest over the whole generated tree (paths + contents). Order-
    independent (paths are sorted) and collision-resistant (each path + body is
    length-delimited by a NUL so `{"ab": "c"}` and `{"a": "bc"}` can't collide)."""
    h = hashlib.sha256()
    for path in sorted(tree):
        body = tree[path]
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(str(len(body)).encode("ascii"))
        h.update(b"\0")
        h.update(body.encode("utf-8"))
        h.update(b"\0")
    return f"{_ALGO}:{h.hexdigest()}"


def summarize_specs(app: AppSpec, design: DesignSpec) -> dict[str, Any]:
    """The compact, JSON-safe `app` summary for the manifest + Projects list.

    Pure projection of the two VALIDATED specs — counts, the section-kind set, the
    design tokens, and the `spec_digest`. The caller owns validity framing: a valid
    summary is `{"valid": True, ...}`; a spec that won't load is the caller's
    `{"valid": False, "error": ...}` (kept out of here so this stays a pure fn of
    two good specs)."""
    section_kinds = sorted({s.kind for p in app.pages for s in p.sections})
    section_count = sum(len(p.sections) for p in app.pages)
    routes = [p.route for p in app.pages]
    palette = design.palette
    typography = design.typography
    return {
        "valid": True,
        "name": app.name,
        "app_kind": app.app_kind,
        "page_count": len(app.pages),
        "route_count": len(set(routes)),
        "section_count": section_count,
        "entity_count": len(app.entities),
        "section_kinds": section_kinds,
        "design": {
            "palette": {
                "primary": palette.primary,
                "surface": palette.surface,
                "text": palette.text,
                "accent": palette.accent,
            },
            "typography": {
                "heading_font": typography.heading_font,
                "body_font": typography.body_font,
            },
            "layout_family": design.layout_family,
            "component_style": design.component_style,
            "density": design.density,
        },
        "spec_digest": spec_digest(app, design),
    }


__all__ = [
    "spec_digest",
    "summarize_specs",
    "tree_digest",
    "tree_file_hashes",
]
