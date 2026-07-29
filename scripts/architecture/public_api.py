"""Exact Python/frontend public-surface and contract-byte gate."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .policy import REPO_ROOT, load_json
from .public_surface import (
    extract_initializer,
    scan_frontend_public_surface,
    scan_python_public_surface,
)

_SCHEMA = "disclaude-architecture-public-api-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_PACKAGE = re.compile(r"PKG-\d{2}-[A-Z0-9-]+")
_SURFACES = {"frontend", "python"}
_TRANSITION_FIELDS = {"surface", "path", "public_name", "target_sha256", "owner_package", "reason"}
_BRIDGE_FIELDS = {
    "path", "public_name", "old_origin", "new_origin", "owner_package",
    "removal_package", "reason",
}
_ACCEPTED_DIAGRAM_SHA256 = "759993f1a3104700efe8f48395923117546bd0fd1ca371681bc2a09fc95b9f26"
_ACCEPTED_AUTHORITY_COMMIT = "c89f517c95fe3104dd52c9f78b4f94d18f0ec1f7"
_ACCEPTED_AUTHORITY_SHA256 = "7c1023c27ab91b018525612293901a8b7d671030b0ea7b0a2ff5c1fd66a0b3b2"
_PKG02_BASE_COMMIT = "1cf00dbe194a2a276ea1fd17ab74589355f2e0dc"
_AUTHORITY_PATH = "architecture/public-api.json"
_DERIVED_PATHS = {_AUTHORITY_PATH, "architecture/test-inventory.json",
                  "docs/governance/CAMPAIGN-STATUS.md", "docs/governance/PROTECTED.sha256"}
_TargetKey = tuple[str, str, str, str]
_TargetIdentity = tuple[str, str, str]


def load_public_api(root: Path | None = None) -> dict[str, Any]:
    """Load only the requested root's public API authority."""
    resolved_root = REPO_ROOT if root is None else root
    return load_json(resolved_root / "architecture" / "public-api.json")


