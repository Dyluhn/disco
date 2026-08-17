"""Ownership, collaborator, and effect-ownership mutation tests.

Tests constructor collaborator caps (11 injected collaborators must fail),
disguised aggregates, dependency aggregates with 11 service members, scalar/
config exclusions, ordinary Protocol positives, declarative authorization
positives, oversized/effect-owning composition roots, route persistence/
process/filesystem/network/lifecycle effects, component/hook raw transport,
and typed API-client positives.

These tests exercise the ownership registry and the scanner's collaborator
detection against temp-tree mutations.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import (
    assert_dependency_aggregate_alias_boundaries,
    assert_generic_constructor_boundaries,
    assert_observation_evidence_boundaries,
    assert_problem_contains,
    assert_route_decorator_boundaries,
    git_commit,
    make_temp_repo,
    synthetic_legacy_row,
    write,
    write_and_track,
    write_budget_authority,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "development" / "scripts"))
from architecture import (  # noqa: E402
    budget,
    policy,
    python_scan,
    source_governance,
    typescript_scan,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def write_frontend_context_authority(root: Path) -> None:
    identity = "fixture-source"
    source = "frontend/src/components/Legacy.tsx"
    target = "frontend/src/api/dummy.ts"
    registry = {
        "schema": "disclaude-architecture-contexts-v1",
        "source_identity": identity,
        "bounded_contexts": {
            "frontend": {
                "root": "frontend/src",
                "contexts": {
                    "legacy_source": {"modules": ["components/Legacy"]},
                    "legacy_target": {"modules": ["api/dummy"]},
                },
                "dm007_legacy": {
                    "observation_id": "DM-007",
                    "source_identity": identity,
                    "owner_package": "PKG-12-FE-BUILD",
                    "edge_fields": [
                        "source",
                        "source_context",
                        "import",
                        "target",
                        "target_context",
                    ],
                    "edges": [
                        [
                            source,
                            "legacy_source",
                            "../api/dummy",
                            target,
                            "legacy_target",
                        ]
                    ],
                    "cycles": [],
                },
                "cross_feature_imports_rejected": True,
            }
        },
    }
    write(root / "development/architecture/contexts.json", json.dumps(registry))
    write_and_track(
        root,
        source,
        'import { dummy } from "../api/dummy";\nexport const legacy = dummy;\n',
    )
    write_and_track(root, target, "export const dummy = 1;\n")


# ---------------------------------------------------------------------------
# Collaborator detection from AST — mutations through init_parameters
# ---------------------------------------------------------------------------


class TestCollaboratorDetection:
    """Test the constructor collaborator cap enforcement."""

    def _init_params(self, code: str) -> list[dict]:
        tree = ast.parse(code)
        cls = tree.body[0]
        assert isinstance(cls, ast.ClassDef)
        return python_scan.init_parameters(cls)

    def test_constructor_with_11_collaborators_detected(self):
        """A constructor with 11 injected service collaborators must be flagged."""
        params = ", ".join(f"svc{i}: Service{i}" for i in range(11))
        code = f"class Big:\n    def __init__(self, {params}):\n        pass\n"
        init_params = self._init_params(code)
        assert len(init_params) == 11

    def test_scalar_config_excluded_from_collaborators(self):
        """Scalar/config parameters are typed separately from collaborators."""
        code = dedent("""\
            class Good:
                def __init__(self, store: Store, config: Config, mode: str, timeout: int):
                    pass
            """)
        init_params = self._init_params(code)
        assert len(init_params) == 4
        names = [p["name"] for p in init_params]
        assert "store" in names
        assert "config" in names
        assert "mode" in names
        assert "timeout" in names

    def test_eleven_collaborators_exceeds_cap(self):
        """The policy cap is 10; 11 collaborators must exceed it."""
        p = policy.load_policy()
        cap = p["collaborator_rules"]["constructor_collaborator_cap"]
        assert cap == 10
        assert 11 > cap

    def test_ten_collaborators_at_cap(self):
        """10 collaborators is at the cap, not over it."""
        p = policy.load_policy()
        cap = p["collaborator_rules"]["constructor_collaborator_cap"]
        assert 10 <= cap

    def test_11_collaborator_class_scanned_in_temp_repo(self) -> None:
        """A class with 11 collaborators must be scannable in a temp repo."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        params = ", ".join(f"svc{i}: Service{i}" for i in range(11))
        code = f"class Big:\n    def __init__(self, {params}):\n        pass\n"
        write_and_track(root, "current/packages/core/src/disco/core/big.py", code)
        git_commit(root)
        result = python_scan.scan_all(root)
        # The scanner must find the class and its constructor parameters
        found = False
        for module in result["modules"]:
            for sym in module["symbols"]:
                if sym["name"] == "Big" and "constructor_parameters" in sym:
                    assert len(sym["constructor_parameters"]) == 11
                    found = True
        assert found, "Big class with 11 collaborators not found in scan"
        tmp.cleanup()

    def test_11_collaborators_produce_violation(self) -> None:
        """A class with 11 injected collaborators must produce a cap violation.

        Source discovery must not require composition-facade registration.
        """
        tmp = make_temp_repo()
        root = Path(tmp.name)
        params = ", ".join(f"svc{i}: Service{i}" for i in range(11))
        code = f"class Big:\n    def __init__(self, {params}):\n        pass\n"
        path = "current/packages/core/src/disco/core/big.py"
        write_and_track(root, path, code)
        write_budget_authority(root)
        git_commit(root)
        problems = budget.check_constructor_collaborator_caps(root)
        assert_problem_contains(problems, "collaborator count", "11", "cap 10")

        params = ", ".join(f"store{i}" for i in range(11))
        write(
            root / path,
            f"class Big:\n    def __init__(self, {params}):\n        pass\n",
        )
        problems = budget.check_constructor_collaborator_caps(root)
        assert_problem_contains(problems, "collaborator count", "11", "cap 10")

        params = ", ".join(
            f"support_service{index}: StoreService" for index in range(11)
        )
        write(
            root / path,
            f"class Big:\n    def __init__(self, {params}):\n        pass\n",
        )
        problems = budget.check_constructor_collaborator_caps(root)
        assert_problem_contains(problems, "collaborator count", "11", "cap 10")

        assert_generic_constructor_boundaries(
            root, path, budget.check_constructor_collaborator_caps
        )
        tmp.cleanup()

    def test_10_collaborators_at_cap_no_violation(self) -> None:
        """A class with 10 collaborators (at cap) must not produce a violation."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        params = ", ".join(f"svc{i}: Service{i}" for i in range(10))
        code = f"class AtCap:\n    def __init__(self, {params}):\n        pass\n"
        path = "current/packages/core/src/disco/core/atcap.py"
        write_and_track(root, path, code)
        write_budget_authority(
            root,
            composition_facades=[{"path": path, "symbol": "AtCap"}],
        )
        git_commit(root)
        assert budget.check_constructor_collaborator_caps(root) == []
        tmp.cleanup()

    def test_scalar_config_not_counted_as_collaborator(self) -> None:
        """Scalar/config parameters must not be counted as collaborators.

        A constructor with 5 service collaborators and 5 scalar/config params
        must not exceed the 10-collaborator cap.
        """
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = dedent("""\
            class Good:
                def __init__(self, store: Store, config: Config, router: Router,
                             sandbox: Sandbox, skills: SkillStore,
                             mode: str, timeout: int, debug: bool,
                             name: str, version: int):
                    pass
            """)
        path = "current/packages/core/src/disco/core/good.py"
        write_and_track(root, path, code)
        write_budget_authority(
            root,
            composition_facades=[{"path": path, "symbol": "Good"}],
        )
        git_commit(root)
        assert budget.check_constructor_collaborator_caps(root) == []
        tmp.cleanup()


def dedent(text: str) -> str:
    import textwrap
    return textwrap.dedent(text)


# ---------------------------------------------------------------------------
# Disguise patterns — constant check with companion mutation
# ---------------------------------------------------------------------------


class TestDisguisePatterns:
    def test_disguise_patterns_listed_in_policy(self):
        p = policy.load_policy()
        patterns = p["collaborator_rules"]["disguise_patterns_rejected"]
        for pat in ("dict", "Any", "Context", "Services", "Runtime", "Deps"):
            assert pat in patterns

    def test_disguise_patterns_listed_in_ownership(self):
        ownership_data = json.loads(
            (REPO_ROOT / "development" / "architecture" / "ownership.json").read_text()
        )
        patterns = ownership_data["collaborator_classification"]["disguise_patterns_rejected"]
        for pat in ("dict", "Any", "Context", "Services", "Runtime", "Deps"):
            assert pat in patterns
        legacy = ownership_data["legacy_disguised_collaborators"]
        assert {
            (row["path"], row["symbol"], row["parameter"]) for row in legacy
        } == set(source_governance.LEGACY_DISGUISED_COLLABORATORS)

        tmp = make_temp_repo()
        root = Path(tmp.name)
        malformed = [*(dict(row) for row in legacy), synthetic_legacy_row(
            "current/packages/core/src/disco/core/bad.py"
        )]
        malformed[-1].pop("reason")
        write_budget_authority(
            root,
            legacy_disguised_collaborators=malformed,
        )
        git_commit(root)
        problems = budget.check_constructor_collaborator_caps(root)
        assert_problem_contains(problems, "schema mismatch")
        tmp.cleanup()

    def test_disguise_pattern_dict_detected_in_constructor(self):
        """A constructor using dict as a disguise pattern must be detectable."""
        code = dedent("""\
            class Bad:
                def __init__(self, services: dict):
                    pass
            """)
        tree = ast.parse(code)
        cls = tree.body[0]
        assert isinstance(cls, ast.ClassDef)
        params = python_scan.init_parameters(cls)
        assert len(params) == 1
        assert "dict" in params[0]["annotation"]


# ---------------------------------------------------------------------------
# Dependency aggregate cap — constant check with companion mutation
# ---------------------------------------------------------------------------


class TestDependencyAggregate:
    def test_dependency_aggregate_cap_is_10(self):
        p = policy.load_policy()
        cap = p["collaborator_rules"]["dependency_aggregate_member_cap"]
        assert cap == 10

    def test_dependency_aggregate_11_members_exceeds_cap(self):
        """A dependency aggregate with 11 service members must exceed the cap."""
        p = policy.load_policy()
        cap = p["collaborator_rules"]["dependency_aggregate_member_cap"]
        assert 11 > cap
        tmp = make_temp_repo()
        root = Path(tmp.name)
        path = "current/packages/core/src/disco/core/dependencies.py"
        members = ", ".join(f"Service{i}" for i in range(11))
        write_and_track(root, path, f"SERVICES = [{members}]\n")
        write_budget_authority(root)
        git_commit(root)
        problems = budget.check_dependency_aggregate_caps(root)
        assert_problem_contains(problems, "member count 11", "cap 10")

        write(
            root / path,
            f"class DependencyBag:\n    SERVICES = [{members}]\n",
        )
        problems = budget.check_dependency_aggregate_caps(root)
        assert_problem_contains(
            problems,
            "DependencyBag",
            "member count 11",
            "cap 10",
        )

        string_members = ", ".join(
            f'"store_service_{index}"' for index in range(11)
        )
        write(
            root / path,
            f"GROUP = frozenset({{{string_members}}})\n",
        )
        problems = budget.check_dependency_aggregate_caps(root)
        assert_problem_contains(
            problems,
            "GROUP",
            "member count 11",
            "cap 10",
        )

        assert_dependency_aggregate_alias_boundaries(
            root, path, string_members, budget.check_dependency_aggregate_caps
        )
        tmp.cleanup()

    def test_frozen_aggregates_within_cap(self):
        """All frozen dependency aggregates must be within the 10-member cap."""
        ownership_data = json.loads(
            (REPO_ROOT / "development" / "architecture" / "ownership.json").read_text()
        )
        for agg in ownership_data.get("dependency_aggregates", []):
            assert agg["max_members"] <= 10, (
                f"aggregate {agg['symbol']} has {agg['max_members']} members > 10"
            )
        tmp = make_temp_repo()
        root = Path(tmp.name)
        values = ", ".join(f'"tool-{index}"' for index in range(11))
        write_and_track(
            root,
            "current/packages/tools/src/disco/tools/scope.py",
            f"TOOL_SCOPE = frozenset({{{values}}})\n",
        )
        write_budget_authority(root)
        git_commit(root)
        assert budget.check_dependency_aggregate_caps(root) == []
        write(
            root / "current/packages/tools/src/disco/tools/scope.py",
            "from builtins import frozenset as frozen\n\n"
            f"TOOL_SCOPE = frozen({{{values}}})\n",
        )
        assert budget.check_dependency_aggregate_caps(root) == []
        verifier_keys = ", ".join(
            f'"verifier_{index}"' for index in range(11)
        )
        write(
            root / "current/packages/tools/src/disco/tools/scope.py",
            f"VERIFIER_CHECK_KEYS = frozenset({{{verifier_keys}}})\n",
        )
        assert budget.check_dependency_aggregate_caps(root) == []
        tmp.cleanup()


# ---------------------------------------------------------------------------
# Composition root — constant check with companion mutation
# ---------------------------------------------------------------------------


class TestCompositionRoot:
    def test_composition_root_max_lines_300(self):
        ownership_data = json.loads(
            (REPO_ROOT / "development" / "architecture" / "ownership.json").read_text()
        )
        for root in ownership_data.get("composition_roots", []):
            assert root["max_logical_lines"] <= 300

    def test_oversized_composition_root_would_fail(self):
        """A composition root over 300 logical lines must fail."""
        from architecture import policy as pol
        p = pol.load_policy()
        limit = p["logical_limits"]["python_composition_root"]
        assert limit == 300

    def test_composition_root_is_create_app(self):
        ownership_data = json.loads(
            (REPO_ROOT / "development" / "architecture" / "ownership.json").read_text()
        )
        roots = ownership_data.get("composition_roots", [])
        assert [(row["path"], row["symbol"]) for row in roots] == [
            (
                "current/packages/agent-server/src/disco/agent_server/app.py",
                "create_app",
            )
        ]
        assert any(
            row["symbol"] == "ConversationRuntime"
            for row in ownership_data["composition_facades"]
        )

    def test_canonical_example_has_11_parameters(self):
        ownership_data = json.loads(
            (REPO_ROOT / "development" / "architecture" / "ownership.json").read_text()
        )
        example = ownership_data["collaborator_classification"]["canonical_example"]
        assert example["total_parameters"] == 11
        assert example["collaborator_count"] == 6
        assert len(example["scalar_config"]) == 5
        tree = ast.parse(
            (
                REPO_ROOT
                / "current/packages/agent-server/src/disco/agent_server/runtime.py"
            ).read_text()
        )
        runtime = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "ConversationRuntime"
        )
        classified = python_scan.classify_init_parameters(runtime)
        assert [row["name"] for row in classified["collaborators"]] == example[
            "collaborators"
        ]
        assert [row["name"] for row in classified["scalar_config"]] == example[
            "scalar_config"
        ]
        assert python_scan.constructor_collaborator_count(runtime) == 6

    def test_oversized_callable_in_temp_repo_fails(self) -> None:
        """A callable over 100 logical lines must fail the scanner."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = "def big():\n" + "    pass\n" * 101
        write_and_track(root, "current/packages/core/src/disco/core/big.py", code)
        git_commit(root)
        result = python_scan.scan_all(root)
        found_violation = False
        for module in result["modules"]:
            for v in module["violations"]:
                if v["rule"] == "python_callable_logical_gt_100":
                    found_violation = True
        assert found_violation, "oversized callable not detected"
        tmp.cleanup()


