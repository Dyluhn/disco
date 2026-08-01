"""Shared fixtures and helpers for the architecture gate test suite.

This module is NOT a package init. ``tests/architecture`` stays non-package.
Each test module inserts this directory onto ``sys.path`` and imports helpers
from ``_helpers``.

All mutation tests build git-initialized temporary trees so the stable
``check_*(root=Path)`` boundary is exercised against realistic fixtures, not
against registry constants or source-text searches.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Insert scripts/ so ``from architecture import ...`` resolves in every test.
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from architecture import source_governance  # noqa: E402


def write(path: Path, content: str) -> Path:
    """Write a file, creating parent directories, and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def make_temp_repo() -> tempfile.TemporaryDirectory:
    """Create a temp directory with ``git init`` and return the context.

    The caller is responsible for calling ``.cleanup()``. Use the
    ``temp_repo`` fixture in conftest-style tests for automatic cleanup.
    """
    tmp = tempfile.TemporaryDirectory(prefix="arch-gate-test-")
    root = Path(tmp.name)
    subprocess.run(["git", "init", str(root)], capture_output=True, check=True)
    # Set a minimal git identity so commits work.
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@test.test"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"],
        capture_output=True,
        check=True,
    )
    # A commit may otherwise detach automatic maintenance that outlives the
    # test and repopulates .git/objects while TemporaryDirectory removes it.
    for key, value in (("gc.auto", "0"), ("maintenance.auto", "false")):
        subprocess.run(
            ["git", "-C", str(root), "config", key, value],
            capture_output=True,
            check=True,
        )
    return tmp


def git_add(root: Path, *paths: str) -> None:
    """Git-add one or more files in a temp repo."""
    subprocess.run(
        ["git", "-C", str(root), "add", *paths], capture_output=True, check=True
    )


def git_commit(root: Path, message: str = "test") -> None:
    """Create a git commit in a temp repo."""
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", message, "--allow-empty"],
        capture_output=True,
        check=True,
    )


def write_and_track(root: Path, rel: str, content: str) -> Path:
    """Write a file under a temp repo, git-add it, and return its full path."""
    path = root / rel
    write(path, content)
    git_add(root, rel)
    return path


def make_minimal_arch_tree(root: Path) -> None:
    """Write a minimal architecture/ directory with the required JSON files.

    This gives temp repos enough structure for checkers that load
    ``architecture/*.json`` to function without the full sealed baseline.
    """
    arch = root / "architecture"
    arch.mkdir(parents=True, exist_ok=True)
    write(arch / "policy.json", json.dumps({
        "schema": "disclaude-architecture-policy-v1",
        "source_identity": "test",
        "roots": {"python": ["packages/", "harness/", "scripts/", "tests/"],
                  "typescript": ["frontend/src/"]},
        "logical_limits": {
            "python_production_module": 700, "python_class": 350,
            "python_callable": 100, "python_callable_mccabe": 15,
            "python_service_public_methods": 12, "python_route_handler": 80,
            "python_composition_root": 300,
            "python_constructor_collaborators": 10,
            "python_dependency_aggregate_members": 10,
            "harness_oracle_module": 400, "harness_module": 700,
            "test_module": 1200, "typescript_module": 500,
            "typescript_component": 250, "typescript_hook": 200,
            "typescript_callable_mccabe": 15,
        },
        "typed_classifications": {"protocol_non_violations": []},
        "collaborator_rules": {
            "constructor_collaborator_cap": 10,
            "dependency_aggregate_member_cap": 10,
            "scalar_config_excluded": True,
            "value_input_excluded": True,
            "disguise_patterns_rejected": ["dict", "Any", "Context", "Services", "Runtime", "Deps"],
        },
        "shrink_only_rules": {},
        "import_rules": {},
        "exact_exclusions": {"importlinter_exemptions": []},
    }))
    write(arch / "ownership.json", json.dumps({
        "schema": "disclaude-architecture-ownership-v1",
        "composition_roots": [],
        "dependency_aggregates": [],
        "effect_owners": [],
        "collaborator_classification": {
            "disguise_patterns_rejected": ["dict", "Any", "Context", "Services", "Runtime", "Deps"],
            "canonical_example": {
                "total_parameters": 0,
                "collaborator_count": 0,
                "scalar_config": [],
            },
        },
    }))
    write(arch / "contexts.json", json.dumps({
        "schema": "disclaude-architecture-contexts-v1",
        "package_layering": {"layers": [
            ["disco.app_server", "disco.agent_server"],
            ["disco.tools"], ["disco.retrieval"], ["disco.core"],
        ]},
        "bounded_contexts": {},
        "permitted_edges": [],
        "dm005_upward_edge_observations": [],
        "runtime_edges": [],
    }))
    write(arch / "generated.json", json.dumps({
        "schema": "disclaude-architecture-generated-proof-v1",
        "entries": [],
    }))
    write(arch / "debt.json", json.dumps([]))
    write(arch / "dispositions.json", json.dumps([]))
    write(arch / "disposition-ids.txt", "")
    write(arch / "observations.json", json.dumps([]))
    write(arch / "public-api.json", json.dumps({
        "schema": "disclaude-architecture-public-api-v1",
        "python_initializers": [],
        "contract_files": [],
    }))
    write(arch / "test-inventory.json", json.dumps({
        "schema": "disclaude-architecture-test-inventory-v1",
        "collected": {"counts": {}, "total": 0, "roots": {}},
        "mapping_static": {
            "python_static_test_id_count": 0,
            "typescript_static_test_id_count": 0,
            "python_test_file_count": 0,
            "typescript_test_file_count": 0,
            "markers": [], "fixtures": [],
        },
        "frontend_real_collection": {},
    }))


