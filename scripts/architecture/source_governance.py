"""Source-discovered collaborator, aggregate, route, and observation gates.

Registries freeze exact accepted legacy facts; they never define the discovery
universe.  Every tracked non-test Python source is inspected so a new class,
aggregate, or route cannot opt out by omitting a registry row.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import observation_governance, python_scan
from .policy import load_ownership, load_policy

LEGACY_DISGUISED_COLLABORATORS: dict[tuple[str, str, str], dict[str, str]] = {}
FROZEN_GENERIC_COLLABORATORS: dict[tuple[str, str, str], str] = {
    (
        "packages/agent-server/src/disco/agent_server/share_service.py",
        "ShareService",
        "store",
    ): "Any",
    (
        "packages/agent-server/src/disco/agent_server/title_service.py",
        "TitleService",
        "store",
    ): "Any",
    (
        "packages/agent-server/src/disco/agent_server/verify/host.py",
        "HostWebAppVerifier",
        "client",
    ): "Any | None",
    (
        "packages/agent-server/src/disco/agent_server/verify/model_verifier.py",
        "ModelVerifier",
        "router",
    ): "Any",
    (
        "packages/core/src/disco/core/dod_evaluator.py",
        "LLMSubjectiveJudge",
        "router",
    ): "Any",
    (
        "packages/tools/src/disco/tools/sandbox/gvisor.py",
        "GvisorSandboxService",
        "client",
    ): "Any | None",
    (
        "packages/tools/src/disco/tools/sandbox/local.py",
        "LocalSandboxService",
        "client",
    ): "Any | None",
    (
        "packages/tools/src/disco/tools/sandbox/podman.py",
        "PodmanSandboxService",
        "client",
    ): "Any | None",
}
FROZEN_GENERIC_SCALAR_PARAMETERS: dict[tuple[str, str, str], str] = {
    (
        "packages/agent-server/src/disco/agent_server/preview_manager.py",
        "PreviewManager",
        "port_pool",
    ): "list[int] | None",
    (
        "packages/core/src/disco/core/loop/engine.py",
        "AgentLoop",
        "stop_hooks",
    ): "list[StopHook] | None",
    (
        "packages/core/src/disco/core/loop/engine.py",
        "AgentLoop",
        "planning_tools",
    ): "frozenset[str]",
}
_LEGACY_DISGUISE_FIELDS = {
    "path",
    "symbol",
    "parameter",
    "annotation",
    "disposition_id",
    "owner_package",
    "removal_package",
    "reason",
}
_DEPENDENCY_NAME_TOKEN = re.compile(
    r"^(?:clients?|collaborators?|dependenc(?:y|ies)|deps|ports?|"
    r"repositories|repository|routers?|runtimes?|services?|stores?|"
    r"transports?|verifiers?)$"
)
LegacyKey = tuple[str, str, str]
SourceTree = tuple[str, ast.Module]


def _tracked_source_trees(root: Path, label: str) -> tuple[list[SourceTree], list[str]]:
    trees: list[SourceTree] = []
    problems: list[str] = []
    for full in python_scan.tracked_python_files(root):
        path = full.relative_to(root).as_posix()
        if python_scan.is_test_path(path):
            continue
        try:
            tree = ast.parse(full.read_text(encoding="utf-8"), filename=path)
        except (SyntaxError, UnicodeDecodeError) as exc:
            problems.append(f"{label}: {path} is unparseable: {exc!r}")
            continue
        trees.append((path, tree))
    return trees, problems


def _legacy_row_key(row: Any) -> LegacyKey | None:
    if not isinstance(row, dict) or set(row) != _LEGACY_DISGUISE_FIELDS:
        return None
    if not all(isinstance(value, str) and value for value in row.values()):
        return None
    return (row["path"], row["symbol"], row["parameter"])


def _legacy_row_problem(index: int, row: Any) -> tuple[LegacyKey | None, str | None]:
    key = _legacy_row_key(row)
    if key is None:
        return None, f"legacy disguised collaborator row {index}: schema mismatch"
    expected = LEGACY_DISGUISED_COLLABORATORS.get(key)
    if expected is None:
        return key, f"new legacy disguised collaborator is forbidden: {key}"
    drift = {
        field: (expected[field], row[field])
        for field in expected
        if row[field] != expected[field]
    }
    if drift:
        return (
            key,
            f"legacy disguised collaborator metadata drift for {key}: {drift}",
        )
    return key, None


def _validated_legacy_disguises(
    ownership: dict[str, Any],
) -> tuple[set[LegacyKey], list[str]]:
    rows = ownership.get("legacy_disguised_collaborators")
    if not isinstance(rows, list):
        return set(), [
            "legacy disguised collaborator registry must be an object list"
        ]
    valid: set[LegacyKey] = set()
    seen: set[LegacyKey] = set()
    problems: list[str] = []
    for index, row in enumerate(rows):
        key, problem = _legacy_row_problem(index, row)
        if problem is not None:
            problems.append(problem)
        if key is None:
            continue
        if key in seen:
            problems.append(
                f"legacy disguised collaborator registry has duplicate tuple: {key}"
            )
            continue
        seen.add(key)
        if problem is None:
            valid.add(key)
    missing = sorted(set(LEGACY_DISGUISED_COLLABORATORS) - seen)
    if missing:
        problems.append(
            f"legacy disguised collaborator registry missing frozen tuples: {missing}"
        )
    return valid, problems


def _constructor_tree_problems(
    path: str,
    tree: ast.Module,
    cap: int,
    accepted: set[LegacyKey],
) -> tuple[
    list[str],
    dict[LegacyKey, dict[str, str]],
    set[LegacyKey],
]:
    problems: list[str] = []
    discovered: dict[LegacyKey, dict[str, str]] = {}
    seen_scalars: set[LegacyKey] = set()
    qnames = python_scan.build_qualified_name_map(tree)
    classes = (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    for cls in classes:
        if python_scan.is_protocol_with_zero_mutable_state(cls):
            continue
        if not python_scan.init_parameters(cls):
            continue
        end = int(cls.end_lineno or cls.lineno)
        symbol = qnames.get((cls.lineno, end, cls.name), cls.name)
        classified = python_scan.classify_init_parameters(cls)
        non_scalar = classified["collaborators"] + classified["disguised"]
        count = 0
        for param in non_scalar:
            key = (path, symbol, param["name"])
            if FROZEN_GENERIC_SCALAR_PARAMETERS.get(key) == param["annotation"]:
                seen_scalars.add(key)
            else:
                count += 1
        if count > cap:
            problems.append(
                f"constructor collaborator count: {path}:{symbol} "
                f"count {count} exceeds cap {cap}"
            )
        for param in classified["disguised"]:
            key = (path, symbol, param["name"])
            if key in seen_scalars:
                continue
            discovered[key] = param
            if key not in accepted:
                problems.append(
                    f"unregistered disguised collaborator {path}:{symbol}."
                    f"{param['name']} ({param['annotation']}) hides an "
                    "aggregate behind a generic type"
                )
    return problems, discovered, seen_scalars


def _stale_legacy_problems(
    registered: set[LegacyKey],
    discovered: dict[LegacyKey, dict[str, str]],
) -> list[str]:
    problems: list[str] = []
    for key in sorted(registered):
        param = discovered.get(key)
        if param is None:
            problems.append(
                "stale legacy disguised collaborator no longer exists or "
                f"is no longer disguised: {key}"
            )
            continue
        expected = LEGACY_DISGUISED_COLLABORATORS[key]
        if param["annotation"] != expected["annotation"]:
            problems.append(
                f"legacy disguised collaborator annotation drift for {key}: "
                f"{param['annotation']!r}"
            )
    return problems


def _stale_frozen_generic_problems(
    discovered: dict[LegacyKey, dict[str, str]],
) -> list[str]:
    problems: list[str] = []
    for key, annotation in sorted(FROZEN_GENERIC_COLLABORATORS.items()):
        param = discovered.get(key)
        if param is None:
            problems.append(
                f"stale frozen generic collaborator no longer exists: {key}"
            )
        elif param["annotation"] != annotation:
            problems.append(
                f"frozen generic collaborator annotation drift for {key}: "
                f"{param['annotation']!r}"
            )
    return problems


def _stale_frozen_scalar_problems(seen: set[LegacyKey]) -> list[str]:
    return [
        f"stale frozen generic scalar/config parameter: {key}"
        for key in sorted(set(FROZEN_GENERIC_SCALAR_PARAMETERS) - seen)
    ]


def check_constructor_collaborator_caps(root: Path) -> list[str]:
    """Enforce collaborator caps and exact source-discovered disguises."""
    ownership = load_ownership(root)
    policy = load_policy(root)
    cap = policy["collaborator_rules"]["constructor_collaborator_cap"]
    registered, problems = _validated_legacy_disguises(ownership)
    trees, parse_problems = _tracked_source_trees(
        root, "constructor collaborator scan"
    )
    problems.extend(parse_problems)
    discovered: dict[LegacyKey, dict[str, str]] = {}
    seen_scalars: set[LegacyKey] = set()
    accepted = registered | set(FROZEN_GENERIC_COLLABORATORS)
    for path, tree in trees:
        tree_problems, tree_discovered, tree_scalars = _constructor_tree_problems(
            path, tree, cap, accepted
        )
        problems.extend(tree_problems)
        discovered.update(tree_discovered)
        seen_scalars.update(tree_scalars)
    problems.extend(_stale_legacy_problems(registered, discovered))
    problems.extend(_stale_frozen_generic_problems(discovered))
    problems.extend(_stale_frozen_scalar_problems(seen_scalars))
    return problems


def _is_collection_value(
    value: ast.AST,
    aliases: dict[str, str] | None = None,
) -> bool:
    if isinstance(value, ast.Call):
        return (
            python_scan.resolve_import_alias(
                python_scan.annotation_name(value.func), aliases
            ).rsplit(".", 1)[-1] in {"frozenset", "set"}
            and len(value.args) == 1
            and _is_collection_value(value.args[0], aliases)
        )
    return isinstance(value, (ast.Set, ast.List, ast.Tuple))


def is_collection_assignment(
    node: ast.AST,
    aliases: dict[str, str] | None = None,
) -> bool:
    if isinstance(node, ast.Assign):
        return _is_collection_value(node.value, aliases)
    return (
        isinstance(node, ast.AnnAssign)
        and node.value is not None
        and _is_collection_value(node.value, aliases)
    )


def _assignment_target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [target.id for target in targets if isinstance(target, ast.Name)]


def _aggregate_elements(
    value: ast.AST,
    aliases: dict[str, str] | None = None,
) -> list[ast.expr] | None:
    if isinstance(value, ast.Call):
        if (
            python_scan.resolve_import_alias(
                python_scan.annotation_name(value.func), aliases
            ).rsplit(".", 1)[-1] not in {"frozenset", "set"}
            or len(value.args) != 1
        ):
            return None
        return _aggregate_elements(value.args[0], aliases)
    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        return list(value.elts)
    return None


def _dependency_like_name(name: str) -> bool:
    tail = name.rsplit(".", 1)[-1]
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", tail)
    tokens = re.split(r"[^A-Za-z0-9]+", separated)
    return any(
        _DEPENDENCY_NAME_TOKEN.fullmatch(re.sub(r"\d+$", "", token.lower()))
        for token in tokens
        if token
    )


def _declarative_collection_name(name: str) -> bool:
    tokens = {
        token.lower()
        for token in re.split(r"[^A-Za-z0-9]+", name)
        if token
    }
    return not tokens.isdisjoint({"fields", "keys", "kinds", "names", "types"})


def _dependency_like_element(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        name = python_scan.annotation_name(node.func)
    elif isinstance(node, (ast.Name, ast.Attribute)):
        name = python_scan.annotation_name(node)
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        name = node.value
    else:
        return False
    return _dependency_like_name(name.rsplit(".", 1)[-1])


def is_dependency_aggregate_assignment(
    node: ast.AST,
    aliases: dict[str, str] | None = None,
) -> bool:
    """Use target/member source semantics, never registry membership alone."""
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return False
    if not is_collection_assignment(node, aliases):
        return False
    targets = _assignment_target_names(node)
    if targets and all(name == "__all__" for name in targets):
        return False
    if targets and all(_declarative_collection_name(name) for name in targets):
        return False
    if any(_dependency_like_name(name) for name in targets):
        return True
    value = node.value
    elements = _aggregate_elements(value, aliases) if value is not None else None
    return bool(elements) and any(_dependency_like_element(item) for item in elements)


def _dependency_member_count(
    node: ast.Assign | ast.AnnAssign,
    aliases: dict[str, str],
) -> int:
    value = node.value
    elements = _aggregate_elements(value, aliases) if value is not None else None
    if elements is None:
        return 0
    if any(_dependency_like_name(name) for name in _assignment_target_names(node)):
        return len(elements)
    return sum(_dependency_like_element(item) for item in elements)


def dependency_aggregate_assignments(
    tree: ast.Module,
) -> Iterable[tuple[str, ast.Assign | ast.AnnAssign, int]]:
    """Yield qualified module/class aggregates, excluding function locals."""
    aliases = python_scan.effect_import_aliases(tree)

    def walk(
        body: list[ast.stmt],
        prefix: str = "",
    ) -> Iterable[tuple[str, ast.Assign | ast.AnnAssign, int]]:
        for statement in body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                if is_dependency_aggregate_assignment(statement, aliases):
                    for name in _assignment_target_names(statement):
                        yield (
                            f"{prefix}.{name}" if prefix else name,
                            statement,
                            _dependency_member_count(statement, aliases),
                        )
            elif isinstance(statement, ast.ClassDef):
                nested = f"{prefix}.{statement.name}" if prefix else statement.name
                yield from walk(statement.body, nested)

    yield from walk(tree.body)


def _collection_assignments(
    tree: ast.Module,
) -> Iterable[tuple[str, ast.Assign | ast.AnnAssign]]:
    aliases = python_scan.effect_import_aliases(tree)

    def walk(body: list[ast.stmt]) -> Iterable[tuple[str, ast.Assign | ast.AnnAssign]]:
        for statement in body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                if is_collection_assignment(statement, aliases):
                    for name in _assignment_target_names(statement):
                        yield name, statement
            elif isinstance(statement, ast.ClassDef):
                yield from walk(statement.body)

    yield from walk(tree.body)


def _registered_aggregate_problems(
    root: Path,
    ownership: dict[str, Any],
) -> list[str]:
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in ownership.get("dependency_aggregates", []):
        path = row["path"]
        symbol = row["symbol"]
        key = (path, symbol)
        if key in seen:
            problems.append(f"duplicate dependency aggregate registry tuple: {key}")
            continue
        seen.add(key)
        full = root / path
        if not full.is_file():
            problems.append(f"dependency aggregate {symbol}: file absent: {path}")
            continue
        try:
            tree = ast.parse(full.read_text(encoding="utf-8"), filename=path)
        except (SyntaxError, UnicodeDecodeError) as exc:
            problems.append(
                f"dependency aggregate {symbol}: file unparseable: {exc!r}"
            )
            continue
        matches = [
            node
            for name, node in _collection_assignments(tree)
            if name == symbol
        ]
        if len(matches) != 1:
            problems.append(
                f"dependency aggregate {symbol}: expected one statically "
                f"countable assignment in {path}, found {len(matches)}"
            )
    return problems


def check_dependency_aggregate_caps(root: Path) -> list[str]:
    """Discover and cap every module/class dependency aggregate."""
    ownership = load_ownership(root)
    cap = load_policy(root)["collaborator_rules"]["dependency_aggregate_member_cap"]
    problems = _registered_aggregate_problems(root, ownership)
    trees, parse_problems = _tracked_source_trees(root, "dependency aggregate scan")
    problems.extend(parse_problems)
    for path, tree in trees:
        for symbol, _node, count in dependency_aggregate_assignments(tree):
            if count > cap:
                problems.append(
                    f"dependency aggregate {path}:{symbol}: member count "
                    f"{count} exceeds cap {cap}"
                )
    return problems


def check_unregistered_route_effects(
    root: Path,
    registered: set[tuple[str, str]],
) -> list[str]:
    """Reject direct effects in every decorated route, regardless of registry."""
    problems: list[str] = []
    trees, parse_problems = _tracked_source_trees(root, "route effect scan")
    problems.extend(parse_problems)
    for path, tree in trees:
        aliases = python_scan.effect_import_aliases(tree)
        routes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and python_scan.is_route(node, path, aliases)
        ]
        for name, count in sorted(Counter(node.name for node in routes).items()):
            if count > 1 and (path, name) in registered:
                problems.append(
                    f"route effect owner: duplicate decorated route source identity "
                    f"in {path}: {name!r} has {count} occurrences"
                )
        for node in routes:
            if (
                python_scan.has_direct_effect_calls(
                    node, python_scan.effect_import_aliases(tree, node)
                )
                and (path, node.name) not in registered
            ):
                problems.append(
                    f"route effect owner: unregistered direct effects in "
                    f"{path}: ['{node.name}']"
                )
    return problems


def check_observations_agreement(
    root: Path,
    ts_result: dict[str, Any] | None = None,
) -> list[str]:
    """Delegate exact source-evidence validation to the sealed helper."""
    return observation_governance.check_observations_agreement(root, ts_result)