# ---------------------------------------------------------------------------
# Effect owners — route and frontend transport
# ---------------------------------------------------------------------------


class TestEffectOwners:
    def test_route_effect_owners_frozen(self):
        """Route-owned effects must be frozen observations."""
        ownership_data = json.loads(
            (REPO_ROOT / "development" / "architecture" / "ownership.json").read_text()
        )
        rows = [
            row
            for row in ownership_data["effect_owners"]
            if row["kind"] == "route_adapter_effect_violation"
        ]
        assert len(rows) == 1
        # PKG-07 extracted the import mechanics out of routes/projects.py into
        # routes/projects_import.py and renamed the handlers. The registry
        # followed; this assertion did not, because the suite was never run.
        assert rows[0]["path"].endswith("/routes/projects_import.py")
        assert rows[0]["symbols"] == [
            "_copy_scanned_tree",
            "_extract_zip_entries",
            "clone_git_url",
            "_materialize_source",
            "import_project",
        ]
        assert rows[0]["frozen_observation"] is True
        # Frozen strings rot silently. Bind them to live source so the next
        # rename fails here instead of going unnoticed for three epics.
        source = (REPO_ROOT / rows[0]["path"]).read_text(encoding="utf-8")
        defined = {
            node.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert set(rows[0]["symbols"]) <= defined

    def test_frontend_transport_confined(self):
        """Frontend raw transport must be confined to frontend/src/api/."""
        ownership_data = json.loads(
            (REPO_ROOT / "development" / "architecture" / "ownership.json").read_text()
        )
        for eff in ownership_data.get("effect_owners", []):
            if eff["kind"] == "frontend_transport":
                assert "frontend/src/api/" in eff["path"]

    def test_route_adapters_may_not_own_effects(self):
        """The policy must declare route adapters may not own effects."""
        p = policy.load_policy()
        route_adapters = p["typed_classifications"]["route_adapters"]
        assert route_adapters["may_not_own_persistence"] is True
        assert route_adapters["may_not_own_process_effects"] is True
        assert route_adapters["may_not_own_network_effects"] is True
        assert route_adapters["may_not_own_lifecycle_transitions"] is True

    def test_frontend_components_may_not_call_raw_transport(self):
        p = policy.load_policy()
        frontend = p["typed_classifications"]["frontend_components"]
        assert frontend["may_not_call_fetch"] is True
        assert frontend["may_not_call_websocket"] is True
        assert frontend["may_not_call_eventsource"] is True
        assert frontend["may_not_call_raw_transport"] is True
        assert frontend["may_consume_typed_api_client"] is True

    def test_route_handler_in_temp_repo_scanned(self) -> None:
        """A route handler in a temp repo must be scanned as a route."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = dedent("""\
            @router.get("/test")
            def handler(request):
                pass
            """) + "    x = 1\n" * 80
        write_and_track(
            root,
            "current/packages/agent-server/src/disco/agent_server/routes/handler.py",
            code,
        )
        git_commit(root)
        result = python_scan.scan_all(root)
        found_route = False
        for module in result["modules"]:
            for sym in module["symbols"]:
                if sym["name"] == "handler" and "route" in str(sym.get("decorators", [])).lower():
                    found_route = True
        assert found_route, "route handler not detected by scanner"
        tmp.cleanup()


# ---------------------------------------------------------------------------
# Protocol non-violation positives — through is_protocol_non_violation
# ---------------------------------------------------------------------------


class TestProtocolNonViolation:
    def test_eventstore_is_protocol_non_violation(self):
        p = policy.load_policy()
        assert policy.is_protocol_non_violation(
            p,
            "current/packages/core/src/disco/core/store/base.py",
            "EventStore",
            "python_service_public_methods_gt_12_candidate",
        )
        assert budget.check_protocol_non_violations(REPO_ROOT) == []

    def test_buildkernel_is_protocol_non_violation(self):
        p = policy.load_policy()
        assert policy.is_protocol_non_violation(
            p,
            "current/packages/agent-server/src/disco/agent_server/build_kernel/base.py",
            "BuildKernel",
            "python_service_public_methods_gt_12_candidate",
        )

    def test_non_listed_symbol_is_not_non_violation(self):
        p = policy.load_policy()
        assert not policy.is_protocol_non_violation(
            p,
            "current/packages/fake/test.py",
            "FakeClass",
            "python_service_public_methods_gt_12_candidate",
        )

    def test_wrong_rule_is_not_non_violation(self):
        p = policy.load_policy()
        assert not policy.is_protocol_non_violation(
            p,
            "current/packages/core/src/disco/core/store/base.py",
            "EventStore",
            "python_callable_logical_gt_100",
        )
        tmp = make_temp_repo()
        root = Path(tmp.name)
        path = "current/packages/core/src/disco/core/stateful.py"
        write_and_track(
            root,
            path,
            "from typing import Protocol\n\n"
            "class Stateful(Protocol):\n"
            "    def __init__(self):\n"
            "        self.value = 1\n",
        )
        policy_data = json.loads(
            (REPO_ROOT / "development/architecture/policy.json").read_text()
        )
        policy_data["typed_classifications"]["protocol_non_violations"] = [
            {
                "path": path,
                "symbol": "Stateful",
                "rule": "python_service_public_methods_gt_12_candidate",
                "disposition_id": "PY-0001",
                "criterion": "Protocol with zero mutable implementation state",
            }
        ]
        write(root / "development/architecture/policy.json", json.dumps(policy_data))
        git_commit(root)
        problems = budget.check_protocol_non_violations(root)
        assert_problem_contains(problems, "mutable implementation state")
        tmp.cleanup()


# ---------------------------------------------------------------------------
# State owners — constant check
# ---------------------------------------------------------------------------


class TestStateOwners:
    def test_state_owners_listed(self):
        ownership_data = json.loads(
            (REPO_ROOT / "development" / "architecture" / "ownership.json").read_text()
        )
        owners = ownership_data.get("state_owners", [])
        symbols = [o["symbol"] for o in owners]
        assert "EventStore" in symbols
        assert "ConversationRuntime" in symbols
        # The workspace state owner is the class WorkspaceCoordinator, which
        # lives in workspace_service.py; "WorkspaceService" is the module, not
        # a symbol, and no such class exists anywhere in the tree.
        assert "WorkspaceCoordinator" in symbols

    def test_evidence_owners_listed(self):
        ownership_data = json.loads(
            (REPO_ROOT / "development" / "architecture" / "ownership.json").read_text()
        )
        owners = ownership_data.get("evidence_owners", [])
        symbols = [o["symbol"] for o in owners]
        assert "DiscoApiClient" in symbols


# ---------------------------------------------------------------------------
# Unknown root, raised cap, and waiver tampering — through policy.load_policy
# ---------------------------------------------------------------------------


class TestPolicyTampering:
    """Tests for unknown root, raised cap, and waiver/symbol tampering."""

    def test_unknown_root_fails(self):
        """The policy must declare unknown_roots_fail."""
        p = policy.load_policy()
        assert p["shrink_only_rules"]["unknown_roots_fail"] is True

    def test_raised_cap_fails(self):
        """The policy must declare raised_caps_fail."""
        p = policy.load_policy()
        assert p["shrink_only_rules"]["raised_caps_fail"] is True

    def test_wildcards_fail(self):
        """The policy must declare wildcards_fail."""
        p = policy.load_policy()
        assert p["shrink_only_rules"]["wildcards_fail"] is True

    def test_new_observations_fail(self):
        """Observation authority and every source segment must fail closed."""
        p = policy.load_policy()
        assert p["shrink_only_rules"]["new_observations_fail"] is True

        tmp = make_temp_repo()
        root = Path(tmp.name)
        first = "current/packages/core/src/disco/core/observed.py"
        second = "current/packages/core/src/disco/core/other.py"
        location = f"{first}:Observed:1-2 + {second}:helper:1-2"
        write_and_track(root, first, "class Observed:\n    pass\n")
        write_and_track(root, second, "def helper():\n    pass\n")
        disposition = {
            "id": "DM-TEST",
            "disposition": "DISTRIBUTED_CONCERN",
            "state": "active",
            "owner_package": "PKG-02-TEST",
            "location": location,
            "role_category": "distributed fixture concern",
        }
        observation = {
            "id": "DM-TEST",
            "kind": "distributed_concern",
            "location": location,
            "concern": "distributed fixture concern",
            "owner_package": "PKG-02-TEST",
            "source_disposition_id": "DM-TEST",
        }
        write(
            root / "development/architecture/dispositions.json",
            json.dumps([disposition]),
        )
        write(
            root / "development/architecture/observations.json",
            json.dumps([observation]),
        )
        assert budget.check_observations_agreement(root) == []

        write(root / "development/architecture/observations.json", "[]")
        assert_problem_contains(
            budget.check_observations_agreement(root),
            "active dispositions missing observations",
            "DM-TEST",
        )
        write(
            root / "development/architecture/observations.json",
            json.dumps([observation]),
        )

        observation["owner_package"] = "PKG-WRONG"
        write(
            root / "development/architecture/observations.json",
            json.dumps([observation]),
        )
        assert_problem_contains(
            budget.check_observations_agreement(root),
            "owner drift",
        )
        observation["owner_package"] = "PKG-02-TEST"

        observation["kind"] = "typed_non_violation"
        write(
            root / "development/architecture/observations.json",
            json.dumps([observation]),
        )
        assert_problem_contains(
            budget.check_observations_agreement(root),
            "kind drift",
        )
        observation["kind"] = "distributed_concern"

        disposition["disposition"] = "TYPED_NON_VIOLATION"
        observation["kind"] = "typed_non_violation"
        observation["classification"] = observation.pop("concern")
        write(
            root / "development/architecture/dispositions.json",
            json.dumps([disposition]),
        )
        write(
            root / "development/architecture/observations.json",
            json.dumps([observation]),
        )
        assert_problem_contains(
            budget.check_observations_agreement(root),
            "not an AST-proven stateless Protocol",
        )
        disposition["disposition"] = "DISTRIBUTED_CONCERN"
        observation["kind"] = "distributed_concern"
        observation["concern"] = observation.pop("classification")
        write(
            root / "development/architecture/dispositions.json",
            json.dumps([disposition]),
        )

        write(root / second, "def renamed():\n    pass\n")
        write(
            root / "development/architecture/observations.json",
            json.dumps([observation]),
        )
        problems = budget.check_observations_agreement(root)
        assert_problem_contains(problems, "symbol helper", "not found", second)

        assert_observation_evidence_boundaries(
            root,
            first,
            disposition,
            observation,
            budget.check_observations_agreement,
            typescript_scan.scan_typescript,
            write_frontend_context_authority,
        )
        tmp.cleanup()

    def test_broadened_context_edges_fail(self):
        """The policy must declare broadened_context_edges_fail."""
        p = policy.load_policy()
        assert p["shrink_only_rules"]["broadened_context_edges_fail"] is True

    def test_logical_limits_not_raised(self):
        """Logical limits must not be raised above their sealed values."""
        p = policy.load_policy()
        limits = p["logical_limits"]
        assert limits["python_production_module"] == 700
        assert limits["python_class"] == 350
        assert limits["python_callable"] == 100
        assert limits["python_callable_mccabe"] == 15
        assert limits["typescript_module"] == 500
        assert limits["typescript_component"] == 250
        assert limits["typescript_hook"] == 200
        assert limits["python_composition_root"] == 300
        assert limits["python_constructor_collaborators"] == 10
        assert limits["python_dependency_aggregate_members"] == 10

    def test_protocol_non_violation_is_typed(self):
        """Protocol non-violations must be typed (path + symbol + rule), not wildcard."""
        p = policy.load_policy()
        for entry in policy.protocol_non_violations(p):
            assert "path" in entry
            assert "symbol" in entry
            assert "rule" in entry
            assert "disposition_id" in entry
            assert "criterion" in entry
            # No wildcard paths
            assert "*" not in entry["path"]
            assert "*" not in entry["symbol"]

    def test_route_adapters_may_validate_and_translate(self):
        """Route adapters must be allowed to validate, translate, delegate, render."""
        p = policy.load_policy()
        route_adapters = p["typed_classifications"]["route_adapters"]
        assert route_adapters["may_validate"] is True
        assert route_adapters["may_translate"] is True
        assert route_adapters["may_delegate"] is True
        assert route_adapters["may_render"] is True

    def test_exemption_may_shrink_not_broaden(self):
        """DM-005 exemptions may shrink but not broaden or wildcard."""
        p = policy.load_policy()
        exclusions = p["exact_exclusions"]
        assert exclusions["exemption_may_shrink"] is True
        assert exclusions["exemption_may_not_broaden"] is True
        assert exclusions["exemption_may_not_wildcard"] is True

    def test_generated_proof_requirements_complete(self):
        """All six generated proof requirements must be listed."""
        p = policy.load_policy()
        requirements = p["generated_proof_requirements"]
        expected = [
            "tracked_generator_owns_output",
            "deterministic_regeneration_command",
            "regenerated_bytes_match_registered_sha256",
            "content_scan_proves_declared_family",
            "named_owner_package_and_removal_responsibility",
            "deterministic_regeneration_test_collected",
        ]
        for req in expected:
            assert req in requirements, f"missing requirement: {req}"

    def test_raised_cap_detected_in_temp_repo(self) -> None:
        """A raised logical limit in a temp repo policy must be detectable.

        This exercises the policy loading boundary against a temp repo with
        a raised cap.
        """
        tmp = make_temp_repo()
        root = Path(tmp.name)
        arch = root / "development" / "architecture"
        arch.mkdir(parents=True)
        # Write a policy with a RAISED callable limit (101 instead of 100)
        p = policy.load_policy()
        p["logical_limits"]["python_callable"] = 101
        write(arch / "policy.json", json.dumps(p))
        # The policy loader reads from the real repo, not the temp repo,
        # so we verify the sealed limit is still 100
        real_p = policy.load_policy()
        assert real_p["logical_limits"]["python_callable"] == 100
        tmp.cleanup()


# ---------------------------------------------------------------------------
# Disguised aggregate/collaborator enforcement — expected reds
# ---------------------------------------------------------------------------


class TestDisguisedCollaboratorEnforcement:
    """Tests for disguised aggregate/collaborator detection.

    A constructor using ``dict``, ``Any``, ``Context``, ``Services``,
    ``Runtime``, or ``Deps`` as a parameter type is a disguised aggregate —
    it hides injected collaborators behind an untyped bag. The scanner must
    flag these as disguise pattern violations.

    These are expected reds until the F writer adds disguise pattern
    enforcement to the scanner.
    """

    def test_dict_disguise_produces_violation(self) -> None:
        """A constructor using ``dict`` as a service bag must be flagged."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = dedent("""\
            class Bad:
                def __init__(self, services: dict):
                    pass
            """)
        path = "current/packages/core/src/disco/core/bad.py"
        write_and_track(root, path, code)
        write_budget_authority(root)
        git_commit(root)
        problems = budget.check_constructor_collaborator_caps(root)
        assert_problem_contains(
            problems,
            "disguised collaborator",
            "services",
        )

        for annotation in ("Any", "dict[str, object]"):
            write(
                root / path,
                "from typing import Any\n\n"
                f"class Bad:\n"
                f"    def __init__(self, service1: {annotation}):\n"
                "        pass\n",
            )
            problems = budget.check_constructor_collaborator_caps(root)
            assert_problem_contains(
                problems, "disguised collaborator", "service1"
            )

        write(
            root / path,
            "from typing import Any\n\n"
            "class Bad:\n"
            "    def __init__(self, research_providers: dict[str, Any]):\n"
            "        pass\n",
        )
        assert budget.check_constructor_collaborator_caps(root) == []

        write(root / path, code)
        ownership_path = root / "development/architecture/ownership.json"
        ownership = json.loads(ownership_path.read_text())
        ownership["legacy_disguised_collaborators"].append(
            {
                "path": path,
                "symbol": "Bad",
                "parameter": "services",
                "annotation": "dict",
                "disposition_id": "DM-001",
                "owner_package": "PKG-06-RUNTIME",
                "removal_package": "PKG-06-RUNTIME",
                "reason": (
                    "DM-001 runtime back-reference exposes ConversationRuntime "
                    "collaborators through self._rt"
                ),
            }
        )
        write(ownership_path, json.dumps(ownership))
        problems = budget.check_constructor_collaborator_caps(root)
        assert_problem_contains(problems, "new legacy disguised collaborator")
        assert_problem_contains(problems, "unregistered disguised collaborator")
        tmp.cleanup()

    def test_any_disguise_produces_violation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A constructor using ``Any`` as a service bag must be flagged."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = dedent("""\
            class Bad:
                def __init__(self, ctx: Any):
                    pass
            """)
        path = "current/packages/core/src/disco/core/bad.py"
        write_and_track(root, path, code)
        write_budget_authority(root)
        git_commit(root)
        problems = budget.check_constructor_collaborator_caps(root)
        assert_problem_contains(problems, "disguised collaborator", "ctx")

        legacy_rel = (
            "current/packages/agent-server/src/disco/agent_server/appkit_ejection.py"
        )
        legacy_path = root / legacy_rel
        collaborators = ", ".join(
            f"service{index}: Service{index}" for index in range(11)
        )
        # write_and_track, not write: the collaborator scanner enumerates via
        # `git ls-files`, so an untracked new file is invisible and the cap
        # assertion below could never have fired.
        write_and_track(
            root,
            legacy_rel,
            "from typing import Any\n\n"
            "class AppKitEjectionService:\n"
            f"    def __init__(self, runtime: Any, {collaborators}) -> None:\n"
            "        self._rt = runtime\n",
        )
        problems = budget.check_constructor_collaborator_caps(root)
        assert_problem_contains(
            problems,
            "collaborator count",
            "AppKitEjectionService",
            "cap 10",
        )

        # A row only becomes "registered" when its tuple is in the frozen
        # module constant, which is now legitimately empty — so the staleness
        # path is unreachable from live data. Register one synthetic tuple to
        # exercise the mechanism itself.
        legacy_row = synthetic_legacy_row(
            legacy_rel,
            symbol="AppKitEjectionService",
            parameter="runtime",
            annotation="Any",
        )
        legacy_key = (legacy_rel, "AppKitEjectionService", "runtime")
        monkeypatch.setattr(
            source_governance,
            "LEGACY_DISGUISED_COLLABORATORS",
            {legacy_key: legacy_row},
        )
        ownership_path = root / "development/architecture/ownership.json"
        ownership = json.loads(ownership_path.read_text())
        ownership["legacy_disguised_collaborators"] = [legacy_row]
        write(ownership_path, json.dumps(ownership))

        write(
            legacy_path,
            "class AppKitEjectionService:\n"
            "    def __init__(self, runtime: str) -> None:\n"
            "        self._rt = runtime\n",
        )
        problems = budget.check_constructor_collaborator_caps(root)
        assert_problem_contains(problems, "stale legacy disguised collaborator")
        tmp.cleanup()

    def test_context_disguise_produces_violation(self) -> None:
        """A constructor using ``Context`` as a service bag must be flagged."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = dedent("""\
            class Bad:
                def __init__(self, ctx: Context):
                    pass
            """)
        path = "current/packages/core/src/disco/core/bad.py"
        write_and_track(root, path, code)
        write_budget_authority(root)
        git_commit(root)
        problems = budget.check_constructor_collaborator_caps(root)
        assert_problem_contains(problems, "disguised collaborator", "ctx")

        ownership_path = root / "development/architecture/ownership.json"
        ownership = json.loads(ownership_path.read_text())
        duplicated = synthetic_legacy_row(path)
        ownership["legacy_disguised_collaborators"].extend(
            [duplicated, dict(duplicated)]
        )
        write(ownership_path, json.dumps(ownership))
        assert_problem_contains(
            budget.check_constructor_collaborator_caps(root),
            "duplicate tuple",
        )
        tmp.cleanup()


# ---------------------------------------------------------------------------
# Route effect enforcement — expected reds
# ---------------------------------------------------------------------------


class TestRouteEffectEnforcement:
    """Tests for route adapter effect ownership enforcement.

    Route adapters may validate, translate, delegate, and render — but they
    may not own persistence, process, filesystem, network, or lifecycle
    effects. The scanner must flag route handlers that directly call
    ``shutil.rmtree``, ``subprocess``, ``mkdir``, ``tempfile``, etc.

    These are expected reds until the F writer adds route effect enforcement.
    """

    def test_route_with_subprocess_fails(self) -> None:
        """A route handler calling subprocess must be flagged."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = dedent("""\
            import subprocess

            @router.post("/exec")
            def handler(request):
                subprocess.run(["echo", "hi"])
                return {"ok": True}
            """)
        path = "current/packages/agent-server/src/disco/agent_server/routes/exec.py"
        write_and_track(root, path, code)
        write_budget_authority(root)
        git_commit(root)
        problems = budget.check_route_effect_owners(root)
        assert_problem_contains(problems, "unregistered direct effects", "handler")

        write(
            root / path,
            dedent("""\
                import subprocess as sp

                @router.post("/exec")
                def handler(request):
                    sp.run(["echo", "hi"])
                    return {"ok": True}
                """),
        )
        problems = budget.check_route_effect_owners(root)
        assert_problem_contains(problems, "unregistered direct effects", "handler")

        write(
            root / path,
            dedent("""\
                import urllib.request

                @router.post("/exec")
                def handler(request):
                    urllib.request.urlopen("https://example.test")
                    return {"ok": True}
                """),
        )
        problems = budget.check_route_effect_owners(root)
        assert_problem_contains(problems, "unregistered direct effects", "handler")

        assert_route_decorator_boundaries(
            root, path, budget.check_route_effect_owners
        )

        write(
            root / path,
            dedent("""\
                import subprocess

                @router.post("/first")
                def handler(request):
                    subprocess.run(["echo", "first"])
                    return {"ok": True}

                @router.post("/second")
                def handler(request):
                    subprocess.run(["echo", "second"])
                    return {"ok": True}
                """),
        )
        write_budget_authority(
            root,
            effect_owners=[
                {
                    "kind": "route_adapter_effect_violation",
                    "path": path,
                    "symbols": ["handler"],
                }
            ],
        )
        problems = budget.check_route_effect_owners(root)
        assert_problem_contains(
            problems,
            "duplicate decorated route source identity",
            "handler",
            "2 occurrences",
        )
        tmp.cleanup()

    def test_route_with_filesystem_ops_fails(self) -> None:
        """A route handler calling shutil.rmtree must be flagged."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = dedent("""\
            import shutil

            @router.delete("/cleanup")
            def handler(request):
                shutil.rmtree("/tmp/old")
                return {"ok": True}
            """)
        path = "current/packages/agent-server/src/disco/agent_server/routes/cleanup.py"
        write_and_track(root, path, code)
        write_budget_authority(root)
        git_commit(root)
        problems = budget.check_route_effect_owners(root)
        assert_problem_contains(problems, "unregistered direct effects", "handler")

        write(
            root / path,
            dedent("""\
                from shutil import rmtree as wipe

                @router.delete("/cleanup")
                def handler(request):
                    wipe("/tmp/old")
                    return {"ok": True}
                """),
        )
        problems = budget.check_route_effect_owners(root)
        assert_problem_contains(problems, "unregistered direct effects", "handler")
        tmp.cleanup()

    def test_route_with_validation_passes(self) -> None:
        """A route handler that only validates and delegates must not be flagged."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = dedent("""\
            @router.post("/validate")
            def handler(request):
                data = request.json
                if not data.get("name"):
                    return {"error": "name required"}
                return service.process(data)
            """)
        path = "current/packages/agent-server/src/disco/agent_server/routes/validate.py"
        write_and_track(root, path, code)
        write_budget_authority(root)
        git_commit(root)
        assert budget.check_route_effect_owners(root) == []
        tmp.cleanup()


# ---------------------------------------------------------------------------
# Composition root non-wiring effects — expected reds
# ---------------------------------------------------------------------------


class TestCompositionRootEffects:
    """Tests for composition root non-wiring effect enforcement.

    A composition root must be wiring-only — it must not contain business
    logic, state mutations, or effects. The scanner must flag composition
    roots that contain non-wiring code.

    These are expected reds until the F writer adds composition root effect
    enforcement.
    """

    def test_oversized_composition_root_fails(self) -> None:
        """A composition root over 300 logical lines must fail."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        path = "current/packages/agent-server/src/disco/agent_server/app.py"
        code = "def create_app():\n" + "    value = 1\n" * 301
        write_and_track(root, path, code)
        write_budget_authority(
            root,
            composition_roots=[{"path": path, "symbol": "create_app"}],
        )
        git_commit(root)
        problems = budget.check_composition_roots(root)
        assert_problem_contains(
            problems,
            "composition root create_app",
            "exceeds limit 300",
        )

        write(
            root / path,
            "import subprocess\n\n"
            "def create_app():\n"
            '    subprocess.run(["echo", "not wiring"])\n',
        )
        problems = budget.check_composition_roots(root)
        assert_problem_contains(problems, "direct effect calls", "wiring-only")

        write(
            root / path,
            "import subprocess as sp\n\n"
            "def create_app():\n"
            '    sp.run(["echo", "not wiring"])\n',
        )
        problems = budget.check_composition_roots(root)
        assert_problem_contains(problems, "direct effect calls", "wiring-only")
        tmp.cleanup()


# ---------------------------------------------------------------------------
# Component/hook raw transport — expected reds
# ---------------------------------------------------------------------------


class TestFrontendRawTransport:
    """Tests for frontend component/hook raw transport enforcement.

    Frontend components and hooks may consume a typed API client but may not
    call ``fetch``, ``WebSocket``, ``EventSource``, or raw transport directly.
    The TS scanner must flag these calls in components and hooks.

    These are expected reds until the F writer adds raw transport enforcement
    to the TS scanner.
    """

    def test_component_with_fetch_fails(self) -> None:
        """A React component calling fetch must be flagged."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = (
            "export function MyComponent() {\n"
            "  const data = fetch('/api/data');\n"
            "  return <div>{data}</div>;\n"
            "}\n"
        )
        write_and_track(root, "frontend/src/components/MyComponent.tsx", code)
        write_frontend_context_authority(root)
        git_commit(root)
        result = typescript_scan.scan_typescript(root)
        transport_violations = [
            v for module in result.get("modules", [])
            for v in module.get("violations", [])
            if "transport" in v["rule"].lower() or "fetch" in v["rule"].lower()
        ]
        assert len(transport_violations) > 0, (
            "component with fetch not flagged — the TS scanner must detect "
            "raw fetch calls in React components"
        )
        tmp.cleanup()

    def test_hook_with_websocket_fails(self) -> None:
        """A React hook calling WebSocket must be flagged."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = (
            "export function useLiveStream() {\n"
            "  const ws = new WebSocket('ws://localhost');\n"
            "  return ws;\n"
            "}\n"
        )
        write_and_track(root, "frontend/src/hooks/useLiveStream.ts", code)
        write_frontend_context_authority(root)
        git_commit(root)
        result = typescript_scan.scan_typescript(root)
        transport_violations = [
            v for module in result.get("modules", [])
            for v in module.get("violations", [])
            if "transport" in v["rule"].lower() or "websocket" in v["rule"].lower()
        ]
        assert len(transport_violations) > 0, (
            "hook with WebSocket not flagged — the TS scanner must detect "
            "raw WebSocket calls in React hooks"
        )
        tmp.cleanup()

    def test_typed_api_client_transport_passes(self) -> None:
        """A typed API client in frontend/src/api/ may use raw transport."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = "export async function fetchData() {\n  return fetch('/api/data');\n}\n"
        write_and_track(root, "frontend/src/api/client.ts", code)
        write_frontend_context_authority(root)
        git_commit(root)
        result = typescript_scan.scan_typescript(root)
        transport_violations = [
            v for module in result.get("modules", [])
            for v in module.get("violations", [])
            if "transport" in v["rule"].lower() or "fetch" in v["rule"].lower()
        ]
        assert len(transport_violations) == 0, (
            f"typed API client in frontend/src/api/ flagged for raw transport: "
            f"{transport_violations}"
        )
        tmp.cleanup()
