"""Python scanner mutation tests for the architecture gate.

Every test builds a git-initialized temp tree, writes a mutated Python file,
and invokes the stable ``scan_file`` / ``scan_all`` boundary with ``root=``.
Each mutation asserts the intended rule name, not just any violation.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import (
    assert_rule_violated,
    dedent,
    git_commit,
    make_temp_repo,
    write,
    write_and_track,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "development" / "scripts"))
from architecture import python_scan  # noqa: E402

# ---------------------------------------------------------------------------
# Logical LOC and McCabe unit tests
# ---------------------------------------------------------------------------


class TestLogicalLoc:
    def test_logical_loc_counts_nonblank_noncomment(self):
        text = dedent("""\
            # comment line
            x = 1

            y = 2  # inline comment
            """)
        result = python_scan.logical_line_set(text)
        assert 2 in result
        assert 4 in result
        assert 1 not in result
        assert 3 not in result

    def test_multiline_string_counts_as_logical(self):
        text = dedent('''\
            x = """multi
            line
            string"""
            y = 1
            ''')
        result = python_scan.logical_line_set(text)
        assert 1 in result
        assert 2 in result
        assert 3 in result
        assert 4 in result

    def test_mccabe_complexity(self):
        code = dedent("""\
            def f(x):
                if x > 0:
                    return 1
                elif x < 0:
                    return -1
                else:
                    return 0
            """)
        tree = ast.parse(code)
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        c = python_scan.complexity(func)
        assert c == 3

    def test_qualified_name_map(self):
        code = dedent("""\
            class Foo:
                def method(self):
                    pass
                class Inner:
                    def inner_method(self):
                        pass
            def top_level():
                pass
            """)
        tree = ast.parse(code)
        qname_map = python_scan.build_qualified_name_map(tree)
        foo_qnames = [
            qname
            for (_line, _end, name), qname in qname_map.items()
            if name == "Foo"
        ]
        assert "Foo" in foo_qnames
        method_qnames = [
            qname
            for (_line, _end, name), qname in qname_map.items()
            if name == "method"
        ]
        assert "Foo.method" in method_qnames
        inner_qnames = [
            qname
            for (_line, _end, name), qname in qname_map.items()
            if name == "inner_method"
        ]
        assert "Foo.Inner.inner_method" in inner_qnames


# ---------------------------------------------------------------------------
# Oversized mutations — each must fail its intended rule
# ---------------------------------------------------------------------------


class TestOversizedPythonMutations:
    """Each oversized mutation must fail its specific rule."""

    def test_oversized_callable_fails(self, tmp_path: Path) -> None:
        code = "def big():\n" + "    pass\n" * 101
        path = write(tmp_path / "oversized.py", code)
        result = python_scan.scan_file(path, tmp_path)
        v = assert_rule_violated(result["violations"], "python_callable_logical_gt_100")
        assert v["value"] > 100
        assert v["limit"] == 100

    def test_oversized_class_fails(self, tmp_path: Path) -> None:
        code = "class Big:\n" + "    pass\n" * 351
        path = write(tmp_path / "oversized_class.py", code)
        result = python_scan.scan_file(path, tmp_path)
        v = assert_rule_violated(result["violations"], "python_class_logical_gt_350")
        assert v["value"] > 350
        assert v["limit"] == 350

    def test_oversized_production_module_fails(self, tmp_path: Path) -> None:
        code = "x = 1\n" * 701
        path = write(tmp_path / "oversized_module.py", code)
        result = python_scan.scan_file(path, tmp_path)
        v = assert_rule_violated(result["violations"], "python_or_harness_module_logical_gt_700")
        assert v["value"] > 700
        assert v["limit"] == 700

    def test_oversized_test_module_fails(self, tmp_path: Path) -> None:
        code = "x = 1\n" * 1201
        path = write(tmp_path / "test_oversized_module.py", code)
        result = python_scan.scan_file(path, tmp_path)
        v = assert_rule_violated(result["violations"], "test_module_logical_gt_1200")
        assert v["value"] > 1200
        assert v["limit"] == 1200

    def test_high_mccabe_fails(self, tmp_path: Path) -> None:
        code = dedent("""\
            def f(x):
                if x == 1: return 1
                if x == 2: return 2
                if x == 3: return 3
                if x == 4: return 4
                if x == 5: return 5
                if x == 6: return 6
                if x == 7: return 7
                if x == 8: return 8
                if x == 9: return 9
                if x == 10: return 10
                if x == 11: return 11
                if x == 12: return 12
                if x == 13: return 13
                if x == 14: return 14
                if x == 15: return 15
                if x == 16: return 16
                return 0
            """)
        path = write(tmp_path / "high_mccabe.py", code)
        result = python_scan.scan_file(path, tmp_path)
        v = assert_rule_violated(result["violations"], "python_callable_ast_mccabe_gt_15")
        assert v["value"] > 15
        assert v["limit"] == 15

    def test_oversized_route_handler_fails(self, tmp_path: Path) -> None:
        code = dedent("""\
            @router.get("/test")
            def handler(request):
                pass
            """) + "    x = 1\n" * 80
        path = write(tmp_path / "routes" / "handler.py", code)
        result = python_scan.scan_file(path, tmp_path)
        v = assert_rule_violated(result["violations"], "http_ws_route_handler_logical_gt_80")
        assert v["value"] > 80
        assert v["limit"] == 80

    def test_oversized_harness_oracle_module_fails(self, tmp_path: Path) -> None:
        code = "x = 1\n" * 401
        path = write(tmp_path / "harness" / "oracles" / "big.py", code)
        result = python_scan.scan_file(path, tmp_path)
        v = assert_rule_violated(result["violations"], "harness_oracle_module_logical_gt_400")
        assert v["value"] > 400
        assert v["limit"] == 400


# ---------------------------------------------------------------------------
# Parse error — fail-closed
# ---------------------------------------------------------------------------


class TestParseError:
    def test_parser_failure_fails_closed(self, tmp_path: Path) -> None:
        path = write(tmp_path / "syntax_error.py", "def f(:\n    pass\n")
        with pytest.raises(SyntaxError):
            python_scan.scan_file(path, tmp_path)

    def test_scan_all_collects_parse_errors(self, tmp_path: Path) -> None:
        tmp = make_temp_repo()
        root = Path(tmp.name)
        write_and_track(root, "current/packages/core/src/bad.py", "def f(:\n    pass\n")
        git_commit(root)
        result = python_scan.scan_all(root)
        assert result["errors"]
        assert any("bad.py" in e["path"] for e in result["errors"])
        tmp.cleanup()


# ---------------------------------------------------------------------------
# Positive controls — non-violations must not appear
# ---------------------------------------------------------------------------


class TestPythonScanPositives:
    def test_small_callable_passes(self, tmp_path: Path) -> None:
        code = "def small():\n    return 1\n"
        path = write(tmp_path / "small.py", code)
        result = python_scan.scan_file(path, tmp_path)
        assert result["violations"] == []

    def test_test_path_callable_skipped(self, tmp_path: Path) -> None:
        code = "def big():\n" + "    pass\n" * 101
        path = write(tmp_path / "test_big.py", code)
        result = python_scan.scan_file(path, tmp_path)
        callable_violations = [
            v for v in result["violations"]
            if v["rule"] == "python_callable_logical_gt_100"
        ]
        assert callable_violations == []

    def test_is_test_path_detection(self):
        assert python_scan.is_test_path("development/tests/architecture/test_gate.py")
        assert python_scan.is_test_path("current/packages/core/tests/test_foo.py")
        assert python_scan.is_test_path("test_foo.py")
        assert not python_scan.is_test_path("current/packages/core/src/module.py")
