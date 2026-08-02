"""Exact Python/frontend public-surface and contract-byte gate.

The interior lives in :mod:`architecture.public_api_parts`; this module keeps
the Git-lineage authority, the two public entry points, and every name the
adversarial suite resolves through the ``public_api`` module object.

The three commit/digest constants below are monkeypatched by
``tests/architecture/test_public_api.py`` and are read by ``_pinned_authority``
and ``_source_prior``, which therefore stay here: a patch only takes effect on
the module whose globals the reader consults.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from . import public_surface
from .policy import REPO_ROOT, load_json
from .public_api_parts import (
    _authority,
    _constants,
    _contracts,
    _frontend,
    _members,
    _surface,
)
from .public_surface import scan_frontend_public_surface, scan_python_public_surface

# Monkeypatched by the adversarial suite — must be read through this module.
_ACCEPTED_AUTHORITY_COMMIT = "c89f517c95fe3104dd52c9f78b4f94d18f0ec1f7"
_ACCEPTED_AUTHORITY_SHA256 = (
    "7c1023c27ab91b018525612293901a8b7d671030b0ea7b0a2ff5c1fd66a0b3b2"
)
_PKG02_BASE_COMMIT = "1cf00dbe194a2a276ea1fd17ab74589355f2e0dc"

_SCHEMA = _constants.SCHEMA
_GIT_SHA = _constants.GIT_SHA
_AUTHORITY_PATH = _constants.AUTHORITY_PATH
_DERIVED_PATHS = _constants.DERIVED_PATHS
_TargetKey = _constants.TargetKey
_TargetIdentity = _constants.TargetIdentity

_compare_exact = _surface.compare_exact
_exact_rows = _surface.exact_rows
_stored_surface = _surface.stored_surface
_surface_targets = _surface.surface_targets
_check_contract_file = _contracts.check_contract_file
_check_diagram_transition = _contracts.check_diagram_transition
_regenerated_contracts = _contracts.regenerated_contracts
_updated_diagram_transitions = _contracts.updated_diagram_transitions

# Deliberate re-exports: the adversarial suite resolves these through the
# ``public_api`` module object, so the names must stay bound here even though
# this module does not call them itself.
_canonical = _surface.canonical
_extract_init_surface = _surface.extract_init_surface
_valid_bridge = _authority.valid_bridge
extract_initializer = public_surface.extract_initializer
# Epic 12-A's fourth authority is reached the same way, so its adversarial
# battery needs no second sys.path-dependent import of its own.
_frontend_authority = _frontend


def load_public_api(root: Path | None = None) -> dict[str, Any]:
    """Load only the requested root's public API authority."""
    resolved_root = REPO_ROOT if root is None else root
    return load_json(resolved_root / "architecture" / "public-api.json")


def _git_output(root: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], stderr=subprocess.PIPE
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("source_identity must resolve to a Git commit") from error


def _authority_at(root: Path, revision: str) -> tuple[dict[str, Any], bytes]:
    blob = _git_output(root, "show", f"{revision}:{_AUTHORITY_PATH}")
    try:
        authority = json.loads(blob)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Git public API authority is unavailable or malformed") from error
    if not isinstance(authority, dict) or authority.get("schema") != _SCHEMA:
        raise RuntimeError("Git public API authority schema mismatch")
    return authority, blob


def _pinned_authority(root: Path) -> tuple[dict[str, Any], bytes]:
    authority, blob = _authority_at(root, _ACCEPTED_AUTHORITY_COMMIT)
    if hashlib.sha256(blob).hexdigest() != _ACCEPTED_AUTHORITY_SHA256:
        raise RuntimeError("pinned public API authority digest mismatch")
    return authority, blob


def _source_prior(root: Path, source_identity: Any) -> tuple[dict[str, Any], bytes, str]:
    if not isinstance(source_identity, str) or not _GIT_SHA.fullmatch(source_identity):
        raise RuntimeError("source_identity must be a full Git commit")
    lineage = _git_output(root, "rev-list", "--parents", "-n", "1", source_identity
                          ).decode().split()
    if len(lineage) != 2 or lineage[0] != source_identity:
        raise RuntimeError("source_identity must have exactly one parent")
    _, source_blob = _authority_at(root, source_identity)
    if lineage[1] == _PKG02_BASE_COMMIT:
        prior, _ = _pinned_authority(root)
        if hashlib.sha256(source_blob).hexdigest() != _ACCEPTED_AUTHORITY_SHA256:
            raise RuntimeError("PKG-02 source authority is not the pinned bootstrap")
    else:
        prior, prior_blob = _authority_at(root, lineage[1])
        if source_blob != prior_blob:
            raise RuntimeError("source commit changed its parent public API authority")
    return prior, source_blob, lineage[1]