def _extract_init_surface(path: Path) -> dict[str, Any]:
    root = path
    while root != root.parent and not (root / ".git").exists():
        root = root.parent
    if not (root / ".git").exists():
        raise ValueError("initializer must be inside an explicit Git root")
    rel = path.relative_to(root).as_posix()
    surface = extract_initializer(root, rel)
    return {key: value for key, value in surface.items() if key != "path"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _exact_rows(
    owner: dict[str, Any], key: str, label: str, problems: list[str],
) -> list[dict[str, Any]]:
    rows = owner.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        problems.append(f"{label} must be an exact object list")
        return []
    paths = [row.get("path") for row in rows]
    if any(not isinstance(path, str) or not path for path in paths):
        problems.append(f"{label} paths must be non-empty strings")
    elif paths != sorted(paths) or len(paths) != len(set(paths)):
        problems.append(f"{label} paths must be sorted and unique")
    return rows


def _compare_exact(
    label: str, stored: list[dict[str, Any]],
    actual: list[dict[str, Any]], problems: list[str],
) -> None:
    if stored == actual:
        return
    stored_map = {row.get("path"): row for row in stored}
    actual_map = {row.get("path"): row for row in actual}
    deleted = sorted(set(stored_map) - set(actual_map))
    added = sorted(set(actual_map) - set(stored_map))
    common = set(stored_map) & set(actual_map)
    changed = sorted(path for path in common if stored_map[path] != actual_map[path])
    problems.append(
        f"{label} drift: deleted={deleted}, added={added}, changed={changed}"
    )


def _check_contract_file(contract: dict[str, Any], root: Path, problems: list[str]) -> None:
    rel = contract.get("path")
    expected = contract.get("sha256")
    if (
        not isinstance(rel, str)
        or not rel
        or not isinstance(expected, str)
        or not _SHA256.fullmatch(expected)
    ):
        problems.append(f"invalid contract-file authority: {contract}")
        return
    path = root / rel
    if not path.is_file():
        problems.append(f"contract file missing: {rel}")
        return
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected != actual or contract.get("bytes") != path.stat().st_size:
        problems.append(
            f"contract file drift: {rel} expected {expected}, actual {actual}"
        )


def _valid_bridge(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and set(row) == _BRIDGE_FIELDS
        and all(isinstance(row[key], str) and row[key] for key in _BRIDGE_FIELDS)
        and bool(_PACKAGE.fullmatch(row["owner_package"]))
        and row["removal_package"] == "PKG-13-FACADES"
        and row["old_origin"] != row["new_origin"]
    )


def _target_key(row: dict[str, Any]) -> _TargetKey:
    return (row["surface"], row["path"], row["public_name"], row["target_sha256"])


def _surface_targets(
    python_surface: list[dict[str, Any]], frontend_surface: list[dict[str, Any]],
) -> dict[_TargetKey, dict[str, Any]]:
    targets: dict[_TargetKey, dict[str, Any]] = {}
    for row in python_surface:
        path = row["path"]
        occurrences: dict[str, int] = {}
        for public_name in row["public_names"]:
            descriptor = {
                "surface": "python",
                "path": path,
                "public_name": public_name,
                "public_signature": row["public_signatures"][public_name],
                "import_origins": [
                    origin
                    for origin in row["import_origins"]
                    if origin["public_name"] == public_name
                ],
            }
            descriptor_key = _canonical(descriptor)
            occurrences[descriptor_key] = occurrences.get(descriptor_key, 0) + 1
            descriptor["occurrence"] = occurrences[descriptor_key]
            digest = hashlib.sha256(
                _canonical(descriptor).encode("utf-8")
            ).hexdigest()
            key = ("python", path, public_name, digest)
            targets[key] = descriptor
    for row in frontend_surface:
        path = row["path"]
        occurrences = {}
        for declaration in row["public_declarations"]:
            descriptor = {
                "surface": "frontend",
                "path": path,
                "public_name": declaration["name"],
                "declaration": declaration,
            }
            descriptor_key = _canonical(descriptor)
            occurrences[descriptor_key] = occurrences.get(descriptor_key, 0) + 1
            descriptor["occurrence"] = occurrences[descriptor_key]
            digest = hashlib.sha256(
                _canonical(descriptor).encode("utf-8")
            ).hexdigest()
            key = ("frontend", path, declaration["name"], digest)
            targets[key] = descriptor
    return targets


def _transition_authority(
    baseline: dict[str, Any], problems: list[str],
) -> dict[_TargetKey, dict[str, Any]]:
    rows = baseline.get("additive_transitions")
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) for row in rows
    ):
        problems.append("additive_transitions must be an exact object list")
        return {}
    if rows != sorted(rows, key=_canonical):
        problems.append("additive_transitions must be canonically sorted")
    result: dict[_TargetKey, dict[str, Any]] = {}
    for row in rows:
        if (
            set(row) != _TRANSITION_FIELDS
            or row.get("surface") not in _SURFACES
            or any(
                not isinstance(row.get(key), str) or not row[key]
                for key in _TRANSITION_FIELDS
            )
            or not _SHA256.fullmatch(row["target_sha256"])
            or not _PACKAGE.fullmatch(row["owner_package"])
        ):
            problems.append(f"invalid additive transition metadata: {row}")
            continue
        key = _target_key(row)
        if key in result:
            problems.append(f"duplicate additive transition target: {key}")
            continue
        result[key] = row
    return result


def _bridge_authority(
    baseline: dict[str, Any], problems: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = baseline.get("compatibility_bridges")
    if not isinstance(rows, list) or not all(_valid_bridge(row) for row in rows):
        problems.append("compatibility_bridges has invalid explicit metadata")
        return {}
    if rows != sorted(rows, key=_canonical):
        problems.append("compatibility_bridges must be canonically sorted")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["path"], row["public_name"])
        if key in result:
            problems.append(f"duplicate compatibility bridge target: {key}")
            continue
        result[key] = row
    return result


def _initializer_module(path: Any) -> str | None:
    if (
        not isinstance(path, str)
        or not path.startswith("packages/")
        or "/src/" not in path
        or not path.endswith("/__init__.py")
    ):
        return None
    return path.split("/src/", 1)[1][:-3].replace("/", ".").removesuffix(".__init__")


def _from_origin(source: str, origin: dict[str, Any]) -> str | None:
    module = origin.get("module")
    name = origin.get("name")
    level = origin.get("level")
    if (
        not isinstance(name, str)
        or not name
        or module is not None
        and not isinstance(module, str)
        or not isinstance(level, int)
        or level < 0
    ):
        return None
    if not level:
        resolved = module or ""
        return f"{resolved}.{name}" if resolved else name
    parts = source.split(".")
    parent_count = level - 1
    if parent_count > len(parts):
        return None
    parts = parts[: len(parts) - parent_count] if parent_count else parts
    if module:
        parts.extend(module.split("."))
    resolved = ".".join(parts)
    return f"{resolved}.{name}" if resolved else name


