"""Deterministic detection of an app's release shape (Track-1 subset).

`detect_release` decides *what kind of release* a workspace is — a node service,
a python service, a static bundle, an AppKit-shaped app, an owner-supplied
container, an as-yet-undeclared import, or a non-web workspace — reading ONLY two
inputs:

* the IMMUTABLE project contents (`files`: a path -> content view), and
* the caller's TYPED declaration (`intent`, `provenance`).

It NEVER branches on the requesting build lane, the calling context, or any
free-text instruction — a hosting decision must be reproducible from what the
project *is*, not from how the request happened to be phrased. Two runs over the
same file map return EQUAL results (every collection is built in a stable order),
so a release verdict is a pure function of committed contents + typed intent.

Precedence ladder (highest wins), the Track-1 subset:

1. A typed `ReleaseIntent`, OR the four AppKit contract files
   (`.disco/appspec.json` + `wrangler.toml` + `worker/index.ts` + `schema.sql`)
   -> `candidate`. AppKit-shaped apps get a single `dev_server` ingress plus a
   sqlite-class resource. A typed intent that arrives ALONGSIDE an owner-supplied
   container manifest is two competing declarations of the release shape: that
   conflict fails closed to `needs_review` citing BOTH pieces of evidence.
2. An existing `Dockerfile` / `compose.yaml` (no intent) -> `needs_review` with a
   repairable diagnostic naming the exact fields that cannot be verified
   statically (owner-review UI is Track 2; here we only fail closed with data).
3. Deterministic detectors: `package.json` with a server `start` script -> node;
   `pyproject.toml` / `requirements.txt` with a web-framework import -> python;
   a root `index.html` with no server evidence -> static.
4. `provenance.imported=True` without a typed intent -> `needs_review`, the same
   repairable diagnostic naming every missing release-contract field.
5. No HTTP evidence at all -> `not_web`, with an honest human reason.

Databases: recognized SQLite evidence (a `DATABASE_URL=file:` reference, a
libSQL/drizzle sqlite config, or a `*.db` file) yields a sqlite resource with the
local profile `file:/data/app.db`. Any UNKNOWN engine (a `mysql://` /
`postgres://` / … url) fails closed to `needs_review` and NEVER a guessed
resource — an unmanaged database is a deploy hazard, not a default.

Deliberately ABSENT here (they belong to the Track-2 detection ladder and are NOT
implemented in this module):

* clean-room probe execution (running the build in an ephemeral jail to observe
  its real ports / output tree rather than reasoning about the files);
* attestation reuse (trusting a prior signed release verdict for an unchanged
  tree instead of re-deriving it);
* preview-evidence consumption (folding a live preview's observed HTTP behaviour
  into the assessment).

Layering: `disco.core` is the leaf — this module imports ONLY pydantic, the
stdlib, and the sibling `disco.core.release.spec` contract. It does NOT import
`disco.tools.*` or any higher layer.

This module is the state-free public compatibility/export facade.  Cohesive
private implementation lives under :mod:`disco.core.release.detect_parts` and is
re-imported here so every public symbol keeps its import path
(`disco.core.release.detect.detect_release` and friends).
"""

from __future__ import annotations

from collections.abc import Mapping

from disco.core.release.spec import ReleaseIntent
from disco.core.release.spec import local_mount_target as local_mount_target

from .detect_parts._env import _discovered_build_env_names as _discovered_build_env_names
from .detect_parts._env import (
    _unrepresentable_build_env_blocker as _unrepresentable_build_env_blocker,
)
from .detect_parts._intent import _from_intent
from .detect_parts._models import (
    DetectionBlocker,
    DetectionResult,
    MissingField,
    Provenance,
    _DetectBlocker,
)
from .detect_parts._node import _node_detect
from .detect_parts._python import _python_detect
from .detect_parts._results import (
    _appkit_result,
    _conflict_result,
    _container_review,
    _detected_result,
    _fail_closed,
    _imported_review,
    _not_web_result,
    _runtime_conflict_result,
)
from .detect_parts._static import _static_detect
from .detect_parts._text import _container_manifest, _is_appkit

# ---- compatibility re-exports --------------------------------------------------
#
# A handful of pre-existing focused test suites reach past the public API into
# specific detection internals (`_discovered_build_env_names`,
# `_unrepresentable_build_env_blocker`) or monkeypatch the shared
# `local_mount_target` real-parent derivation at this module's attribute
# (mirroring the same substitution against `disco.core.release.spec` and
# `disco.core.release.local_compose`, the other two modules that consume it).
# The `as name` re-export form above marks each as an intentionally checked
# name rather than an accidental unused import.

# ---- the public entrypoint ----------------------------------------------------


def detect_release(
    files: Mapping[str, str | bytes],
    *,
    intent: ReleaseIntent | None,
    provenance: Provenance,
) -> DetectionResult:
    """Detect a workspace's release shape from its immutable contents + typed
    intent, following the precedence ladder documented at module top.

    PURE and deterministic: it reads only `files`, `intent`, and `provenance`,
    builds every collection in a stable order, and returns the same
    `DetectionResult` for the same inputs (so `detect_release(m) ==
    detect_release(m)`). It never spawns a process, touches the network, or
    branches on anything but the two typed inputs.
    """
    manifest = _container_manifest(files)

    # Rung 1 — a typed intent that arrives alongside an owner-supplied container
    # manifest is a conflict between two release declarations: fail closed.
    if intent is not None and manifest is not None:
        return _conflict_result(intent, manifest)

    # Rung 1 — a typed intent wins outright.
    if intent is not None:
        return _from_intent(intent, files)

    # Rung 1 — the AppKit contract shape.
    if _is_appkit(files):
        return _appkit_result(files)

    # Rung 2 — an existing container manifest, no intent: needs owner review.
    if manifest is not None:
        return _container_review(manifest)

    # Rung 3 — deterministic detectors (with database handling). Each detector
    # returns `None` (its runtime signature is absent), a `ReleaseService` (a
    # resolved candidate), or a `_DetectBlocker` (its signature is present but the
    # contract is predictably broken — fail closed with the exact typed code). When
    # two DIFFERENT runtime signatures both match (a node server start-script AND a
    # python web-framework entrypoint), that is genuinely conflicting evidence: fail
    # closed to `runtime_conflict` naming both.
    node = _node_detect(files)
    python = _python_detect(files)
    if node is not None and python is not None:
        return _runtime_conflict_result()
    outcome = node if node is not None else python
    if outcome is None:
        outcome = _static_detect(files)
    if isinstance(outcome, _DetectBlocker):
        return _fail_closed(outcome)
    if outcome is not None:
        return _detected_result(outcome, files)

    # Rung 4 — imported without a typed intent: repairable review.
    if provenance.imported:
        return _imported_review(provenance)

    # Rung 5 — no HTTP evidence at all.
    return _not_web_result()


__all__ = [
    "DetectionBlocker",
    "DetectionResult",
    "MissingField",
    "Provenance",
    "detect_release",
]