def dedent(text: str) -> str:
    """Dedent a multiline string."""
    return textwrap.dedent(text)


def assert_problem_contains(problems: list[str], *fragments: str) -> None:
    """Assert that at least one problem contains all the given fragments."""
    for problem in problems:
        if all(frag.lower() in problem.lower() for frag in fragments):
            return
    raise AssertionError(
        f"No problem contained all fragments {fragments!r}. Problems:\n"
        + "\n".join(f"  - {p}" for p in problems)
    )


def assert_rule_violated(violations: list[dict[str, Any]], rule: str) -> dict[str, Any]:
    """Assert that a specific rule appears in violations and return it."""
    matched = [v for v in violations if v["rule"] == rule]
    assert matched, (
        f"rule {rule!r} not found in violations. "
        f"Rules present: {[v['rule'] for v in violations]}"
    )
    return matched[0]


def write_frozen_constructor_fixtures(
    root: Path,
    generic_collaborators: dict[tuple[str, str, str], str],
    scalar_parameters: dict[tuple[str, str, str], str],
) -> None:
    """Materialize exact production constructor allowlists in a temp tree."""
    for (path, symbol, parameter), annotation in generic_collaborators.items():
        write_and_track(
            root,
            path,
            "from typing import Any\n\n"
            f"class {symbol}:\n"
            f"    def __init__(self, {parameter}: {annotation}) -> None:\n"
            f"        self._{parameter} = {parameter}\n",
        )
    scalar_classes: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for (path, symbol, parameter), annotation in scalar_parameters.items():
        scalar_classes.setdefault((path, symbol), []).append(
            (parameter, annotation)
        )
    for (path, symbol), parameters in scalar_classes.items():
        signature = ", ".join(
            f"{parameter}: {annotation}"
            for parameter, annotation in parameters
        )
        write_and_track(
            root,
            path,
            f"class {symbol}:\n"
            f"    def __init__(self, {signature}) -> None:\n"
            "        pass\n",
        )


def assert_generic_constructor_boundaries(
    root: Path,
    path: str,
    check: Any,
) -> None:
    """Exercise generic collaborator names, annotation precedence and Protocols."""
    names = [
        "service1", "client2", "store3", "repository4", "router5",
        "transport6", "verifier7", "service8", "client9", "store10",
        "repository11",
    ]
    for annotation in (
        "Any", "typing.Any", "dict[str, object]", "Mapping[str, object]",
        "list[object]", "typing.Optional[Any]", "Union[Any, None]",
    ):
        params = ", ".join(f"{name}: {annotation}" for name in names)
        write(
            root / path,
            "from typing import Any\n\n"
            f"class Big:\n    def __init__(self, {params}):\n        pass\n",
        )
        assert_problem_contains(check(root), "collaborator count", "11", "cap 10")
    services = ", ".join(f"service{index}: Service" for index in range(10))
    write(
        root / path,
        f"class Big:\n    def __init__(self, {services}, port: Port):\n"
        "        pass\n",
    )
    assert_problem_contains(check(root), "collaborator count", "11", "cap 10")
    write(
        root / path,
        f"class Big:\n    def __init__(self, {services}, port: int):\n"
        "        pass\n",
    )
    assert check(root) == []
    write(
        root / path,
        "@not_runtime_checkable\n"
        f"class Big:\n    def __init__(self, {services}, client: Client):\n"
        "        pass\n",
    )
    assert_problem_contains(check(root), "collaborator count", "11", "cap 10")


def assert_dependency_aggregate_alias_boundaries(
    root: Path,
    path: str,
    string_members: str,
    check: Any,
) -> None:
    """Exercise aliased constructors and mixed dependency/member metadata."""
    write(
        root / path,
        "from builtins import frozenset as frozen\n\n"
        f"DEPENDENCIES = frozen({{{string_members}}})\n",
    )
    assert_problem_contains(
        check(root), "DEPENDENCIES", "member count 11", "cap 10"
    )
    dependency_members = ", ".join(
        f'"dependency_{index}"' for index in range(11)
    )
    write(root / path, f'GROUP = [{dependency_members}, "metadata"]\n')
    assert_problem_contains(check(root), "GROUP", "member count 11", "cap 10")


