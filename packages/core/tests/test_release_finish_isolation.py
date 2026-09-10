"""WO-10 — the durable finish-path non-regression guard.

The self-host release plane (WO-2..7) is computed ON REQUEST from a project's
immutable contents; NOTHING about it runs at finish time. The load-bearing
guarantee is that the finish path gained **no container-build dependency**: no
module under ``disco.core.loop.finish`` may import the two container-build
modules ``disco.core.release.local_compose`` (the compose/Dockerfile emitter) or
``disco.core.release.detect`` (the release detector). If a future edit wires
release detection or overlay emission into a finish gate, this guard fails.

This is a **static import scan** (AST over the finish package's source files), NOT
an import-and-run of the finish modules — importing them would execute
``finish/__init__`` and its mixins, which is exactly what the guard must avoid.
The scan resolves both absolute (``import disco.core.release.detect``,
``from disco.core.release import detect``) and relative (``from ...release.detect
import x``) import forms to an absolute module path before matching.

(The complementary "campaign diff touches no path under ``core/loop/finish/``"
check is the orchestrator's job via git; this guard is the durable, in-suite half
that keeps holding long after the campaign lands.)
"""

from __future__ import annotations

import ast
from pathlib import Path

# The two container-build modules the finish path must never depend on. A match is
# the module itself OR any submodule of it (``startswith(forbidden + ".")``).
_FORBIDDEN = frozenset(
    {
        "disco.core.release.local_compose",
        "disco.core.release.detect",
    }
)

# ``packages/core/src`` — the import root every scanned module name is relative to.
_CORE_SRC = Path(__file__).resolve().parents[1] / "src"
_FINISH_DIR = _CORE_SRC / "disco" / "core" / "loop" / "finish"

# The package a finish source file lives in (used to resolve relative imports in
# the scanner self-tests). Every file directly under ``finish/`` shares it.
_FINISH_PACKAGE = "disco.core.loop.finish"


def _module_package(py_file: Path) -> str:
    """The dotted package a finish source file belongs to (its containing
    package), derived purely from the path — no import."""
    rel = py_file.relative_to(_CORE_SRC).with_suffix("")
    return ".".join(rel.parts[:-1])


def _resolve(module: str | None, level: int, package: str) -> str | None:
    """Resolve an import target to an absolute dotted module name.

    ``level`` 0 is an absolute import (``module`` is already absolute). ``level``
    N>0 is a relative import anchored at ``package``, dropping ``level - 1``
    trailing components (Python's own rule: ``from .`` stays in ``package``,
    ``from ..`` drops one, …), then appending ``module`` if present."""
    if level == 0:
        return module
    base = package.split(".") if package else []
    drop = level - 1
    if drop > len(base):
        return None
    anchor = base[: len(base) - drop] if drop else list(base)
    tail = module.split(".") if module else []
    combined = anchor + tail
    return ".".join(combined) if combined else None


def _imported_modules(source: str, package: str) -> set[str]:
    """Every absolute module name an ``import`` statement in ``source`` references.

    For ``from X import a, b`` both ``X`` and ``X.a`` / ``X.b`` are recorded, so a
    ``from disco.core.release import detect`` is caught even though the module
    node is only ``disco.core.release``."""
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(node.module, node.level, package)
            if base is None:
                continue
            found.add(base)
            for alias in node.names:
                found.add(f"{base}.{alias.name}")
    return found


def _forbidden_hits(modules: set[str]) -> set[str]:
    """The subset of ``modules`` that is (or is a submodule of) a forbidden
    container-build module."""
    hits: set[str] = set()
    for name in modules:
        for forbidden in _FORBIDDEN:
            if name == forbidden or name.startswith(forbidden + "."):
                hits.add(name)
    return hits


# ---- the guard ----------------------------------------------------------------


def test_finish_package_is_present_and_scannable() -> None:
    """The guard is only meaningful if it actually found the finish package and
    some modules to scan (a moved/renamed package must fail loudly, not vacuously
    pass)."""
    assert _FINISH_DIR.is_dir(), f"finish package not found at {_FINISH_DIR}"
    modules = sorted(_FINISH_DIR.rglob("*.py"))
    assert modules, f"no finish modules to scan under {_FINISH_DIR}"


def test_no_finish_module_imports_release_container_build_modules() -> None:
    """No module under ``disco.core.loop.finish`` imports the release detector or
    the local-compose overlay emitter — the finish path carries no container-build
    dependency (WO-10 acceptance #1)."""
    offenders: dict[str, set[str]] = {}
    for py_file in sorted(_FINISH_DIR.rglob("*.py")):
        package = _module_package(py_file)
        modules = _imported_modules(py_file.read_text(encoding="utf-8"), package)
        hits = _forbidden_hits(modules)
        if hits:
            offenders[str(py_file.relative_to(_CORE_SRC))] = hits
    assert not offenders, (
        "a finish module gained a container-build dependency (regression): "
        f"{offenders}. The self-host release plane must stay computed-on-request; "
        "nothing under core/loop/finish/ may import "
        "disco.core.release.local_compose or disco.core.release.detect."
    )


# ---- scanner self-tests (prove the guard is not vacuous) ----------------------


def test_scanner_flags_absolute_from_import() -> None:
    hits = _forbidden_hits(
        _imported_modules("from disco.core.release import detect\n", _FINISH_PACKAGE)
    )
    assert hits == {"disco.core.release.detect"}


def test_scanner_flags_absolute_plain_import() -> None:
    hits = _forbidden_hits(
        _imported_modules("import disco.core.release.local_compose\n", _FINISH_PACKAGE)
    )
    assert hits == {"disco.core.release.local_compose"}


def test_scanner_flags_relative_import() -> None:
    # `from ...release.detect import detect_release` inside disco.core.loop.finish.*
    # resolves to disco.core.release.detect (level 3 drops loop + finish).
    hits = _forbidden_hits(
        _imported_modules("from ...release.detect import detect_release\n", _FINISH_PACKAGE)
    )
    assert "disco.core.release.detect" in hits


def test_scanner_ignores_the_neutral_spec_and_stdlib() -> None:
    # The neutral, container-free `spec` module and stdlib imports are allowed —
    # only the two container-build modules are forbidden.
    source = "from disco.core.release.spec import ReleaseSpec\nimport os\nimport json\n"
    assert _forbidden_hits(_imported_modules(source, _FINISH_PACKAGE)) == set()