def _target_origin(target: dict[str, Any]) -> str | None:
    origins = target.get("import_origins")
    source = _initializer_module(target.get("path"))
    if (
        not isinstance(origins, list)
        or len(origins) != 1
        or source is None
        or not isinstance(origins[0], dict)
    ):
        return None
    origin = origins[0]
    module = origin.get("module")
    if origin.get("kind") == "import":
        return module if isinstance(module, str) and module else None
    if origin.get("kind") != "from":
        return None
    return _from_origin(source, origin)


def _check_metadata(
    baseline: dict[str, Any], python_surface: list[dict[str, Any]],
    frontend_surface: list[dict[str, Any]], problems: list[str],
) -> None:
    try:
        targets = _surface_targets(python_surface, frontend_surface)
    except ValueError as error:
        problems.append(str(error))
        return
    transitions = _transition_authority(baseline, problems)
    bridges = _bridge_authority(baseline, problems)
    stale_transitions = sorted(set(transitions) - set(targets))
    if stale_transitions:
        problems.append(
            f"additive transition target is not public: {stale_transitions}"
        )
    for (path, public_name), bridge in bridges.items():
        identity = ("python", path, public_name)
        keys = [key for key in targets if key[:3] == identity]
        if len(keys) != 1:
            problems.append(
                "compatibility bridge no longer preserves public name: "
                f"{[bridge]}"
            )
            continue
        key = keys[0]
        actual_origin = _target_origin(targets[key])
        if actual_origin != bridge["new_origin"]:
            problems.append(
                "compatibility bridge new_origin does not match live origin: "
                f"{bridge}; actual={actual_origin}"
            )
        transition = transitions.get(key)
        if (
            transition is None
            or transition["owner_package"] != bridge["owner_package"]
        ):
            problems.append(
                "compatibility bridge requires a same-owner additive "
                f"transition: {bridge}"
            )