def assert_route_decorator_boundaries(root: Path, path: str, check: Any) -> None:
    """Exercise exact HTTP decorator identities, direct imports and substrings."""
    for method in ("head", "options", "trace", "api_route", "route"):
        write(
            root / path,
            dedent(f"""\
                import subprocess

                @router.{method}("/exec")
                def handler(request):
                    subprocess.run(["echo", "hi"])
                    return {{"ok": True}}
                """),
        )
        assert_problem_contains(check(root), "unregistered direct effects", "handler")
    for method in ("get", "options", "api_route"):
        write(
            root / path,
            dedent(f"""\
                import subprocess
                from framework import {method}

                @{method}("/exec")
                def handler(request):
                    subprocess.run(["echo", "hi"])
                    return {{"ok": True}}
                """),
        )
        assert_problem_contains(check(root), "unregistered direct effects", "handler")
    write(
        root / path,
        dedent("""\
            import subprocess

            @router.forget("/exec")
            def handler(request):
                subprocess.run(["echo", "hi"])
                return {"ok": True}
            """),
    )
    assert check(root) == []


def assert_observation_evidence_boundaries(
    root: Path,
    first: str,
    disposition: dict[str, Any],
    observation: dict[str, Any],
    check: Any,
    scan_typescript: Any,
    write_context: Any,
) -> None:
    """Exercise prose fail-closure and compiler-backed TypeScript line spans."""
    location = f"{first}:Fake Symbol:1-2"
    disposition["location"] = location
    observation["location"] = location
    write(root / "architecture/dispositions.json", json.dumps([disposition]))
    write(root / "architecture/observations.json", json.dumps([observation]))
    assert_problem_contains(
        check(root), "unapproved descriptive observation clause", "Fake Symbol"
    )
    write_context(root)
    ts_path = "frontend/src/api/dummy.ts"
    location = f"{ts_path}:CommentOnly:1-2"
    disposition["location"] = location
    observation["location"] = location
    write(
        root / ts_path,
        "// export interface CommentOnly {}\nexport const dummy = 1;\n",
    )
    write(root / "architecture/dispositions.json", json.dumps([disposition]))
    write(root / "architecture/observations.json", json.dumps([observation]))
    problems = check(root, scan_typescript(root))
    assert_problem_contains(problems, "symbol CommentOnly", "not found", ts_path)
    location = f"{ts_path}:Declared:1-1"
    disposition["location"] = location
    observation["location"] = location
    write(
        root / ts_path,
        "// compiler declaration follows\nexport interface Declared {}\n",
    )
    write(root / "architecture/dispositions.json", json.dumps([disposition]))
    write(root / "architecture/observations.json", json.dumps([observation]))
    problems = check(root, scan_typescript(root))
    assert_problem_contains(
        problems, "symbol Declared", "outside registered ranges", ts_path
    )


def synthetic_legacy_row(
    path: str,
    *,
    symbol: str = "Bad",
    parameter: str = "services",
    annotation: str = "dict",
) -> dict[str, str]:
    """Return one well-formed ``legacy_disguised_collaborators`` row.

    The live registry is legitimately empty — the campaign removed every
    legacy disguise — so mutation tests must synthesize the row they intend
    to corrupt instead of borrowing ``legacy[0]``. Borrowing made those tests
    silently dependent on a defect existing in production, and they raised
    ``IndexError`` the first time the suite was ever run.
    """
    return {
        "path": path,
        "symbol": symbol,
        "parameter": parameter,
        "annotation": annotation,
        "disposition_id": "DM-001",
        "owner_package": "PKG-06-RUNTIME",
        "removal_package": "PKG-06-RUNTIME",
        "reason": (
            "DM-001 runtime back-reference exposes ConversationRuntime "
            "collaborators through self._rt"
        ),
    }


def write_budget_authority(
    root: Path,
    *,
    composition_facades: list[dict] | None = None,
    composition_roots: list[dict] | None = None,
    dependency_aggregates: list[dict] | None = None,
    effect_owners: list[dict] | None = None,
    legacy_disguised_collaborators: list[dict] | None = None,
) -> None:
    policy_data = json.loads((REPO_ROOT / "architecture/policy.json").read_text())
    ownership = json.loads(
        (REPO_ROOT / "architecture/ownership.json").read_text()
    )
    ownership["composition_facades"] = composition_facades or []
    ownership["composition_roots"] = composition_roots or []
    ownership["dependency_aggregates"] = dependency_aggregates or []
    ownership["effect_owners"] = effect_owners or []
    frozen_legacy = ownership["legacy_disguised_collaborators"]
    ownership["legacy_disguised_collaborators"] = (
        frozen_legacy
        if legacy_disguised_collaborators is None
        else legacy_disguised_collaborators
    )
    write(root / "architecture/policy.json", json.dumps(policy_data))
    write(root / "architecture/ownership.json", json.dumps(ownership))
    for row in frozen_legacy:
        write_and_track(
            root,
            row["path"],
            "from typing import Any\n\n"
            f"class {row['symbol']}:\n"
            "    def __init__(self, runtime: Any) -> None:\n"
            "        self._rt = runtime\n",
        )
    write_frozen_constructor_fixtures(
        root,
        source_governance.FROZEN_GENERIC_COLLABORATORS,
        source_governance.FROZEN_GENERIC_SCALAR_PARAMETERS,
    )