def _candidate_prior(root: Path, source_identity: Any) -> tuple[dict[str, Any], bytes]:
    prior, source_blob, parent = _source_prior(root, source_identity)
    head = _git_output(root, "rev-parse", "HEAD").decode().strip()
    if head == source_identity:
        return prior, source_blob
    lineage = _git_output(root, "rev-list", "--parents", "-n", "1", head
                          ).decode().split()
    changed = set(_git_output(root, "diff", "--name-only", source_identity, head)
                  .decode().splitlines())
    if len(lineage) != 2 or lineage[0] != head or lineage[1] != parent:
        raise RuntimeError("final candidate is not a sibling of source_identity")
    if not changed <= _DERIVED_PATHS:
        raise RuntimeError(f"final candidate has non-derived source drift: {sorted(changed)}")
    return prior, source_blob


def _regeneration_prior(root: Path, supplied: str | None) -> tuple[str, dict[str, Any]]:
    prior, source_blob, _ = _source_prior(root, supplied)
    head = _git_output(root, "rev-parse", "HEAD").decode().strip()
    if supplied != head:
        raise RuntimeError("supplied source_identity must equal HEAD")
    working_blob = (root / _AUTHORITY_PATH).read_bytes()
    if working_blob != source_blob:
        raise RuntimeError("working prewrite authority must equal the source commit blob")
    return supplied, prior


def _immutable_delta_problems(
    root: Path, candidate: dict[str, Any],
    python_surface: list[dict[str, Any]], frontend_surface: list[dict[str, Any]],
) -> list[str]:
    try:
        accepted = _candidate_prior(root, candidate.get("source_identity"))[0]
        accepted_targets = _surface_targets(
            accepted["python_initializers"], accepted["frontend_modules"]
        )
        candidate_targets = _surface_targets(python_surface, frontend_surface)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        return [f"immutable public API authority: {error}"]
    problems: list[str] = []
    _authority.check_regeneration_delta(
        accepted, candidate, root, accepted_targets, candidate_targets, problems
    )
    return [f"immutable public API authority: {problem}" for problem in problems]


def check_public_api(root: Path | None = None) -> dict[str, Any]:
    """Compare exact public signatures, origins, exports, types and schemas."""
    resolved_root = REPO_ROOT if root is None else root
    baseline = load_public_api(resolved_root)
    problems: list[str] = []
    if baseline.get("schema") != _SCHEMA:
        problems.append("public API schema mismatch")
    python_surface = scan_python_public_surface(resolved_root)
    frontend_surface = scan_frontend_public_surface(resolved_root)
    stored_python = _exact_rows(
        baseline, "python_initializers", "python_initializers", problems
    )
    stored_frontend = _exact_rows(
        baseline, "frontend_modules", "frontend_modules", problems
    )
    _compare_exact("Python public surface", stored_python, python_surface, problems)
    _compare_exact(
        "frontend compiler surface", stored_frontend, frontend_surface, problems
    )
    problems.extend(
        _immutable_delta_problems(
            resolved_root, baseline, stored_python, stored_frontend
        )
    )
    contracts = _exact_rows(baseline, "contract_files", "contract_files", problems)
    for contract in contracts:
        _check_contract_file(contract, resolved_root, problems)
    contract_map = {row["path"]: row for row in contracts if "path" in row}
    transitions = baseline.get("diagram_transitions")
    if not isinstance(transitions, list) or len(transitions) != 1:
        problems.append("exactly one PKG-02 diagram transition is required")
    else:
        _check_diagram_transition(
            transitions[0], resolved_root, contract_map, problems
        )
    _authority.check_metadata(
        baseline, python_surface, frontend_surface, resolved_root, problems
    )
    return {
        "ok": not problems,
        "problems": problems,
        "initializer_count": len(python_surface),
        "contract_file_count": len(contracts),
        "frontend_module_count": len(frontend_surface),
    }


