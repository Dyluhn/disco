"""Import/context graph mutation tests for the architecture gate.

Tests static, relative, package-init, function-local, and literal-dynamic
import extraction; package cycles; upward (layer-violating) edges; top-sibling
independence; DM-005 exemption exactness and wildcard rejection; and TS
context-cycle / cross-feature import detection via the ``check_imports(root=)``
boundary.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import (
    assert_problem_contains,
    git_commit,
    make_temp_repo,
    write,
    write_and_track,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "development" / "scripts"))
from architecture import imports  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]


def write_import_authority(root: Path, *, seed_dm005: bool = True) -> None:
    write(
        root / "development/architecture/contexts.json",
        (REPO_ROOT / "development/architecture/contexts.json").read_text(),
    )
    write(root / ".importlinter", (REPO_ROOT / ".importlinter").read_text())
    if not seed_dm005:
        return
    write_and_track(
        root,
        "current/packages/tools/src/disco/tools/builtin/audio_overview.py",
        "def load_audio():\n"
        "    from disco.agent_server import audio_config, tts_local\n"
        "    return audio_config, tts_local\n",
    )
    write_and_track(
        root,
        "current/packages/tools/src/disco/tools/builtin/preview.py",
        "def load_preview():\n"
        "    from disco.agent_server import preview_manager\n"
        "    return preview_manager\n",
    )


def real_context_facts() -> tuple[dict, dict[str, int], frozenset[str]]:
    contexts = imports.load_contexts(REPO_ROOT)
    return (
        contexts,
        imports._build_layer_index(contexts),
        imports._build_dm005_exemptions(contexts),
    )


# ---------------------------------------------------------------------------
# Import extraction unit tests
# ---------------------------------------------------------------------------


class TestImportExtraction:
    def test_dep_pkg_resolves_correctly(self):
        _, layers, _ = real_context_facts()
        assert imports._dep_pkg("disco.core.build_platform", layers) == "disco.core"
        assert imports._dep_pkg("disco.tools.builtin", layers) == "disco.tools"
        assert (
            imports._dep_pkg("disco.agent_server.routes", layers)
            == "disco.agent_server"
        )
        assert imports._dep_pkg("disco", layers) is None
        assert imports._dep_pkg("other.module", layers) is None
        assert imports._dep_pkg(None, layers) is None

    def test_module_name_from_path(self):
        assert imports._module_name("current/packages/core/src/disco/core/events.py") == "disco.core.events"
        assert imports._module_name("current/packages/core/src/disco/core/__init__.py") == "disco.core"
        assert imports._module_name(
            "current/packages/tools/src/disco/tools/builtin/preview.py"
        ) == "disco.tools.builtin.preview"

    def test_extract_static_imports(self):
        code = "import os\nfrom disco.core import events\nfrom disco.tools.builtin import preview\n"
        tree = ast.parse(code)
        imps = imports._extract_imports(tree, "test.module")
        targets = [i["target_module"] for i in imps]
        assert "os" in targets
        assert "disco.core" in targets
        assert "disco.tools.builtin" in targets

    def test_extract_function_local_imports(self):
        code = "def f():\n    import disco.core\n    from disco.tools import builtin\n"
        tree = ast.parse(code)
        imps = imports._extract_imports(tree, "test.module")
        func_local = [i for i in imps if i["kind"] == "function_local"]
        assert len(func_local) == 2
        targets = [i["target_module"] for i in func_local]
        assert "disco.core" in targets
        assert "disco.tools" in targets

    def test_extract_literal_dynamic_imports(self):
        code = dedent("""\
            import importlib
            import importlib as loader
            from importlib import import_module as load
            module_alias = loader
            assigned = module_alias.import_module
            chained = assigned
            annotated: object = load
            builtin = __import__
            import importlib as shared
            from importlib import import_module as shared
            collision = shared
            ordinary = print
            def f(target):
                importlib.import_module("disco.core.events")
                chained("disco.retrieval")
                annotated("disco.tools")
                builtin("disco.agent_server")
                collision("disco.core.events")
                ordinary("not an import")
                loader.import_module(target)
                assigned(target)
                builtin(target)
                collision(target)
            """)
        tree = ast.parse(code)
        imps = imports._extract_imports(tree, "test.module")
        dynamic = [i for i in imps if i["kind"] == "dynamic_literal"]
        assert {item["target_module"] for item in dynamic} == {
            "disco.agent_server",
            "disco.core.events",
            "disco.retrieval",
            "disco.tools",
        }
        nonliteral = [i for i in imps if i["kind"] == "dynamic_nonliteral"]
        assert len(nonliteral) == 4

        ordered = imports._extract_imports(
            ast.parse(
                dedent("""\
                    importlib.import_module("ignored.before_import")
                    __import__("literal.builtin")
                    import os as __import__
                    __import__("ignored.import_shadow")
                    from builtins import __import__ as __import__
                    __import__("literal.import_restore")
                    import importlib as shared
                    shared.import_module("literal.module_first")
                    from importlib import import_module as shared
                    shared("literal.function_latest")
                    shared.import_module("ignored.function_collision")
                    import importlib as shared
                    shared.import_module("literal.module_latest")
                    shared("ignored.module_collision")
                    module_a = module_b = shared
                    function_ref = module_a.import_module
                    annotated: object = function_ref
                    (module_named := module_b).import_module("literal.named_module")
                    (function_named := annotated)("literal.named_function")
                    ordinary = print
                    ordinary("ignored.ordinary")
                    __import__ = ordinary
                    __import__("ignored.shadowed_builtin")
                    from builtins import __import__ as restored
                    restored("literal.restored_builtin")
                    """)
            ),
            "ordered.module",
        )
        assert {
            row["target_module"]
            for row in ordered
            if row["kind"] == "dynamic_literal"
        } == {
            "literal.builtin",
            "literal.function_latest",
            "literal.import_restore",
            "literal.module_first",
            "literal.module_latest",
            "literal.named_function",
            "literal.named_module",
            "literal.restored_builtin",
        }
        assert not any(
            row["target_module"].startswith("ignored.")
            for row in ordered
            if row["kind"].startswith("dynamic_")
        )

        shadowed = imports._extract_imports(
            ast.parse(
                dedent("""\
                    import importlib as outer_module
                    from importlib import import_module as outer_function
                    def parameter_shadow(
                        outer_module, outer_function, __import__
                    ):
                        outer_module.import_module("ignored.parameter_module")
                        outer_function("ignored.parameter_function")
                        __import__("ignored.parameter_builtin")
                    def assignment_shadow():
                        outer_module.import_module("ignored.before_assignment")
                        outer_function("ignored.before_function_assignment")
                        __import__("ignored.before_builtin_assignment")
                        outer_module = print
                        outer_function = print
                        __import__ = print
                        outer_module.import_module("ignored.after_assignment")
                    def definition_shadow():
                        outer_function("ignored.before_definition")
                        def outer_function(value):
                            return value
                    def import_shadow():
                        outer_module.import_module("ignored.before_local_import")
                        import os as outer_module
                        outer_module.import_module("ignored.after_local_import")
                    from importlib import import_module as replaced
                    def replaced():
                        replaced("ignored.recursive_definition")
                    """)
            ),
            "shadowed.module",
        )
        assert not [
            row for row in shadowed if row["kind"].startswith("dynamic_")
        ]

        nested = imports._extract_imports(
            ast.parse(
                dedent("""\
                    import importlib as module_source
                    from importlib import import_module as function_source
                    module_direct = module_source
                    module_a = module_b = module_direct
                    function_direct = module_a.import_module
                    annotated: object = function_source
                    (module_named := module_b)
                    (function_named := function_direct)
                    module_named.import_module("literal.nested_module_alias")
                    function_named("literal.nested_function_alias")
                    def outer(target):
                        module_local = module_b
                        function_local = annotated
                        def inner():
                            module_local.import_module("literal.nested_module")
                            function_local("literal.nested_function")
                            function_local(target)
                    """)
            ),
            "nested.module",
        )
        nested_literal = {
            row["target_module"]
            for row in nested
            if row["kind"] == "dynamic_literal"
        }
        assert nested_literal == {
            "literal.nested_function",
            "literal.nested_function_alias",
            "literal.nested_module",
            "literal.nested_module_alias",
        }
        assert sum(
            row["kind"] == "dynamic_nonliteral" for row in nested
        ) == 1

        temporal = imports._extract_imports(
            ast.parse(
                dedent("""\
                    def late_definition():
                        late_module.import_module("literal.late_module")
                        late_function("literal.late_function")
                    late_lambda = lambda: late_function("literal.late_lambda")
                    import importlib as late_module
                    from importlib import import_module as late_function
                    import importlib as rebound_module
                    from importlib import import_module as rebound_function
                    def rebound_definition():
                        rebound_module.import_module("ignored.rebound_module")
                        rebound_function("ignored.rebound_function")
                    rebound_lambda = lambda: rebound_function(
                        "ignored.rebound_lambda"
                    )
                    rebound_module = print
                    rebound_function = print
                    def late_outer():
                        def late_inner():
                            closure_module.import_module(
                                "literal.late_nested_module"
                            )
                            closure_function("literal.late_nested_function")
                        import importlib as closure_module
                        from importlib import import_module as closure_function
                    def rebound_outer():
                        import importlib as closure_module
                        from importlib import import_module as closure_function
                        def rebound_inner():
                            closure_module.import_module(
                                "ignored.rebound_nested_module"
                            )
                            closure_function("ignored.rebound_nested_function")
                        closure_module = print
                        closure_function = print
                    """)
            ),
            "temporal.module",
        )
        assert {
            row["target_module"]
            for row in temporal
            if row["kind"] == "dynamic_literal"
        } == {
            "literal.late_function",
            "literal.late_lambda",
            "literal.late_module",
            "literal.late_nested_function",
            "literal.late_nested_module",
        }
        assert not any(
            row["target_module"].startswith("ignored.")
            for row in temporal
            if row["kind"].startswith("dynamic_")
        )

    def test_extract_relative_imports(self):
        code = "from . import sibling\nfrom ..parent import thing\n"
        tree = ast.parse(code)
        imps = imports._extract_imports(tree, "disco.core.sub.module")
        targets = [i["target_module"] for i in imps]
        assert "disco.core.sub" in targets
        assert "disco.core.parent" in targets

    def test_package_init_imports(self):
        """An __init__.py importing from a sibling package must be extracted."""
        code = "from disco.core import events\n"
        tree = ast.parse(code)
        imps = imports._extract_imports(tree, "disco.tools")
        assert any(i["target_module"] == "disco.core" for i in imps)


def dedent(text: str) -> str:
    import textwrap
    return textwrap.dedent(text)


# ---------------------------------------------------------------------------
# DM-005 exemption exactness
# ---------------------------------------------------------------------------


class TestDM005Exemptions:
    def test_dm005_exemptions_exact(self):
        _, _, exemptions = real_context_facts()
        assert exemptions == frozenset(
            {
                "disco.tools.builtin.audio_overview -> "
                "disco.agent_server.audio_config",
                "disco.tools.builtin.audio_overview -> "
                "disco.agent_server.tts_local",
                "disco.tools.builtin.preview -> "
                "disco.agent_server.preview_manager",
            }
        )

    def test_exact_edge_is_exempt(self):
        edge = {
            "source_module": "disco.tools.builtin.audio_overview",
            "target_module": "disco.agent_server.audio_config",
            "imported_names": [],
        }
        _, _, exemptions = real_context_facts()
        assert imports._edge_is_exempt(edge, exemptions) is True

    def test_wildcard_dm005_exemption_rejected(self):
        """A wildcard DM-005 exemption must not match."""
        edge = {
            "source_module": "disco.tools.builtin.audio_overview",
            "target_module": "disco.agent_server",
            "imported_names": [],
        }
        _, _, exemptions = real_context_facts()
        assert imports._edge_is_exempt(edge, exemptions) is False

        tmp = make_temp_repo()
        root = Path(tmp.name)
        write_import_authority(root)
        git_commit(root)
        assert imports.check_imports(root)["ok"]

        importlinter = (root / ".importlinter").read_text()
        exact = (
            "disco.tools.builtin.preview -> "
            "disco.agent_server.preview_manager"
        )
        write(root / ".importlinter", importlinter.replace(exact, f"{exact}.*"))
        problems = imports.check_imports(root)["problems"]
        assert_problem_contains(problems, "wildcard exemption")

        write(root / ".importlinter", importlinter.replace(f"    {exact}\n", ""))
        problems = imports.check_imports(root)["problems"]
        assert_problem_contains(problems, "missing exemptions")

        contexts = imports.load_contexts(root)
        contexts["dm005_upward_edge_observations"][2]["edge"] = (
            "disco.tools.builtin.other -> "
            "disco.agent_server.preview_manager"
        )
        write(root / "development/architecture/contexts.json", json.dumps(contexts))
        problems = imports.check_imports(root)["problems"]
        assert_problem_contains(problems, "DM-005 source observations drift")
        tmp.cleanup()

    def test_non_exempted_upward_edge_rejected(self):
        edge = {
            "source_module": "disco.tools.builtin.other",
            "target_module": "disco.agent_server",
            "imported_names": [],
        }
        _, _, exemptions = real_context_facts()
        assert imports._edge_is_exempt(edge, exemptions) is False


# ---------------------------------------------------------------------------
# Upward edge and top-sibling mutations
# ---------------------------------------------------------------------------


class TestUpwardAndSiblingEdges:
    def test_upward_import_detected(self):
        """A tools -> agent_server import is upward (layer 1 -> layer 0)."""
        _, layers, _ = real_context_facts()
        code = "from disco.agent_server import app\n"
        tree = ast.parse(code)
        imps = imports._extract_imports(tree, "disco.tools.builtin.fake")
        found = False
        for imp in imps:
            target_pkg = imports._dep_pkg(imp["target_module"], layers)
            source_pkg = imports._dep_pkg("disco.tools.builtin.fake", layers)
            if target_pkg and source_pkg:
                assert layers[target_pkg] < layers[source_pkg]
                found = True
        assert found

    def test_top_sibling_import_detected(self):
        """An import between top siblings (agent_server -> app_server) is detected."""
        _, layers, _ = real_context_facts()
        code = "from disco.app_server import app\n"
        tree = ast.parse(code)
        imps = imports._extract_imports(tree, "disco.agent_server.routes")
        found = False
        for imp in imps:
            target_pkg = imports._dep_pkg(imp["target_module"], layers)
            source_pkg = imports._dep_pkg("disco.agent_server.routes", layers)
            if target_pkg == "disco.app_server" and source_pkg == "disco.agent_server":
                found = True
        assert found

    def test_downward_import_not_upward(self):
        """A tools -> core import is downward, not upward."""
        _, layers, _ = real_context_facts()
        code = "from disco.core import events\n"
        tree = ast.parse(code)
        imps = imports._extract_imports(tree, "disco.tools.builtin.fake")
        for imp in imps:
            target_pkg = imports._dep_pkg(imp["target_module"], layers)
            source_pkg = imports._dep_pkg("disco.tools.builtin.fake", layers)
            if target_pkg and source_pkg:
                assert layers[target_pkg] >= layers[source_pkg]


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


class TestCycleDetection:
    def test_cycle_detected(self):
        """A simple A -> B -> A cycle must be detected."""
        edges = {("disco.core", "disco.tools"), ("disco.tools", "disco.core")}
        cycles = imports._detect_cycles(edges)
        assert len(cycles) >= 1

        tmp = make_temp_repo()
        root = Path(tmp.name)
        write_and_track(
            root,
            "current/packages/core/src/disco/core/cycle.py",
            "import disco.tools\n",
        )
        write_and_track(
            root,
            "current/packages/tools/src/disco/tools/cycle.py",
            "import disco.core\n",
        )
        write_import_authority(root)
        git_commit(root)
        result = imports.check_imports(root)
        assert_problem_contains(result["problems"], "dependency cycle")
        tmp.cleanup()

    def test_no_cycle_in_dag(self):
        """A DAG must produce no cycles."""
        edges = {
            ("disco.agent_server", "disco.tools"),
            ("disco.tools", "disco.retrieval"),
            ("disco.retrieval", "disco.core"),
        }
        cycles = imports._detect_cycles(edges)
        assert cycles == []


# ---------------------------------------------------------------------------
# Full check_imports boundary on temp repos
# ---------------------------------------------------------------------------


class TestCheckImportsBoundary:
    def test_zero_edge_graph_fails(self, tmp_path: Path) -> None:
        """A zero-edge accepted graph is invalid — the scanner is broken."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        with pytest.raises(FileNotFoundError):
            imports.check_imports(root)
        write(root / "development/architecture/contexts.json", "{}")
        malformed = imports.check_imports(root)
        assert not malformed["ok"]
        assert_problem_contains(malformed["problems"], "schema mismatch")
        write_import_authority(root, seed_dm005=False)
        git_commit(root)
        result = imports.check_imports(root)
        assert not result["ok"]
        assert_problem_contains(result["problems"], "zero edges")
        tmp.cleanup()

    def test_upward_edge_fails(self) -> None:
        """A non-exempted upward import edge must fail check_imports."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = "from disco.agent_server import app\n"
        write_and_track(
            root,
            "current/packages/tools/src/disco/tools/builtin/upward.py",
            code,
        )
        write_import_authority(root)
        git_commit(root)
        result = imports.check_imports(root)
        assert not result["ok"]
        assert_problem_contains(result["problems"], "upward import")
        tmp.cleanup()

        tmp = make_temp_repo()
        root = Path(tmp.name)
        write_and_track(
            root,
            "current/packages/core/src/disco/core/aliased_upward.py",
            "import importlib as loader\n"
            "from importlib import import_module as load\n"
            "module_alias = loader\n"
            "assigned = module_alias.import_module\n"
            "chained = assigned\n"
            "annotated: object = load\n"
            "builtin = __import__\n"
            'chained("disco.agent_server.app")\n'
            'annotated("disco.agent_server.auth")\n'
            'builtin("disco.agent_server.host_proxy")\n',
        )
        write_import_authority(root)
        git_commit(root)
        graph = imports.build_import_graph(root)
        aliased_targets = {
            edge["target_module"]
            for edge in graph["edges"]
            if edge["source_module"] == "disco.core.aliased_upward"
        }
        assert aliased_targets == {
            "disco.agent_server.app",
            "disco.agent_server.auth",
            "disco.agent_server.host_proxy",
        }
        result = imports.check_imports(root)
        assert not result["ok"]
        assert sum("upward import" in problem for problem in result["problems"]) >= 3
        tmp.cleanup()

    def test_top_sibling_edge_fails(self) -> None:
        """An import between top siblings must fail check_imports."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        code = "from disco.app_server import app\n"
        write_and_track(
            root,
            "current/packages/agent-server/src/disco/agent_server/routes/cross.py",
            code,
        )
        write_import_authority(root)
        git_commit(root)
        result = imports.check_imports(root)
        assert not result["ok"]
        assert_problem_contains(result["problems"], "top siblings")
        tmp.cleanup()

    def test_parse_error_fails_closed(self) -> None:
        """A parse error in a tracked file must fail check_imports."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        write_and_track(
            root,
            "current/packages/core/src/disco/core/bad.py",
            "def f(:\n    pass\n",
        )
        write_import_authority(root)
        git_commit(root)
        result = imports.check_imports(root)
        assert not result["ok"]
        assert_problem_contains(result["problems"], "parse error")
        tmp.cleanup()

        tmp = make_temp_repo()
        root = Path(tmp.name)
        write_and_track(
            root,
            "current/packages/core/src/disco/core/nonliteral.py",
            "import importlib as loader\n"
            "from importlib import import_module as load\n"
            "assigned = loader.import_module\n"
            "annotated: object = load\n"
            "builtin = __import__\n"
            "def dynamic(target: str) -> None:\n"
            "    assigned(target)\n"
            "    annotated(target)\n"
            "    builtin(target)\n",
        )
        write_import_authority(root)
        git_commit(root)
        result = imports.check_imports(root)
        assert not result["ok"]
        assert (
            sum(
                "non-literal dynamic import" in problem
                for problem in result["problems"]
            )
            == 3
        )
        tmp.cleanup()

    def test_downward_edges_pass(self) -> None:
        """Only downward edges in a temp repo should pass."""
        tmp = make_temp_repo()
        root = Path(tmp.name)
        write_and_track(
            root,
            "current/packages/agent-server/src/disco/agent_server/mod.py",
            "from disco.tools import builtin\nfrom disco.core import events\n",
        )
        write_and_track(
            root,
            "current/packages/tools/src/disco/tools/__init__.py",
            "from disco.core import events\n",
        )
        write_and_track(
            root,
            "current/packages/core/src/disco/core/__init__.py",
            "",
        )
        write_import_authority(root)
        git_commit(root)
        result = imports.check_imports(root)
        assert result["ok"], result["problems"]
        tmp.cleanup()