def _stored_surface(baseline: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = baseline.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError(f"accepted {key} authority is missing")
    return rows


def _check_python_change(
    identity: _TargetIdentity, added: set[_TargetKey], removed: set[_TargetKey],
    previous_targets: dict[_TargetKey, dict[str, Any]],
    current_targets: dict[_TargetKey, dict[str, Any]],
    current_bridges: dict[tuple[str, str], dict[str, Any]], problems: list[str],
) -> None:
    old_keys = [key for key in removed if key[:3] == identity]
    new_keys = [key for key in added if key[:3] == identity]
    bridge = current_bridges.get((identity[1], identity[2]))
    if len(old_keys) != 1 or len(new_keys) != 1 or bridge is None:
        problems.append(
            f"Python incompatible change requires one bridge: {identity}"
        )
        return
    old_target = previous_targets[old_keys[0]]
    new_target = current_targets[new_keys[0]]
    if old_target["public_signature"] != new_target["public_signature"]:
        problems.append(f"compatibility bridge cannot change signature: {identity}")
    old_origin = _target_origin(old_target)
    new_origin = _target_origin(new_target)
    if (
        old_origin is None
        or new_origin is None
        or old_origin == new_origin
        or bridge["old_origin"] != old_origin
        or bridge["new_origin"] != new_origin
    ):
        problems.append(
            "compatibility bridge origins do not match accepted/current "
            f"surfaces: {identity}; old={old_origin}; new={new_origin}"
        )


def _check_python_changes(
    changed: set[_TargetIdentity], added: set[_TargetKey], removed: set[_TargetKey],
    previous_targets: dict[_TargetKey, dict[str, Any]],
    current_targets: dict[_TargetKey, dict[str, Any]],
    current_bridges: dict[tuple[str, str], dict[str, Any]], problems: list[str],
) -> None:
    for identity in sorted(changed):
        if identity[0] == "frontend":
            problems.append(f"frontend incompatible change cannot be bridged: {identity}")
        else:
            _check_python_change(
                identity, added, removed, previous_targets,
                current_targets, current_bridges, problems,
            )


def _check_transition_delta(
    previous: dict[str, Any], inventory: dict[str, Any],
    added: set[_TargetKey], current_keys: set[_TargetKey], problems: list[str],
) -> None:
    previous_rows = _transition_authority(previous, problems)
    current_rows = _transition_authority(inventory, problems)
    preserved = set(previous_rows) & current_keys
    expected = preserved | added
    if set(current_rows) != expected:
        problems.append(
            "additive transitions do not exactly authorize regeneration: "
            f"missing={sorted(expected - set(current_rows))}, "
            f"extra={sorted(set(current_rows) - expected)}"
        )
    for key in preserved:
        if current_rows.get(key) != previous_rows[key]:
            problems.append(f"accepted additive transition changed: {key}")


def _check_bridge_delta(
    previous: dict[str, Any], inventory: dict[str, Any],
    current_identities: set[_TargetIdentity], changed_python: set[_TargetIdentity],
    problems: list[str],
) -> None:
    previous_rows = _bridge_authority(previous, problems)
    current_rows = _bridge_authority(inventory, problems)
    preserved = {
        ("python", path, name)
        for path, name in previous_rows
        if ("python", path, name) in current_identities
        and ("python", path, name) not in changed_python
    }
    expected = preserved | changed_python
    current = {("python", path, name) for path, name in current_rows}
    if current != expected:
        problems.append(
            "compatibility bridges do not exactly authorize regeneration: "
            f"missing={sorted(expected - current)}, "
            f"extra={sorted(current - expected)}"
        )
    for _, path, name in preserved:
        key = (path, name)
        if current_rows.get(key) != previous_rows[key]:
            problems.append(f"accepted compatibility bridge changed: {key}")


def _check_regeneration_delta(
    previous: dict[str, Any], inventory: dict[str, Any],
    previous_targets: dict[_TargetKey, dict[str, Any]],
    current_targets: dict[_TargetKey, dict[str, Any]],
    problems: list[str],
) -> None:
    previous_keys = set(previous_targets)
    current_keys = set(current_targets)
    added = current_keys - previous_keys
    removed = previous_keys - current_keys
    current_identities = {key[:3] for key in current_keys}
    deleted = [key for key in removed if key[:3] not in current_identities]
    if deleted:
        problems.append(
            f"public API regeneration deletes targets: {sorted(deleted)}"
        )
    changed = {key[:3] for key in removed if key[:3] in current_identities}
    changed_python = {item for item in changed if item[0] == "python"}
    current_bridges = _bridge_authority(inventory, problems)
    _check_python_changes(
        changed, added, removed, previous_targets,
        current_targets, current_bridges, problems,
    )
    _check_transition_delta(previous, inventory, added, current_keys, problems)
    _check_bridge_delta(
        previous, inventory, current_identities, changed_python, problems,
    )


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
    _check_regeneration_delta(
        accepted, candidate, accepted_targets,
        candidate_targets, problems
    )
    return [f"immutable public API authority: {problem}" for problem in problems]


def _check_diagram_transition(
    transition: dict[str, Any], root: Path,
    contracts: dict[str, dict[str, Any]], problems: list[str],
) -> None:
    required = {
        "owner",
        "reason",
        "from_sha256",
        "to_sha256",
        "from_parent",
        "path",
    }
    if set(transition) != required:
        problems.append(f"diagram transition schema mismatch: {transition}")
        return
    if (
        transition["owner"] != "PKG-02-GATE"
        or not transition["reason"]
        or transition["from_sha256"] != _ACCEPTED_DIAGRAM_SHA256
        or transition["from_parent"]
        != "1cf00dbe194a2a276ea1fd17ab74589355f2e0dc"
        or transition["path"] != "docs/architecture.generated.md"
    ):
        problems.append(f"diagram transition accepted authority drift: {transition}")
        return
    path = root / transition["path"]
    actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    contract = contracts.get(transition["path"], {})
    if (
        transition["to_sha256"] != actual
        or contract.get("sha256") != actual
        or transition["to_sha256"] == transition["from_sha256"]
    ):
        problems.append("diagram transition target/contract bytes do not agree")


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
    _compare_exact(
        "Python public surface", stored_python, python_surface, problems
    )
    _compare_exact(
        "frontend compiler surface", stored_frontend, frontend_surface, problems
    )
    problems.extend(
        _immutable_delta_problems(
            resolved_root, baseline, stored_python, stored_frontend
        )
    )
    contracts = _exact_rows(
        baseline, "contract_files", "contract_files", problems
    )
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
    _check_metadata(baseline, python_surface, frontend_surface, problems)
    return {
        "ok": not problems,
        "problems": problems,
        "initializer_count": len(python_surface),
        "contract_file_count": len(contracts),
        "frontend_module_count": len(frontend_surface),
    }


def _contract_snapshot(root: Path, paths: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rel in sorted(paths):
        path = root / rel
        if not path.is_file():
            raise FileNotFoundError(f"contract file missing: {rel}")
        rows.append(
            {
                "path": rel,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
        )
    return rows


def _regeneration_prior(root: Path, supplied: str | None) -> tuple[str, dict[str, Any]]:
    prior, source_blob, _ = _source_prior(root, supplied)
    head = _git_output(root, "rev-parse", "HEAD").decode().strip()
    if supplied != head:
        raise RuntimeError("supplied source_identity must equal HEAD")
    working_blob = (root / _AUTHORITY_PATH).read_bytes()
    if working_blob != source_blob:
        raise RuntimeError("working prewrite authority must equal the source commit blob")
    return supplied, prior


def _regenerated_contracts(previous: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    rows = previous.get("contract_files")
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and isinstance(row.get("path"), str) for row in rows
    ):
        raise RuntimeError("accepted contract-file path authority is missing")
    paths = [row["path"] for row in rows]
    if (
        paths != sorted(paths) or len(paths) != len(set(paths))
        or "docs/architecture.generated.md" not in paths
    ):
        raise RuntimeError("accepted contract-file paths are invalid")
    actual = _contract_snapshot(root, paths)
    accepted = {row["path"]: row for row in rows}
    changed = [row for row in actual if row["path"] != "docs/architecture.generated.md"
               and row != accepted[row["path"]]]
    if changed:
        raise RuntimeError(f"non-diagram contract bytes cannot be rebaselined: {changed}")
    return actual


def _updated_diagram_transitions(previous: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    rows = previous.get("diagram_transitions")
    if (
        not isinstance(rows, list) or len(rows) != 1
        or not isinstance(rows[0], dict)
        or rows[0].get("from_sha256") != _ACCEPTED_DIAGRAM_SHA256
    ):
        raise RuntimeError("accepted diagram transition authority is missing")
    transition = dict(rows[0])
    diagram = root / "docs" / "architecture.generated.md"
    transition["to_sha256"] = hashlib.sha256(diagram.read_bytes()).hexdigest()
    return [transition]


def regenerate_public_api(
    root: Path | None = None,
    source_identity: str | None = None,
    *,
    additive_transitions: list[dict[str, Any]] | None = None,
    compatibility_bridges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved_root = REPO_ROOT if root is None else root
    source_identity, prior = _regeneration_prior(resolved_root, source_identity)
    previous = load_public_api(resolved_root)
    previous_python = _stored_surface(previous, "python_initializers")
    previous_frontend = _stored_surface(previous, "frontend_modules")
    accepted_problems: list[str] = []
    _check_metadata(
        previous,
        previous_python,
        previous_frontend,
        accepted_problems,
    )
    _check_regeneration_delta(
        prior,
        previous,
        _surface_targets(
            _stored_surface(prior, "python_initializers"),
            _stored_surface(prior, "frontend_modules"),
        ),
        _surface_targets(previous_python, previous_frontend),
        accepted_problems,
    )
    if accepted_problems:
        raise RuntimeError(
            "accepted public API metadata is invalid: "
            + "; ".join(accepted_problems)
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
        "additive_transitions": (
            previous.get("additive_transitions", [])
            if additive_transitions is None
            else additive_transitions
        ),
        "compatibility_bridges": (
            previous.get("compatibility_bridges", [])
            if compatibility_bridges is None
            else compatibility_bridges
        ),
        "compatibility_rule": (
            "Any deleted or renamed public name, origin, signature, frontend "
            "export/type/schema, or contract byte fails. Additions require an "
            "owning-package baseline transition; moved implementations retain "
            "the old public name through an explicit bridge until PKG-13."
        ),
    }
    problems: list[str] = []
    _check_metadata(inventory, python_surface, frontend_surface, problems)
    _check_regeneration_delta(
        previous,
        inventory,
        _surface_targets(previous_python, previous_frontend),
        _surface_targets(python_surface, frontend_surface),
        problems,
    )
    if problems:
        raise RuntimeError(
            "public API regeneration rejected: " + "; ".join(problems)
        )
    output = resolved_root / "architecture" / "public-api.json"
    output.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    return {
        "written": str(output),
        "initializer_count": len(inventory["python_initializers"]),
        "frontend_module_count": len(inventory["frontend_modules"]),
        "contract_file_count": len(inventory["contract_files"]),
    }