def _carried(
    previous: dict[str, Any], key: str, supplied: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    return previous.get(key, []) if supplied is None else supplied


def _accepted_authority_problems(
    resolved_root: Path, prior: dict[str, Any], previous: dict[str, Any],
    previous_python: list[dict[str, Any]], previous_frontend: list[dict[str, Any]],
) -> list[str]:
    problems: list[str] = []
    _authority.check_metadata(
        previous, previous_python, previous_frontend, resolved_root, problems
    )
    _authority.check_regeneration_delta(
        prior,
        previous,
        resolved_root,
        _surface_targets(
            _stored_surface(prior, "python_initializers"),
            _stored_surface(prior, "frontend_modules"),
        ),
        _surface_targets(previous_python, previous_frontend),
        problems,
    )
    return problems


def regenerate_public_api(
    root: Path | None = None,
    source_identity: str | None = None,
    *,
    additive_transitions: list[dict[str, Any]] | None = None,
    compatibility_bridges: list[dict[str, Any]] | None = None,
    member_transitions: list[dict[str, Any]] | None = None,
    frontend_declaration_transitions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved_root = REPO_ROOT if root is None else root
    source_identity, prior = _regeneration_prior(resolved_root, source_identity)
    previous = load_public_api(resolved_root)
    previous_python = _stored_surface(previous, "python_initializers")
    previous_frontend = _stored_surface(previous, "frontend_modules")
    accepted_problems = _accepted_authority_problems(
        resolved_root, prior, previous, previous_python, previous_frontend
    )
    if accepted_problems:
        raise RuntimeError(
            "accepted public API metadata is invalid: " + "; ".join(accepted_problems)
        )
    contracts = _regenerated_contracts(previous, resolved_root)
    transitions = _updated_diagram_transitions(previous, resolved_root)
    python_surface = scan_python_public_surface(resolved_root)
    frontend_surface = scan_frontend_public_surface(resolved_root)
    inventory = {
        "schema": _SCHEMA,
        "source_identity": source_identity,
        "python_initializers": python_surface,
        "frontend_modules": frontend_surface,
        "contract_files": contracts,
        "diagram_transitions": transitions,
        "additive_transitions": _carried(
            previous, "additive_transitions", additive_transitions
        ),
        "compatibility_bridges": _carried(
            previous, "compatibility_bridges", compatibility_bridges
        ),
        "member_transitions": _carried(
            previous, "member_transitions", member_transitions
        ),
        "frontend_declaration_transitions": _carried(
            previous,
            "frontend_declaration_transitions",
            frontend_declaration_transitions,
        ),
        "compatibility_rule": (
            "Any deleted or renamed public name, origin, signature, frontend "
            "export/type/schema, or contract byte fails. Additions require an "
            "owning-package baseline transition; moved implementations retain "
            "the old public name through an explicit bridge until PKG-13. A "
            "member-level change at an unchanged origin requires an explicit "
            "member transition pinning both signature digests and the exact "
            "member delta. A frontend declaration change requires an explicit "
            "frontend declaration transition pinning both target digests and "
            "both declaration texts."
        ),
    }
    problems: list[str] = []
    _authority.check_metadata(
        inventory, python_surface, frontend_surface, resolved_root, problems
    )
    _authority.check_regeneration_delta(
        previous,
        inventory,
        resolved_root,
        _surface_targets(previous_python, previous_frontend),
        _surface_targets(python_surface, frontend_surface),
        problems,
    )
    if problems:
        raise RuntimeError("public API regeneration rejected: " + "; ".join(problems))
    output = resolved_root / "architecture" / "public-api.json"
    output.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    return {
        "written": str(output),
        "initializer_count": len(inventory["python_initializers"]),
        "frontend_module_count": len(inventory["frontend_modules"]),
        "contract_file_count": len(inventory["contract_files"]),
        "member_transition_count": len(inventory["member_transitions"]),
        "frontend_declaration_transition_count": len(
            inventory["frontend_declaration_transitions"]
        ),
    }


def member_signature_sha256(signature: Any) -> str:
    """Expose the member-transition digest helper for record authoring."""
    return _members.signature_sha256(signature)
