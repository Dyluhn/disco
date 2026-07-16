"""Permanent regressions for the R7 Batch-2 reopen (four Python release-proof defects).

An independent adversary found four real defects in the single Python release-detection proof;
each is fixed at its root and pinned here as an asserting (never printing) regression:

* D1 — a module/package identity collision (``real.py`` vs ``real/__init__.py`` both dotted
  ``real``) produced a PYTHONHASHSEED-dependent verdict; the only sound verdict is needs_review.
* D2 — a runnable package-``__init__`` re-export (``from .impl import app`` inside ``pkg``) was
  over-rejected because the recursion climbed relative imports from ``()`` instead of ``pkg``.
* D3 — unsatisfiable PEP 440 wildcard intersections (``==0.30.*,!=0.30.*``) slipped through as a
  candidate; certification is now witness-based over the supported grammar.
* D4 — a reversed-but-valid PEP 508 marker (``"3.13" == python_version``) was over-rejected.

Every case is self-contained in the repo (no evidence-archive imports); the end-to-end rows drive
the real ``detect_release`` with no mocks.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import textwrap
from typing import Any

import pytest
from disco.core.release.detect import (
    Provenance,
    _workspace_python_modules,
    detect_release,
)
from disco.core.release.python_proof import (
    active_requirement_name,
    python_entrypoint_error,
    requires_python_supported,
)
from disco.core.release.spec import ReleaseAssessment, ReleaseIntent, RuntimeStrategy

_CAND = ReleaseAssessment.candidate
_NR = ReleaseAssessment.needs_review
_GOOD_APP = "from fastapi import FastAPI\napp = FastAPI()\n"
_REQS = "fastapi==0.111\nuvicorn==0.30\n"


def _detect(
    files: dict[str, str], start: tuple[str, ...] = ("uvicorn", "main:app")
) -> ReleaseAssessment:
    intent = ReleaseIntent(runtime=RuntimeStrategy.python, start_cmd=start)
    return detect_release(dict(files), intent=intent, provenance=Provenance()).assessment


def _proof(source: str, attribute: str = "app", **kwargs: Any) -> str | None:
    kwargs.setdefault("server", "uvicorn")
    kwargs.setdefault("factory", False)
    kwargs.setdefault("installed_packages", frozenset({"fastapi"}))
    return python_entrypoint_error(source, attribute, **kwargs)


# =======================================================================================
# FIX 1 — module/package identity collision (CRITICAL false candidate).
# ``real.py`` (runnable) and ``real/__init__.py`` (``app = None``) both dot to ``real``;
# CPython binds the PACKAGE, so the only sound verdict is needs_review — regardless of
# frozenset iteration order (PYTHONHASHSEED) or file insertion order.
# =======================================================================================

_COLLISION_GOOD_FILE_BAD_PKG = {
    "requirements.txt": _REQS,
    "main.py": "from real import app\n",
    "real.py": _GOOD_APP,  # runnable module
    "real/__init__.py": "app = None\n",  # unrunnable package (CPython prefers THIS)
}
_COLLISION_BAD_FILE_GOOD_PKG = {
    "requirements.txt": _REQS,
    "main.py": "from real import app\n",
    "real.py": "app = None\n",
    "real/__init__.py": _GOOD_APP,
}

_SEED_SWEEP_SCRIPT = textwrap.dedent(
    """
    from disco.core.release.detect import detect_release, Provenance
    from disco.core.release.spec import ReleaseIntent, RuntimeStrategy
    files = {
        "requirements.txt": "fastapi==0.111\\nuvicorn==0.30\\n",
        "main.py": "from real import app\\n",
        "real.py": "from fastapi import FastAPI\\napp = FastAPI()\\n",
        "real/__init__.py": "app = None\\n",
    }
    intent = ReleaseIntent(runtime=RuntimeStrategy.python, start_cmd=("uvicorn", "main:app"))
    print(detect_release(dict(files), intent=intent, provenance=Provenance()).assessment.value)
    """
)


def test_collision_is_needs_review_both_arrangements() -> None:
    # Good-file/bad-package AND bad-file/good-package both fail closed: the package always wins.
    assert _detect(_COLLISION_GOOD_FILE_BAD_PKG) is _NR
    assert _detect(_COLLISION_BAD_FILE_GOOD_PKG) is _NR


def test_collision_verdict_is_insertion_order_invariant() -> None:
    keys = list(_COLLISION_GOOD_FILE_BAD_PKG)
    for order in itertools.permutations(keys):
        files = {key: _COLLISION_GOOD_FILE_BAD_PKG[key] for key in order}
        assert _detect(files) is _NR


def test_collision_verdict_is_hashseed_invariant() -> None:
    # The defect was a frozenset-iteration (PYTHONHASHSEED) dependent verdict; sweep >=20 seeds in
    # subprocesses (the only way to vary the seed) and assert a single, stable needs_review.
    verdicts: set[str] = set()
    for seed in range(25):
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        completed = subprocess.run(
            [sys.executable, "-c", _SEED_SWEEP_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        verdicts.add(completed.stdout.strip())
    assert verdicts == {"needs_review"}


def test_collision_fails_closed_at_proof_level_direct_and_reexport() -> None:
    # A module-scope dependency import that resolves through an ambiguous identity fails closed
    # even though the target itself is a real constructor.
    assert (
        _proof(
            "from real import helper\n" + _GOOD_APP,
            local_modules=frozenset({"real"}),
            local_module_paths=frozenset({"real"}),
            ambiguous_modules=frozenset({"real"}),
        )
        is not None
    )
    # A re-export target that resolves through an ambiguous identity fails closed...
    assert (
        _proof(
            "from real import app\n",
            local_modules=frozenset({"real"}),
            local_module_paths=frozenset({"real"}),
            local_module_sources={"real": _GOOD_APP},
            ambiguous_modules=frozenset({"real"}),
        )
        is not None
    )
    # ...while the identical shape WITHOUT the ambiguity proves out (control: fix is not blanket).
    assert (
        _proof(
            "from real import app\n",
            local_modules=frozenset({"real"}),
            local_module_paths=frozenset({"real"}),
            local_module_sources={"real": _GOOD_APP},
        )
        is None
    )


def test_ambiguous_traversal_through_parent_package_fails_closed() -> None:
    # ``pkg`` is ambiguous (pkg.py vs pkg/__init__.py); importing ``pkg.sub`` traverses it.
    assert (
        _proof(
            "from pkg.sub import helper\n" + _GOOD_APP,
            local_modules=frozenset({"pkg"}),
            local_module_paths=frozenset({"pkg", "pkg.sub"}),
            ambiguous_modules=frozenset({"pkg"}),
        )
        is not None
    )


@pytest.mark.parametrize(
    ("files", "start"),
    [
        # single module
        ({"requirements.txt": _REQS, "main.py": _GOOD_APP}, ("uvicorn", "main:app")),
        # single package (the target is a package __init__)
        ({"requirements.txt": _REQS, "main/__init__.py": _GOOD_APP}, ("uvicorn", "main:app")),
        # normal package + submodule (no collision)
        (
            {"requirements.txt": _REQS, "pkg/__init__.py": "", "pkg/main.py": _GOOD_APP},
            ("uvicorn", "pkg.main:app"),
        ),
    ],
)
def test_noncolliding_ordinary_shapes_stay_candidate(
    files: dict[str, str], start: tuple[str, ...]
) -> None:
    assert _detect(files, start=start) is _CAND


# =======================================================================================
# FIX 2 — package ``__init__`` re-export was over-rejected. A runnable package init that
# relatively re-exports a real app must become a candidate; missing/bad/cyclic/beyond-top
# targets and ambiguous identities still fail closed.
# =======================================================================================


def _pkg_proof(
    pkg_init: str, *, impl: str | None = None, extra_dotted: frozenset[str] = frozenset()
) -> str | None:
    sources = {"pkg": pkg_init}
    if impl is not None:
        sources["pkg.impl"] = impl
    return _proof(
        "from pkg import app\n",
        local_modules=frozenset({"main", "pkg"}),
        local_module_paths=frozenset({"main", "pkg", "pkg.impl"}) | extra_dotted,
        local_module_sources=sources,
        package_modules=frozenset({"pkg"}),
    )


def test_package_init_reexport_runnable_is_candidate() -> None:
    assert _pkg_proof("from .impl import app\n", impl=_GOOD_APP) is None


def test_package_init_reexport_nested_package_chain_is_candidate() -> None:
    # pkg/__init__ -> pkg.sub/__init__ -> pkg.sub.impl, both inits are packages.
    assert (
        _proof(
            "from pkg import app\n",
            local_modules=frozenset({"main", "pkg"}),
            local_module_paths=frozenset({"main", "pkg", "pkg.sub", "pkg.sub.impl"}),
            local_module_sources={
                "pkg": "from .sub import app\n",
                "pkg.sub": "from .impl import app\n",
                "pkg.sub.impl": _GOOD_APP,
            },
            package_modules=frozenset({"pkg", "pkg.sub"}),
        )
        is None
    )


@pytest.mark.parametrize(
    ("pkg_init", "impl"),
    [
        ("from .missing import app\n", None),  # missing destination
        ("from .impl import app\n", "app = None\n"),  # bad target (not an app)
        ("from .impl import app\n", "from pkg import app\n"),  # cycle back to pkg
        ("from ..impl import app\n", None),  # beyond top-level relative
    ],
)
def test_package_init_reexport_bad_targets_fail_closed(pkg_init: str, impl: str | None) -> None:
    assert _pkg_proof(pkg_init, impl=impl) is not None


def test_package_init_ambiguous_reexport_still_fails_closed() -> None:
    # Even a runnable package init re-export fails closed when ``pkg`` is an ambiguous identity.
    assert (
        _proof(
            "from pkg import app\n",
            local_modules=frozenset({"main", "pkg"}),
            local_module_paths=frozenset({"main", "pkg", "pkg.impl"}),
            local_module_sources={"pkg": "from .impl import app\n", "pkg.impl": _GOOD_APP},
            package_modules=frozenset({"pkg"}),
            ambiguous_modules=frozenset({"pkg"}),
        )
        is not None
    )


def test_e2e_package_init_reexport() -> None:
    good = {
        "requirements.txt": _REQS,
        "main.py": "from pkg import app\n",
        "pkg/__init__.py": "from .impl import app\n",
        "pkg/impl.py": _GOOD_APP,
    }
    assert _detect(good) is _CAND
    bad = {**good, "pkg/impl.py": "app = None\n"}
    assert _detect(bad) is _NR


# =======================================================================================
# FIX 3 — unsatisfiable PEP 440 wildcard intersections (HIGH false candidate). Requirement
# names are certified only when a real installable witness version satisfies the whole set.
# =======================================================================================

_REJECT_SPECS = [
    "==1.*,==2.*",
    "==1.*,<1",
    "==1.2.*,<1.2",
    "~=1.4,!=1.*",
    "==0.30.*,!=0.30.*",
    "==1.*,!=1.*",
]
_ACCEPT_SPECS = [
    "==1.*,!=1.0.*",
    "==1.2.*,>=1.2,<1.3",
    "~=1.4",
    ">=1.0.0rc1",
    "==1.0rc1",
    "[standard]",
    ">=1.0,<2",
]


@pytest.mark.parametrize("spec", _REJECT_SPECS)
def test_unsatisfiable_specifier_is_rejected(spec: str) -> None:
    with pytest.raises(ValueError) as raised:
        active_requirement_name(f"uvicorn{spec}")
    assert len(str(raised.value)) <= 100


@pytest.mark.parametrize("spec", _ACCEPT_SPECS)
def test_satisfiable_specifier_is_accepted(spec: str) -> None:
    assert active_requirement_name(f"uvicorn{spec}") == "uvicorn"


def test_e2e_impossible_dep_spec_is_needs_review() -> None:
    for spec in _REJECT_SPECS:
        files = {"requirements.txt": f"fastapi==0.111\nuvicorn{spec}\n", "main.py": _GOOD_APP}
        assert _detect(files) is _NR


def test_e2e_valid_compound_dep_spec_is_candidate() -> None:
    files = {"requirements.txt": "fastapi>=0.100,<1\nuvicorn>=0.30,<1\n", "main.py": _GOOD_APP}
    assert _detect(files) is _CAND


def test_requires_python_supported_is_intact() -> None:
    assert requires_python_supported(">=3.9") is True
    assert requires_python_supported("<3.13") is False


# =======================================================================================
# FIX 4 — reversed valid PEP 508 marker (over-rejection). ``"3.13" == python_version`` is
# valid and equals ``python_version == "3.13"``; ordered operators invert on reversal.
# =======================================================================================


@pytest.mark.parametrize(
    ("marker", "active"),
    [
        ('"3.13" == python_version', True),
        ('"3.99" == python_version', False),
        ('"3.13" != python_version', False),
        ('"3.99" != python_version', True),
        ('"3.10" < python_version', True),
        ('"3.99" < python_version', False),
        ('"3.13" <= python_version', True),
        ('"3.20" >= python_version', True),
        ('"3.13" >= python_version', True),
        ('"3.10" > python_version', False),
    ],
)
def test_reversed_python_version_marker(marker: str, active: bool) -> None:
    result = active_requirement_name(f"uvicorn; {marker}")
    assert result == ("uvicorn" if active else None)


@pytest.mark.parametrize(
    "marker",
    [
        '"3.13" === python_version',
        '"3.13" in python_version',
        '"3.13" not in python_version',
        '"3.13" ~= python_version',
    ],
)
def test_reversed_unsound_python_version_marker_fails_closed(marker: str) -> None:
    with pytest.raises(ValueError):
        active_requirement_name(f"uvicorn; {marker}")


def test_reversed_marker_on_moving_var_fails_closed() -> None:
    with pytest.raises(ValueError):
        active_requirement_name('uvicorn; "x86_64" == platform_machine')


def test_e2e_reversed_valid_marker_is_candidate() -> None:
    files = {
        "requirements.txt": 'fastapi==0.111\nuvicorn; "3.13" == python_version\n',
        "main.py": _GOOD_APP,
    }
    assert _detect(files) is _CAND


def test_e2e_reversed_inactive_marker_dep_absent() -> None:
    # A valid-but-false reversed marker legitimately drops uvicorn; the server dep is then unmet.
    files = {
        "requirements.txt": 'fastapi==0.111\nuvicorn; "3.99" == python_version\n',
        "main.py": _GOOD_APP,
    }
    assert _detect(files) is _NR


def test_e2e_reversed_moving_marker_is_needs_review() -> None:
    files = {
        "requirements.txt": 'fastapi==0.111\nuvicorn; "x86_64" == platform_machine\n',
        "main.py": _GOOD_APP,
    }
    assert _detect(files) is _NR


def test_reqfile_pyproject_marker_parity() -> None:
    # Both the requirements-file line path and the pyproject dependency-string path validate
    # through the same ``active_requirement_name`` grammar, so a reversed marker resolves the
    # same on both surfaces.
    line = 'uvicorn; "3.13" == python_version'
    assert active_requirement_name(line) is not None
    pyproject = (
        "[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n"
        "[project]\nname='x'\nversion='1.0.0'\nrequires-python='>=3.9'\n"
        "dependencies=['fastapi', 'uvicorn; \"3.13\" == python_version']\n"
    )
    files = {"pyproject.toml": pyproject, "main.py": _GOOD_APP}
    assert _detect(files) is _CAND


# =======================================================================================
# Batch-3 reopen (five FRESH adversary holes).
# H1 — a module file ``real.py`` shadowing a package/namespace dir ``real/`` of the same
#      name (``import real.sub`` -> "'real' is not a package"): FALSE candidate.
# H2/H3 — an ancestor package ``__init__`` that ``raise``\\s or imports a missing dist is
#      run BEFORE the submodule target, making it unreachable: FALSE candidates.
# H4/H5 — narrow satisfiable intervals (``>1.2,<1.2.1`` -> ``1.2.0.1``): OVER-rejected.
# =======================================================================================

# ---- H1: module-file / package-dir shadow ----

_H1_SHADOW = {
    "requirements.txt": "uvicorn\nfastapi\n",
    "real.py": "x = 1\n",  # a regular module FILE...
    "real/sub.py": _GOOD_APP,  # ...that shadows the real/ package DIR
}

_H1_SEED_SCRIPT = textwrap.dedent(
    """
    from disco.core.release.detect import detect_release, Provenance
    from disco.core.release.spec import ReleaseIntent, RuntimeStrategy
    files = {
        "requirements.txt": "uvicorn\\nfastapi\\n",
        "real.py": "x = 1\\n",
        "real/sub.py": "from fastapi import FastAPI\\napp = FastAPI()\\n",
    }
    intent = ReleaseIntent(runtime=RuntimeStrategy.python, start_cmd=("uvicorn", "real.sub:app"))
    print(detect_release(dict(files), intent=intent, provenance=Provenance()).assessment.value)
    """
)


def test_h1_module_shadows_package_dir_is_needs_review() -> None:
    assert _detect(_H1_SHADOW, start=("uvicorn", "real.sub:app")) is _NR


def test_h1_shadow_is_hashseed_invariant() -> None:
    verdicts: set[str] = set()
    for seed in range(25):
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        completed = subprocess.run(
            [sys.executable, "-c", _H1_SEED_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        verdicts.add(completed.stdout.strip())
    assert verdicts == {"needs_review"}


def test_h1_workspace_modules_flags_shadow_and_not_positive_controls() -> None:
    # Root-cause locus: the shadow name is ``ambiguous``; the benign shapes are not.
    _roots, _dotted, _sources, ambiguous, _packages = _workspace_python_modules(
        {"real.py": "x = 1\n", "real/sub.py": _GOOD_APP}, "."
    )
    assert "real" in ambiguous
    for benign in (
        {"pkg/__init__.py": "", "pkg/mod.py": _GOOD_APP},  # normal package
        {"pkg/mod.py": _GOOD_APP},  # namespace package
        {"main.py": _GOOD_APP},  # single module
    ):
        _r, _d, _s, amb, _p = _workspace_python_modules(benign, ".")
        assert amb == frozenset()


@pytest.mark.parametrize(
    ("files", "start"),
    [
        (
            {"requirements.txt": _REQS, "pkg/__init__.py": "", "pkg/mod.py": _GOOD_APP},
            ("uvicorn", "pkg.mod:app"),
        ),
        ({"requirements.txt": _REQS, "pkg/mod.py": _GOOD_APP}, ("uvicorn", "pkg.mod:app")),
        ({"requirements.txt": _REQS, "main.py": _GOOD_APP}, ("uvicorn", "main:app")),
    ],
)
def test_h1_positive_controls_stay_candidate(files: dict[str, str], start: tuple[str, ...]) -> None:
    assert _detect(files, start=start) is _CAND


# ---- H2/H3: ancestor package __init__ side effects ----


def test_h2_ancestor_init_raises_is_needs_review() -> None:
    files = {
        "requirements.txt": _REQS,
        "pkg/__init__.py": "raise RuntimeError('boom')\n",
        "pkg/mod.py": _GOOD_APP,
    }
    assert _detect(files, start=("uvicorn", "pkg.mod:app")) is _NR


def test_h3_ancestor_init_bad_import_is_needs_review() -> None:
    files = {
        "requirements.txt": _REQS,
        "pkg/__init__.py": "import totally_absent_distribution_xyz\n",
        "pkg/mod.py": _GOOD_APP,
    }
    assert _detect(files, start=("uvicorn", "pkg.mod:app")) is _NR


def test_h2_h3_deep_ancestor_abort_is_needs_review() -> None:
    # A bad ancestor two levels up (a/__init__ imported before a.b.c) must still fail closed.
    files = {
        "requirements.txt": _REQS,
        "a/__init__.py": "import totally_absent_distribution_xyz\n",
        "a/b/__init__.py": "",
        "a/b/c.py": _GOOD_APP,
    }
    assert _detect(files, start=("uvicorn", "a.b.c:app")) is _NR


@pytest.mark.parametrize(
    "pkg_init",
    ["", "# just a comment\n", "__all__ = []\n", '"""docstring only"""\n'],
)
def test_h2_h3_benign_ancestor_init_stays_candidate(pkg_init: str) -> None:
    files = {"requirements.txt": _REQS, "pkg/__init__.py": pkg_init, "pkg/mod.py": _GOOD_APP}
    assert _detect(files, start=("uvicorn", "pkg.mod:app")) is _CAND


def test_h2_h3_deep_benign_chain_stays_candidate() -> None:
    files = {
        "requirements.txt": _REQS,
        "a/__init__.py": "",
        "a/b/__init__.py": "__all__ = []\n",
        "a/b/c.py": _GOOD_APP,
    }
    assert _detect(files, start=("uvicorn", "a.b.c:app")) is _CAND


def _ancestor_proof(pkg_init: str) -> str | None:
    return python_entrypoint_error(
        _GOOD_APP,
        "app",
        server="uvicorn",
        factory=False,
        installed_packages=frozenset({"fastapi"}),
        local_modules=frozenset({"pkg"}),
        local_module_paths=frozenset({"pkg", "pkg.mod"}),
        local_module_sources={"pkg": pkg_init, "pkg.mod": _GOOD_APP},
        target_package=("pkg",),
        target_module="pkg.mod",
        package_modules=frozenset({"pkg"}),
    )


def test_ancestor_validation_at_proof_level() -> None:
    # Abort and unproven-import ancestors fail closed; a benign ancestor proves out.
    assert _ancestor_proof("raise RuntimeError('x')\n") is not None
    assert _ancestor_proof("import totally_absent_distribution_xyz\n") is not None
    assert _ancestor_proof("assert False\n") is not None
    assert _ancestor_proof("__all__ = []\n") is None


# ---- H4/H5: narrow satisfiable intervals over-rejected ----


@pytest.mark.parametrize(
    "spec",
    ["foo>1.2,<1.2.1", "foo>1,<1.0.1", "foo>1.0,<1.0.0.2", "foo>=2.5,<2.5.0.1"],
)
def test_h4_h5_narrow_satisfiable_interval_is_accepted(spec: str) -> None:
    assert active_requirement_name(spec) == "foo"


def test_h4_h5_narrow_interval_e2e_is_candidate() -> None:
    files = {"requirements.txt": "fastapi>1.2,<1.2.1\nuvicorn>1,<1.0.1\n", "main.py": _GOOD_APP}
    assert _detect(files) is _CAND


@pytest.mark.parametrize(
    "spec",
    [
        "foo==0.30.*,!=0.30.*",
        "foo==1.*,!=1.*",
        "foo>1.2,<1.2",
        "foo>1,<1",
        "foo==1.*,==2.*",
        "foo>2,<1",
    ],
)
def test_h4_h5_deeper_witnesses_do_not_mint_false_candidates(spec: str) -> None:
    with pytest.raises(ValueError):
        active_requirement_name(spec)


# =======================================================================================
# Batch-4 reopen (two inherited false candidates the ancestor path also exhibits).
# G1 — empty-container falsy asserts (``assert ()``/``[]``/``{}``) raise AssertionError at
#      import but evaded the falsy-constant-only abort detector: FALSE candidate.
# G2 — a transitive undeclared dependency one hop behind a local import (``import helper``
#      where helper imports a dist absent from the plan) evaded the existence-only local
#      import check: FALSE candidate. A proven local import is now recursively import-safe.
# =======================================================================================

_RAW_ASGI = "async def app(scope, receive, send):\n    pass\n"


# ---- G1: statically-falsy asserts (empty displays + falsy literals) ----


@pytest.mark.parametrize(
    "assertion",
    [
        "assert ()",
        "assert []",
        "assert {}",
        "assert False",
        "assert None",
        "assert 0",
        "assert 0.0",
        'assert ""',
        "assert b''",
    ],
)
def test_g1_statically_falsy_assert_is_needs_review(assertion: str) -> None:
    files = {"requirements.txt": _REQS, "main.py": assertion + "\n" + _GOOD_APP}
    assert _detect(files) is _NR


@pytest.mark.parametrize(
    "assertion",
    [
        "assert 1",
        'assert "x"',
        "assert (1,)",  # non-empty tuple is truthy
        "assert [1]",  # non-empty list is truthy
        "assert {1: 2}",  # non-empty dict is truthy
        "assert set()",  # a Call is not statically decidable -> left to the runtime probe
        "assert frozenset()",
    ],
)
def test_g1_non_falsy_or_undecidable_assert_stays_candidate(assertion: str) -> None:
    files = {"requirements.txt": _REQS, "main.py": assertion + "\n" + _GOOD_APP}
    assert _detect(files) is _CAND


def test_g1_empty_display_assert_at_proof_level() -> None:
    for falsy in ("assert ()", "assert []", "assert {}"):
        assert (
            python_entrypoint_error(
                falsy + "\n" + _GOOD_APP,
                "app",
                server="uvicorn",
                factory=False,
                installed_packages=frozenset({"fastapi"}),
            )
            is not None
        )


# ---- G2: recursive import safety (transitive undeclared dependency) ----

_MISSING = "import totally_absent_dist_xyz\n"


def test_g2_transitive_missing_dep_via_ancestor_is_needs_review() -> None:
    # __init__ imports sub; sub imports a dist absent from the plan -> ModuleNotFound at boot.
    files = {
        "requirements.txt": "uvicorn\n",
        "pkg/__init__.py": "from . import sub\n",
        "pkg/sub.py": _MISSING,
        "pkg/mod.py": _RAW_ASGI,
    }
    assert _detect(files, start=("uvicorn", "pkg.mod:app")) is _NR


def test_g2_transitive_missing_dep_at_target_scope_is_needs_review() -> None:
    files = {
        "requirements.txt": _REQS,
        "helper.py": _MISSING,
        "main.py": "import helper\n" + _GOOD_APP,
    }
    assert _detect(files) is _NR


def test_g2_transitive_missing_dep_via_reexport_is_needs_review() -> None:
    files = {
        "requirements.txt": _REQS,
        "helper.py": _MISSING,
        "real.py": "import helper\n" + _GOOD_APP,
        "main.py": "from real import app\n",
    }
    assert _detect(files) is _NR


def test_g2_deep_chain_broken_at_tail_is_needs_review() -> None:
    files = {
        "requirements.txt": _REQS,
        "a.py": "import b\n",
        "b.py": "import c\n",
        "c.py": _MISSING,
        "main.py": "import a\n" + _GOOD_APP,
    }
    assert _detect(files) is _NR


@pytest.mark.parametrize(
    ("files", "start"),
    [
        # a local module importing stdlib
        (
            {
                "requirements.txt": _REQS,
                "helper.py": "import os, json\n",
                "main.py": "import helper\n" + _GOOD_APP,
            },
            ("uvicorn", "main:app"),
        ),
        # a local module importing an INSTALLED dep that IS in the plan
        (
            {
                "requirements.txt": _REQS,
                "helper.py": "import fastapi\n",
                "main.py": "import helper\n" + _GOOD_APP,
            },
            ("uvicorn", "main:app"),
        ),
        # a deep local chain where every hop resolves
        (
            {
                "requirements.txt": _REQS,
                "a.py": "import b\n",
                "b.py": "import c\n",
                "c.py": "import os\n",
                "main.py": "import a\n" + _GOOD_APP,
            },
            ("uvicorn", "main:app"),
        ),
        # a benign import cycle among resolving local modules
        (
            {
                "requirements.txt": _REQS,
                "x.py": "import y\n",
                "y.py": "import x\n",
                "main.py": "import x\n" + _GOOD_APP,
            },
            ("uvicorn", "main:app"),
        ),
    ],
)
def test_g2_positive_controls_stay_candidate(files: dict[str, str], start: tuple[str, ...]) -> None:
    assert _detect(files, start=start) is _CAND


def test_g2_recursive_import_proof_at_proof_level() -> None:
    def proof(helper_source: str) -> str | None:
        return python_entrypoint_error(
            "import helper\n" + _GOOD_APP,
            "app",
            server="uvicorn",
            factory=False,
            installed_packages=frozenset({"fastapi"}),
            local_modules=frozenset({"main", "helper"}),
            local_module_paths=frozenset({"main", "helper"}),
            local_module_sources={"helper": helper_source},
            target_module="main",
        )

    assert proof(_MISSING) is not None  # transitive missing dep -> fail closed
    assert proof("import fastapi\n") is None  # resolves to an installed dep -> proves out


def test_g2_no_sources_preserves_existence_only_behavior() -> None:
    # A unit caller that supplies NO sources map keeps the pre-existing existence-only behavior:
    # a local import is proven by existence alone (there is nothing to recurse into).
    assert (
        python_entrypoint_error(
            "import helper\n" + _GOOD_APP,
            "app",
            server="uvicorn",
            factory=False,
            installed_packages=frozenset({"fastapi"}),
            local_modules=frozenset({"main", "helper"}),
            local_module_paths=frozenset({"main", "helper"}),
        )
        is None
    )


# =======================================================================================
# Batch-5 reopen (four issues a third adversary found in the newest code).
# G2A — my G2 recursion checked a dependency's IMPORTS but never ran the abort detector on
#       the dependency ITSELF (a dep that ``raise``/``assert False``/``sys.exit``): FALSE candidate.
# G2N — ``from helper import missing_name`` where helper exists but never binds the name: FALSE
#       candidate (ImportError at boot).
# G1  — constant-foldable falsy asserts (``assert 1 and 0``, ``assert not 1``, ``assert (0,)[0]``):
#       FALSE candidates.
# G2O — a try-guarded optional import was wrongly required to resolve: OVER-rejection.
# =======================================================================================


# ---- G2A: a recursed dependency that unconditionally aborts ----


@pytest.mark.parametrize(
    "dep_source",
    ["raise RuntimeError('boom')\n", "assert False\n", "import sys\nsys.exit(1)\n"],
)
def test_g2a_dependency_that_aborts_is_needs_review(dep_source: str) -> None:
    files = {
        "requirements.txt": _REQS,
        "helper.py": dep_source,
        "main.py": "import helper\n" + _GOOD_APP,
    }
    assert _detect(files) is _NR


def test_g2a_transitive_dependency_abort_is_needs_review() -> None:
    files = {
        "requirements.txt": _REQS,
        "h2.py": "raise RuntimeError('x')\n",
        "helper.py": "import h2\n",
        "main.py": "import helper\n" + _GOOD_APP,
    }
    assert _detect(files) is _NR


def test_g2a_ancestor_dependency_abort_is_needs_review() -> None:
    # ancestor a/__init__ imports helper; helper aborts -> importing a.mod crashes.
    files = {
        "requirements.txt": _REQS,
        "helper.py": "raise RuntimeError('x')\n",
        "a/__init__.py": "import helper\n",
        "a/mod.py": _GOOD_APP,
    }
    assert _detect(files, start=("uvicorn", "a.mod:app")) is _NR


def test_g2a_benign_dependency_stays_candidate() -> None:
    files = {
        "requirements.txt": _REQS,
        "helper.py": "import os\nX = 1\n",
        "main.py": "import helper\n" + _GOOD_APP,
    }
    assert _detect(files) is _CAND


# ---- G2N: from <local module> import <name> where the name is not bound ----


def test_g2n_unbound_name_is_needs_review() -> None:
    files = {
        "requirements.txt": _REQS,
        "helper.py": "x = 1\n",
        "main.py": "from helper import missing_name\n" + _GOOD_APP,
    }
    assert _detect(files) is _NR


def test_g2n_relative_unbound_name_is_needs_review() -> None:
    files = {
        "requirements.txt": _REQS,
        "pkg/__init__.py": "",
        "pkg/helper.py": "x = 1\n",
        "pkg/main.py": "from .helper import missing_name\n" + _GOOD_APP,
    }
    assert _detect(files, start=("uvicorn", "pkg.main:app")) is _NR


@pytest.mark.parametrize(
    ("files", "start"),
    [
        # a bound name
        (
            {
                "requirements.txt": _REQS,
                "helper.py": "x = 1\n",
                "main.py": "from helper import x\n" + _GOOD_APP,
            },
            ("uvicorn", "main:app"),
        ),
        # a real submodule
        (
            {
                "requirements.txt": _REQS,
                "helper/__init__.py": "",
                "helper/sub.py": "y = 1\n",
                "main.py": "from helper import sub\n" + _GOOD_APP,
            },
            ("uvicorn", "main:app"),
        ),
        # a name re-imported into helper
        (
            {
                "requirements.txt": _REQS,
                "other.py": "thing = 1\n",
                "helper.py": "from other import thing\n",
                "main.py": "from helper import thing\n" + _GOOD_APP,
            },
            ("uvicorn", "main:app"),
        ),
        # a bound name via a relative import
        (
            {
                "requirements.txt": _REQS,
                "pkg/__init__.py": "",
                "pkg/helper.py": "x = 1\n",
                "pkg/main.py": "from .helper import x\n" + _GOOD_APP,
            },
            ("uvicorn", "pkg.main:app"),
        ),
    ],
)
def test_g2n_resolvable_names_stay_candidate(files: dict[str, str], start: tuple[str, ...]) -> None:
    assert _detect(files, start=start) is _CAND


def test_g2n_does_not_over_reject_stdlib_or_installed_names() -> None:
    # The name check applies ONLY to workspace-local modules; stdlib/installed names are never
    # required to exist (we don't have their source).
    files = {
        "requirements.txt": _REQS,
        "main.py": "from os import path\nfrom fastapi import FastAPI\napp = FastAPI()\n",
    }
    assert _detect(files) is _CAND


# ---- G1: constant-foldable falsy asserts ----


@pytest.mark.parametrize(
    "assertion",
    [
        "assert 1 and 0",
        "assert 0 or 0",
        "assert not 1",
        "assert () or []",
        "assert (0,)[0]",
        "assert 0 and undefinedname",  # short-circuits on 0 without evaluating the name
    ],
)
def test_g1_constant_foldable_falsy_assert_is_needs_review(assertion: str) -> None:
    files = {"requirements.txt": _REQS, "main.py": assertion + "\n" + _GOOD_APP}
    assert _detect(files) is _NR


@pytest.mark.parametrize(
    "assertion",
    [
        "assert 1",
        'assert "x"',
        "assert (0,)",  # non-empty tuple (a falsy element) is still truthy
        "assert [0]",
        "assert 1 or 0",  # short-circuits True
        "assert not 0",
        "assert some_name",  # a Name is undecidable -> not flagged
        "assert set()",  # a Call is undecidable -> deferred to runtime
        "assert frozenset()",
    ],
)
def test_g1_truthy_or_undecidable_assert_stays_candidate(assertion: str) -> None:
    prelude = "some_name = 1\n" if "some_name" in assertion else ""
    files = {"requirements.txt": _REQS, "main.py": prelude + assertion + "\n" + _GOOD_APP}
    assert _detect(files) is _CAND


# ---- G2O: try-guarded optional imports ----

_GUARD = "try:\n    import fancy_optional_xyz\nexcept ImportError:\n    fancy_optional_xyz = None\n"


def test_g2o_guarded_optional_import_at_target_is_candidate() -> None:
    files = {"requirements.txt": _REQS, "main.py": _GUARD + _GOOD_APP}
    assert _detect(files) is _CAND


def test_g2o_guarded_optional_import_at_dependency_is_candidate() -> None:
    files = {
        "requirements.txt": _REQS,
        "helper.py": _GUARD,
        "main.py": "import helper\n" + _GOOD_APP,
    }
    assert _detect(files) is _CAND


def test_g2o_unguarded_missing_import_is_needs_review() -> None:
    files = {"requirements.txt": _REQS, "main.py": "import fancy_optional_xyz\n" + _GOOD_APP}
    assert _detect(files) is _NR


def test_g2o_import_error_handler_that_aborts_stays_required() -> None:
    guard_abort = (
        "try:\n    import fancy_optional_xyz\n"
        "except ImportError:\n    import sys\n    sys.exit(1)\n"
    )
    files = {"requirements.txt": _REQS, "main.py": guard_abort + _GOOD_APP}
    assert _detect(files) is _NR


def test_g2o_unrelated_except_does_not_guard_import() -> None:
    # An ``except ValueError`` does not catch ImportError, so the import is still required.
    guard_unrelated = "try:\n    import fancy_optional_xyz\nexcept ValueError:\n    pass\n"
    files = {"requirements.txt": _REQS, "main.py": guard_unrelated + _GOOD_APP}
    assert _detect(files) is _NR


# =======================================================================================
# Batch-6 reopen (four issues a fourth adversary found in the newest code).
# F1  — a namespace package (no ``__init__.py``, so no source) bypassed the name-existence
#       check: ``from pkg import missing`` -> FALSE candidate.
# F2/F3/F-next — the name check over-trusted the exact-once binding visitor, counting a
#       ``del``'d name, a comprehension target, and an ``except ... as e`` name as bound:
#       FALSE candidates.
# O2  — a sibling ``except`` that aborts (``except ValueError: sys.exit(1)``) poisoned the
#       optionality of an import its ImportError handler actually catches: OVER-rejection.
# =======================================================================================


# ---- F1: namespace-package name bypass ----


def test_f1_namespace_package_missing_name_is_needs_review() -> None:
    files = {
        "requirements.txt": _REQS,
        "main.py": "from pkg import missing\n" + _GOOD_APP,
        "pkg/other.py": "v = 1\n",  # pkg has NO __init__.py -> namespace package
    }
    assert _detect(files) is _NR


def test_f1_namespace_package_real_submodule_stays_candidate() -> None:
    files = {
        "requirements.txt": _REQS,
        "main.py": "from pkg import other\n" + _GOOD_APP,
        "pkg/other.py": "v = 1\n",
    }
    assert _detect(files) is _CAND


# ---- F2/F3/F-next: names that do not survive to import completion ----


@pytest.mark.parametrize(
    "helper_source",
    [
        "X = 1\ndel X\n",  # F2: del'd at module scope
        "data = [X for X in range(3)]\n",  # F3: comprehension target does not leak
        "try:\n    1 / 0\nexcept Exception as X:\n    pass\n",  # F-next: except-as auto-deleted
    ],
)
def test_f2_f3_fnext_non_surviving_name_is_needs_review(helper_source: str) -> None:
    files = {
        "requirements.txt": _REQS,
        "helper.py": helper_source,
        "main.py": "from helper import X\n" + _GOOD_APP,
    }
    assert _detect(files) is _NR


@pytest.mark.parametrize(
    "helper_source",
    [
        "X, Y = 1, 2\n",  # tuple unpack
        "[X, Y] = [1, 2]\n",  # list unpack
        "(X, (Y, Z)) = (1, (2, 3))\n",  # nested unpack
        "X, *Y = [1, 2, 3]\n",  # starred unpack
        "X: int = 1\n",  # annotated
        "X = 0\nX += 1\n",  # augmented
        "for X in range(1):\n    pass\n",  # for-target
        "import contextlib\nwith contextlib.nullcontext() as X:\n    pass\n",  # with-as
        "print(X := 1)\n",  # walrus
        "import random\nif random.random() > 0.5:\n    X = 1\nelse:\n    X = 2\n",  # conditional
        "try:\n    X = 1\nexcept Exception:\n    X = 2\n",  # try-bind
        "class X:\n    pass\n",  # class
        "def X():\n    pass\n",  # def
        "import os as X\n",  # import-as
        "from os import path as X\n",  # from-import-as
    ],
)
def test_f2_legitimate_binding_forms_stay_candidate(helper_source: str) -> None:
    files = {
        "requirements.txt": _REQS,
        "helper.py": helper_source,
        "main.py": "from helper import X\n" + _GOOD_APP,
    }
    assert _detect(files) is _CAND


# ---- O2: sibling aborting handler must not poison a caught, non-aborting handler ----


def test_o2_sibling_aborting_handler_does_not_poison_guard() -> None:
    # The ImportError handler (import json as J) does not abort, so `import ujson` is optional;
    # the unrelated `except ValueError: sys.exit(1)` is irrelevant to routing.
    source = (
        "try:\n    import ujson as J\n"
        "except ImportError:\n    import json as J\n"
        "except ValueError:\n    import sys\n\n    sys.exit(1)\n"
    )
    files = {"requirements.txt": _REQS, "main.py": source + _GOOD_APP}
    assert _detect(files) is _CAND


def test_o2_importerror_fallback_stays_candidate() -> None:
    source = "try:\n    import ujson as J\nexcept ImportError:\n    import json as J\n"
    files = {"requirements.txt": _REQS, "main.py": source + _GOOD_APP}
    assert _detect(files) is _CAND


def test_o2_matching_handler_that_aborts_stays_required() -> None:
    # An `except Exception` catches ImportError FIRST and aborts -> the import stays required.
    source = (
        "try:\n    import fancy_optional_xyz\n"
        "except Exception:\n    import sys\n\n    sys.exit(1)\n"
    )
    files = {"requirements.txt": _REQS, "main.py": source + _GOOD_APP}
    assert _detect(files) is _NR


# =======================================================================================
# Batch-7 reopen (one issue a fifth adversary found in the newest code).
# E1  — the import guard lumped ``ModuleNotFoundError`` with ``ImportError`` when deciding
#       whether to WAIVE the from-import name-existence check.  But a missing MODULE raises
#       ``ModuleNotFoundError`` while a missing NAME in a PRESENT module raises a PLAIN
#       ``ImportError`` — and since MNFE is a subclass, ``except ModuleNotFoundError`` does
#       NOT catch a plain ImportError.  So ``try: from helper import missing; except
#       ModuleNotFoundError`` (helper present) boot-crashes yet was minted a FALSE candidate.
#       Optionality is now split BY FAILURE MODE: module-resolution optionality keeps the broad
#       {MNFE, ImportError, Exception, BaseException, bare} set; NAME-existence optionality uses
#       the narrower {ImportError, Exception, BaseException, bare} set (MNFE-only EXCLUDED),
#       routed for a plain ImportError via the same first-matching-handler rule.
# =======================================================================================


def _guard_missing_name(handler: str) -> dict[str, str]:
    """``from helper import thing`` (helper present, ``thing`` ABSENT) under a try/except."""
    return {
        "requirements.txt": _REQS,
        "helper.py": "other = 1\n",
        "main.py": f"try:\n    from helper import thing\n{handler}" + _GOOD_APP,
    }


def test_e1_module_not_found_only_guard_on_missing_name_is_needs_review() -> None:
    # THE defect: MNFE-only cannot catch the plain ImportError a missing NAME raises, so the
    # import boot-crashes and the only sound verdict is needs_review.
    assert _detect(_guard_missing_name("except ModuleNotFoundError:\n    thing = None\n")) is _NR


@pytest.mark.parametrize(
    "handler",
    [
        "except (ValueError, ModuleNotFoundError):\n    thing = None\n",  # MNFE inside a tuple
        "except ModuleNotFoundError:\n    sys.exit(1)\n",  # (also aborts) still required
        # first-matching plain-ImportError handler aborts -> name check runs
        "except ImportError:\n    raise\n",
        "except Exception:\n    raise SystemExit\n",
    ],
)
def test_e1_missing_name_not_waived_when_plain_import_not_swallowed(handler: str) -> None:
    assert _detect(_guard_missing_name(handler)) is _NR


@pytest.mark.parametrize(
    "handler",
    [
        "except ImportError:\n    thing = None\n",  # catches the plain ImportError
        "except (ValueError, ImportError):\n    thing = None\n",  # in a tuple
        "except Exception:\n    thing = None\n",  # broader
        "except BaseException:\n    thing = None\n",  # broadest
        "except:  # noqa: E722\n    thing = None\n",  # bare
        # first-matching routing: MNFE does NOT catch a plain ImportError, so routing falls
        # through to the non-aborting ImportError handler -> name check waived.
        "except ModuleNotFoundError:\n    thing = None\nexcept ImportError:\n    thing = None\n",
        # a first MNFE handler that ABORTS is skipped for a plain ImportError; the next handler
        # catches it non-abortingly -> still waived (routing is per-exception, not per-source).
        "except ModuleNotFoundError:\n    sys.exit(1)\nexcept ImportError:\n    thing = None\n",
    ],
)
def test_e1_missing_name_waived_when_plain_import_swallowed(handler: str) -> None:
    assert _detect(_guard_missing_name(handler)) is _CAND


@pytest.mark.parametrize(
    "statement",
    [
        "import missing_mod_xyz",  # plain import of an absent module
        "from missing_mod_xyz import thing",  # from-import of an absent module
    ],
)
def test_e1_module_not_found_guard_still_makes_missing_module_optional(statement: str) -> None:
    # Module-resolution optionality is UNCHANGED: a genuinely missing module raises MNFE, which
    # ``except ModuleNotFoundError`` catches, so it stays a candidate.
    source = f"try:\n    {statement}\nexcept ModuleNotFoundError:\n    pass\n"
    files = {"requirements.txt": _REQS, "main.py": source + _GOOD_APP}
    assert _detect(files) is _CAND


def test_e1_module_not_found_guard_on_present_name_stays_candidate() -> None:
    # The split must not OVER-reject: when the guarded name genuinely exists, the name check
    # passes and the module is a candidate (guard or no guard).
    files = {
        "requirements.txt": _REQS,
        "helper.py": "thing = 1\n",
        "main.py": "try:\n    from helper import thing\nexcept ModuleNotFoundError:\n"
        "    thing = None\n" + _GOOD_APP,
    }
    assert _detect(files) is _CAND


def test_e1_relative_module_not_found_guard_on_missing_name_is_needs_review() -> None:
    # Same failure mode through a relative import in a package.
    files = {
        "requirements.txt": _REQS,
        "pkg/__init__.py": "",
        "pkg/helper.py": "other = 1\n",
        "pkg/main.py": "try:\n    from .helper import thing\nexcept ModuleNotFoundError:\n"
        "    thing = None\n" + _GOOD_APP,
    }
    assert _detect(files, start=("uvicorn", "pkg.main:app")) is _NR


def test_e1_transitive_module_not_found_guard_on_missing_name_is_needs_review() -> None:
    # The defect one hop behind the entrypoint: main imports helper (present), helper guards a
    # ``from sibling import thing`` (sibling present, thing absent) under MNFE-only -> boot-crash.
    files = {
        "requirements.txt": _REQS,
        "sibling.py": "other = 1\n",
        "helper.py": "try:\n    from sibling import thing\nexcept ModuleNotFoundError:\n"
        "    thing = None\n",
        "main.py": "import helper\n" + _GOOD_APP,
    }
    assert _detect(files) is _NR


# =======================================================================================
# Batch-7 reopen (one issue a sixth adversary found in the newest code).
# E2  — the exception-hierarchy split (E1) fixed the from-import NAME check, but the
#       ``module_optional`` short-circuit that SKIPPED the transitive chain proof
#       (``_local_chain_is_proven``) was NOT split: a guarded import of a PRESENT module
#       skipped proving that module's own imports regardless of whether the guard could
#       actually swallow the transitive failure, minting three false candidates —
#         FC-a: ``except ModuleNotFoundError`` over a dep whose from-import NAME is missing
#               (plain ImportError escapes the MNFE guard),
#         FC-b: ``except ImportError`` over a dep that ``raise``s RuntimeError (escapes),
#         FC-c: ``except Exception`` over a dep that ``sys.exit()``s (SystemExit escapes).
#       The unifying fix is a GUARD-TIER model: each import carries the SET of exception
#       tiers its enclosing ``try`` bodies swallow (MODULE<NAME<EXCEPTION<BASE), threaded
#       as the ambient tier set through the transitive-chain recursion; a transitive failure
#       is optional iff its min-swallow-tier is in that set.  The set (not a scalar) is
#       required because guards are NON-monotonic — ``except ModuleNotFoundError: sys.exit()``
#       then ``except ImportError: x=None`` swallows a missing NAME (tier 2) yet aborts a
#       missing MODULE (tier 1), proven against the live runtime.
# =======================================================================================

# raw-ASGI target + uvicorn-only, matching the reported repro; ``optional_helper`` is a PRESENT
# workspace module imported under a ``try`` guard, so the transitive chain IS reached and proven.
_E2_REQS = "uvicorn==0.30\n"


def _e2(guard_handler: str, helper_body: str, **extra: str) -> dict[str, str]:
    """main.py guards ``import optional_helper`` with ``guard_handler``; the helper body is what
    the (present) dependency does when imported."""
    guarded = f"try:\n    import optional_helper\n{guard_handler}    optional_helper = None\n"
    main = guarded + _RAW_ASGI
    files = {"requirements.txt": _E2_REQS, "main.py": main, "optional_helper.py": helper_body}
    files.update(extra)
    return files


@pytest.mark.parametrize(
    ("guard_handler", "helper_body", "extra"),
    [
        # FC-a: MNFE guard cannot catch the plain ImportError a missing transitive NAME raises.
        ("except ModuleNotFoundError:\n", "from db import get_pool\n", {"db.py": "URL = 'x'\n"}),
        # FC-b: ImportError guard cannot catch a transitive RuntimeError.
        ("except ImportError:\n", "raise RuntimeError('boom')\n", {}),
        # FC-c: Exception guard cannot catch the SystemExit a transitive sys.exit() raises.
        ("except Exception:\n", "import sys\nsys.exit(1)\n", {}),
    ],
)
def test_e2_guard_too_narrow_for_transitive_failure_is_needs_review(
    guard_handler: str, helper_body: str, extra: dict[str, str]
) -> None:
    assert _detect(_e2(guard_handler, helper_body, **extra)) is _NR


@pytest.mark.parametrize(
    ("guard_handler", "helper_body", "extra"),
    [
        # MNFE guard DOES swallow a genuinely missing transitive MODULE.
        ("except ModuleNotFoundError:\n", "import totally_absent_mod_xyz\n", {}),
        # Exception guard swallows a transitive RuntimeError (an Exception subclass).
        ("except Exception:\n", "raise RuntimeError('x')\n", {}),
        # Exception guard swallows a transitive AssertionError (statically-false assert).
        ("except Exception:\n", "assert False\n", {}),
        # BaseException / bare guards swallow even a transitive SystemExit (sys.exit / raise).
        ("except BaseException:\n", "import sys\nsys.exit(1)\n", {}),
        ("except:  # noqa: E722\n", "raise SystemExit\n", {}),
        # A clean optional local module stays a candidate.
        ("except ModuleNotFoundError:\n", "import os\n", {}),
        # No over-rejection: the guarded dep's imported NAME genuinely exists.
        ("except ModuleNotFoundError:\n", "from db import URL\n", {"db.py": "URL = 'x'\n"}),
    ],
)
def test_e2_guard_broad_enough_for_transitive_failure_is_candidate(
    guard_handler: str, helper_body: str, extra: dict[str, str]
) -> None:
    assert _detect(_e2(guard_handler, helper_body, **extra)) is _CAND


def test_e2_non_monotonic_guard_swallows_name_but_not_module() -> None:
    # ``except ModuleNotFoundError: raise SystemExit`` then ``except ImportError: x=None`` is
    # NON-monotonic: it swallows a missing NAME (plain ImportError -> second handler) but ABORTS a
    # missing MODULE (ModuleNotFoundError -> first handler). Both verdicts proven vs live runtime.
    handler = "except ModuleNotFoundError:\n    raise SystemExit\nexcept ImportError:\n"
    name_missing = _e2(handler, "from db import get_pool\n", **{"db.py": "URL = 'x'\n"})
    module_missing = _e2(handler, "import totally_absent_mod_xyz\n")
    assert _detect(name_missing) is _CAND  # NAME (tier 2) is swallowed by the ImportError handler
    assert _detect(module_missing) is _NR  # MODULE (tier 1) hits the aborting MNFE handler


@pytest.mark.parametrize(
    ("guard_handler", "expected"),
    [
        # A tuple that includes ImportError is tier NAME -> swallows a transitive missing name.
        ("except (ValueError, ImportError):\n", _CAND),
        # A tuple of only ModuleNotFoundError is tier MODULE -> does NOT swallow a missing name.
        ("except (ValueError, ModuleNotFoundError):\n", _NR),
        # First-matching handler ABORTS (re-raise) -> tier NONE, nothing is optional.
        ("except ImportError:\n    raise\nexcept Exception:\n", _NR),
    ],
)
def test_e2_tier_boundaries_over_transitive_missing_name(
    guard_handler: str, expected: ReleaseAssessment
) -> None:
    # ``optional_helper`` (present) does ``from db import get_pool`` (get_pool absent -> plain
    # ImportError at the tier-NAME boundary).
    files = _e2(guard_handler, "from db import get_pool\n", **{"db.py": "URL = 'x'\n"})
    assert _detect(files) is expected


def test_e2_nested_inner_guard_swallows_transitive_name() -> None:
    # The DEP itself guards its missing-name import: candidate regardless of the outer guard.
    files = {
        "requirements.txt": _E2_REQS,
        "main.py": "try:\n    import optional_helper\nexcept ModuleNotFoundError:\n"
        "    optional_helper = None\n" + _RAW_ASGI,
        "optional_helper.py": "try:\n    from db import get_pool\nexcept ImportError:\n"
        "    get_pool = None\n",
        "db.py": "URL = 'x'\n",
    }
    assert _detect(files) is _CAND


def test_e2_nested_outer_module_guard_does_not_reach_unguarded_transitive_name() -> None:
    # The dep's missing-name import is UNGUARDED in the dep; the plain ImportError propagates to
    # main's ``except ModuleNotFoundError`` which does NOT catch it -> boot-crash.
    files = {
        "requirements.txt": _E2_REQS,
        "main.py": "try:\n    import optional_helper\nexcept ModuleNotFoundError:\n"
        "    optional_helper = None\n" + _RAW_ASGI,
        "optional_helper.py": "from db import get_pool\n",
        "db.py": "URL = 'x'\n",
    }
    assert _detect(files) is _NR


def test_e2_unguarded_transitive_abort_still_fails_closed() -> None:
    # Regression guard for the ORIGINAL G2A behavior: an UNGUARDED importer (ambient set empty) must
    # still fail closed on ANY transitive module-scope abort, at every abort tier.
    for helper_body in ("raise RuntimeError('x')\n", "assert False\n", "import sys\nsys.exit(1)\n"):
        files = {
            "requirements.txt": _E2_REQS,
            "main.py": "import optional_helper\n" + _RAW_ASGI,
            "optional_helper.py": helper_body,
        }
        assert _detect(files) is _NR


# =======================================================================================
# Batch-8 reopen (three abort-detector clusters a seventh adversary found).
# C1 — ``os._exit`` / ``os.abort`` / ``os.kill(getpid, …)`` terminate the process with NO
#      catchable exception, but were tiered as catchable BASE (swallowed by ``except
#      BaseException`` / bare).  Fix: an UNCATCHABLE tier strictly above BASE, in NO guard's
#      swallow-set, so such an abort fails closed under every guard.
# C2 — a builtin exception NAME rebound at module scope (``ValueError = SystemExit; raise
#      ValueError``) defeats the name->tier map (runtime raises SystemExit, escapes ``except
#      Exception``).  Fix: a ``raise <Name>`` whose Name is assigned anywhere at module scope
#      is UNCATCHABLE (its real type is statically unknowable); an un-rebound builtin keeps
#      its correct tier.
# C3 — the abort detector was top-level-only; aborts nested in STATICALLY-CERTAIN control flow
#      were missed.  Fix: recurse into ``if``/``while`` whose test folds via the pure-literal
#      ``_static_truthiness`` (now also folding ``Compare`` of constants), ``for`` over a
#      statically non-empty literal, ``with`` (body always runs), and ``try`` (body routed
#      through its own handlers + ``finally`` always runs).  BOUNDARY: ``_static_truthiness``
#      NEVER evaluates a Call/Name/Attribute, so ``if len([1]): raise`` / ``if flag: raise`` /
#      ``if os.environ.get('X'): raise`` stay candidate — deferred to the boot probe, exactly
#      like ``1 / 0``.
# =======================================================================================

_E3_REQS = "uvicorn==0.30\n"


def _e3_top(main_body: str) -> dict[str, str]:
    """A top-level (unguarded) module body followed by a raw-ASGI app."""
    return {"requirements.txt": _E3_REQS, "main.py": main_body + _RAW_ASGI}


def _e3_guarded(handler: str, helper_body: str) -> dict[str, str]:
    """main.py guards ``import helper`` with ``handler``; helper aborts via ``helper_body``."""
    return {
        "requirements.txt": _E3_REQS,
        "main.py": f"try:\n    import helper\n{handler}    pass\n" + _RAW_ASGI,
        "helper.py": helper_body,
    }


# ---- C1: os._exit / os.abort / os.kill are UNCATCHABLE ----


@pytest.mark.parametrize(
    "helper_body",
    [
        "import os\nos._exit(0)\n",
        "import os\nos.abort()\n",
        "import os\nimport signal\nos.kill(os.getpid(), signal.SIGKILL)\n",
    ],
)
@pytest.mark.parametrize("handler", ["except BaseException:\n", "except:  # noqa: E722\n"])
def test_c1_uncatchable_terminator_under_broadest_guard_is_needs_review(
    handler: str, helper_body: str
) -> None:
    # Even a BaseException / bare guard cannot rescue an uncatchable process terminator.
    assert _detect(_e3_guarded(handler, helper_body)) is _NR


def test_c1_uncatchable_terminator_top_level_is_needs_review() -> None:
    assert _detect(_e3_top("import os\nos._exit(0)\n")) is _NR


def test_c1_sys_exit_stays_catchable_base() -> None:
    # sys.exit / exit / quit raise a CATCHABLE SystemExit -> BASE -> a bare guard swallows it.
    assert _detect(_e3_guarded("except BaseException:\n", "import sys\nsys.exit(1)\n")) is _CAND
    assert _detect(_e3_guarded("except:  # noqa: E722\n", "exit(1)\n")) is _CAND


# ---- C2: rebound builtin exception name ----


def test_c2_rebound_exception_name_is_needs_review() -> None:
    # ``ValueError = SystemExit; raise ValueError`` really raises SystemExit -> escapes except
    # Exception; the rebound name's real type is statically unknowable -> UNCATCHABLE, fail closed.
    files = _e3_guarded("except Exception:\n", "ValueError = SystemExit\nraise ValueError\n")
    assert _detect(files) is _NR


def test_c2_unrebound_builtin_exception_name_keeps_its_tier() -> None:
    # An un-rebound builtin keeps its correct tier: ValueError is an Exception -> swallowed by
    # except Exception -> candidate.
    assert _detect(_e3_guarded("except Exception:\n", "raise ValueError\n")) is _CAND


# ---- C3: aborts nested in statically-certain control flow ----


@pytest.mark.parametrize(
    "main_body",
    [
        "if True:\n    raise RuntimeError()\n",
        "while True:\n    raise RuntimeError()\n",
        "if 1 == 1:\n    raise RuntimeError()\n",
        "if 1 < 2:\n    raise RuntimeError()\n",
        "if 'a' == 'a':\n    raise RuntimeError()\n",
        "for _ in [1]:\n    raise RuntimeError()\n",
        "for _ in 'ab':\n    raise RuntimeError()\n",
        "f = open(__file__)\nwith f:\n    raise RuntimeError()\n",
        "if True:\n    if True:\n        raise RuntimeError()\n",  # deeper nesting
        "if False:\n    x = 1\nelse:\n    raise RuntimeError()\n",  # else branch certain
        "try:\n    raise RuntimeError()\nfinally:\n    pass\n",  # body propagates
        "x = 1\ntry:\n    x = 2\nfinally:\n    raise RuntimeError()\n",  # finally always runs
        # try-body abort caught, but the handler itself terminates uncatchably:
        "import os\ntry:\n    raise RuntimeError()\nexcept Exception:\n    os._exit(0)\n",
    ],
)
def test_c3_statically_certain_nested_abort_is_needs_review(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _NR


def test_c3_handler_nested_abort_forces_name_check() -> None:
    # The ImportError handler re-raises via ``if True: raise`` -> it aborts -> the guarded
    # from-import is NOT optional -> the missing name is required -> needs_review.
    files = {
        "requirements.txt": _E3_REQS,
        "helper.py": "other = 1\n",
        "main.py": "try:\n    from helper import missing\nexcept ImportError:\n"
        "    if True:\n        raise\n" + _RAW_ASGI,
    }
    assert _detect(files) is _NR


@pytest.mark.parametrize(
    "main_body",
    [
        "if False:\n    raise RuntimeError()\n",  # dead branch
        "if 1 == 2:\n    raise RuntimeError()\n",  # false compare
        "while True:\n    break\n",  # loop breaks, body never aborts
        "for _ in []:\n    raise RuntimeError()\n",  # empty iterable, body never runs
        "if True:\n    x = 1\n",  # certain branch, but no abort
        "import os\nif os.environ.get('X'):\n    raise RuntimeError()\n",  # runtime-conditional
        "if len([1]):\n    raise RuntimeError()\n",  # BOUNDARY: call in test -> deferred
        "flag = True\nif flag:\n    raise RuntimeError()\n",  # BOUNDARY: name in test -> deferred
    ],
)
def test_c3_undecidable_or_dead_control_flow_stays_candidate(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _CAND


def test_c3_module_try_that_swallows_its_own_abort_stays_candidate() -> None:
    # A top-level try whose handler catches the body's abort does NOT abort the module.
    files = _e3_top("try:\n    raise RuntimeError()\nexcept Exception:\n    pass\n")
    assert _detect(files) is _CAND


# =======================================================================================
# Batch-7 reopen (the last two compound-statement forms the module-abort detector never
# covered — a fresh adversary minted static false candidates from both).
# H1 — ``ast.Match`` was unmodeled: a ``match`` over a pure-literal subject whose taken case
#      aborts (``match 1: case 1: raise``) fell through to _TIER_NONE -> FALSE candidate.
# H2 — the ``else``/``orelse`` of ``try``/``for``/``while`` was never inspected: a ``for``/
#      ``while``/``try`` whose body cannot skip the ``else`` yet the ``else`` aborts crashed
#      on import but returned candidate -> FALSE candidate.
# Every row drives the real ``detect_release`` (raw-ASGI target, no third-party dep). The
# abort statement precedes the app so the module crashes at import before ``app`` is defined.
# =======================================================================================


@pytest.mark.parametrize(
    "main_body",
    [
        # H1 — Match over a literal subject whose provably-taken case aborts.
        "match 1:\n    case 1:\n        raise RuntimeError()\n",  # value pattern folds
        "match 0:\n    case _:\n        raise RuntimeError()\n",  # wildcard always matches
        "import os\nmatch 0:\n    case _:\n        os._exit(0)\n",  # UNCATCHABLE tier
        "match 2:\n    case 1:\n        pass\n    case 2:\n        raise RuntimeError()\n",
        "match 3:\n    case 1 | 3:\n        raise RuntimeError()\n",  # OR-pattern
        "match None:\n    case None:\n        raise RuntimeError()\n",  # singleton
        # H2 — the else/orelse of try/for/while runs and aborts.
        "try:\n    pass\nexcept Exception:\n    pass\nelse:\n    raise RuntimeError()\n",
        "for _ in [1]:\n    pass\nelse:\n    raise RuntimeError()\n",  # body completes
        "for _ in []:\n    pass\nelse:\n    raise RuntimeError()\n",  # body skipped
        "while False:\n    pass\nelse:\n    raise RuntimeError()\n",  # else always runs
    ],
)
def test_b7_uncovered_abort_forms_are_needs_review(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _NR


@pytest.mark.parametrize(
    "main_body",
    [
        # BOUNDARY: a match subject that is a Name is not foldable -> deferred.
        "x = 0\nmatch x:\n    case 1:\n        raise RuntimeError()\n",
        # BOUNDARY: a for over a Name iterable is not a static literal -> deferred.
        "items = []\nfor _ in items:\n    pass\nelse:\n    raise RuntimeError()\n",
        # BOUNDARY: a call body may raise, so the try's else is not certain.
        "import os\ntry:\n    os.getcwd()\n"
        "except Exception:\n    pass\nelse:\n    raise RuntimeError()\n",
        # A match subject folds and the pattern matches, but a guard may fail.
        "x = False\nmatch 1:\n    case 1 if x:\n        raise RuntimeError()\n",
        # A match subject folds but the pattern is structural (class) -> deferred.
        "match 1:\n    case int():\n        raise RuntimeError()\n",
    ],
)
def test_b7_dynamic_boundary_controls_stay_candidate(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _CAND


@pytest.mark.parametrize(
    "main_body",
    [
        # A match on a non-literal subject never folds, even with an aborting case.
        "y = object()\nmatch y:\n    case int():\n        raise RuntimeError()\n",
        # A realistic match on a Name with real cases (no abort) stays candidate.
        "mode = 'a'\nmatch mode:\n    case 'a':\n        v = 1\n    case _:\n        v = 2\n",
        # A for over a nonempty literal with Name elements and NO else stays candidate.
        "a = 1\nb = 2\nfor r in [a, b]:\n    print(r)\n",
        # A for whose else does NOT abort stays candidate.
        "t = 0\nfor i in [1, 2]:\n    t += i\nelse:\n    print(t)\n",
        # A try whose body is an import (not provably non-raising) does not recurse into its else.
        "try:\n    import os\nexcept ImportError:\n    import sys as os\nelse:\n    z = os\n",
    ],
)
def test_b7_runnable_shapes_stay_candidate(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _CAND


# =======================================================================================
# Batch-8 reopen — the corr#9 reachability walkers must fold pure-literal reachability the
# SAME way the abort detector / _static_truthiness already do, or a statically-DEAD break /
# guard / branch defeats the try/for/while-else analysis (proof=candidate, import CRASHES).
# I1 — _stmt_contains_break counted dead breaks (dead `if False:` branch, non-matching
#      `case`, and — a 4th instance I found — a handler for a body that cannot raise).
# I2 — _static_match_index bailed on ANY guard; a literal-False guard skips the case and a
#      literal-True guard is taken.
# I3 — _statement_cannot_raise returned False for compound stmts; a dead `if False: raise` /
#      no-op `match` in a try body defeated the try-else analysis.
# All rows drive the real detect_release (raw-ASGI target); the offending code precedes app.
# =======================================================================================


@pytest.mark.parametrize(
    "main_body",
    [
        # I1 — a statically-dead break must NOT block the for-else abort.
        "for x in [1, 2]:\n    if False:\n        break\nelse:\n    raise RuntimeError()\n",
        "for x in [1, 2]:\n    match 9:\n        case 1:\n            break\n"
        "else:\n    raise RuntimeError()\n",
        "import os\nfor x in [1, 2]:\n    if False:\n        break\nelse:\n    os._exit(7)\n",
        # I1 (4th instance) — a handler is dead when the try body cannot raise, so its break is too.
        "for x in [1, 2]:\n    try:\n        pass\n    except Exception:\n        break\n"
        "else:\n    raise RuntimeError()\n",
        # I1 — a non-matching earlier case's break is dead; the taken case has none.
        "for x in [1, 2]:\n    match 5:\n        case 1:\n            break\n        case _:\n"
        "            pass\nelse:\n    raise RuntimeError()\n",
        # I1 — a fully-resolved match whose taken body has no break lets the for-else fire.
        "for i in [1, 2]:\n    match 7:\n        case 7:\n            pass\n"
        "else:\n    raise RuntimeError()\n",
        # I2 — a literal-False guard SKIPS its case; a later case aborts.
        "match 1:\n    case 1 if False:\n        pass\n    case _:\n        raise RuntimeError()\n",
        # I2 — a literal-True guard is TAKEN.
        "match 1:\n    case _ if True:\n        raise RuntimeError()\n",
        # I3 — a dead `if False: raise` in a try body cannot raise, so the try-else fires.
        "try:\n    if False:\n        raise ValueError()\nexcept Exception:\n    pass\n"
        "else:\n    raise RuntimeError()\n",
        # I3 — an uncatchable else behind a dead-branch try body.
        "import os\ntry:\n    if False:\n        raise ValueError()\nexcept Exception:\n    pass\n"
        "else:\n    os._exit(4)\n",
        # I3 (match-in-try-body) — a match that provably takes no case cannot raise.
        "try:\n    match 9:\n        case 1:\n            raise ValueError()\nexcept Exception:\n"
        "    pass\nelse:\n    raise RuntimeError()\n",
    ],
)
def test_b8_dead_reachability_no_longer_hides_abort(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _NR


@pytest.mark.parametrize(
    "main_body",
    [
        # A DYNAMIC break may fire -> the for-else is not certain -> candidate.
        "flag = True\nfor x in [1, 2]:\n    if flag:\n        break\n"
        "else:\n    raise RuntimeError()\n",
        # A DYNAMIC guard is undecidable -> the match's taken case is not proven -> candidate.
        "cond = False\nmatch 1:\n    case 1 if cond:\n        raise RuntimeError()\n",
        # An UNCONDITIONAL real break exits the loop, skipping the else -> candidate.
        "for x in [1, 2]:\n    break\nelse:\n    raise RuntimeError()\n",
        # A LIVE break in a LIVE (if True) branch must not be folded away -> candidate.
        "for x in [1, 2]:\n    if True:\n        break\nelse:\n    raise RuntimeError()\n",
        # A call body MAY raise, so the handler is reachable and its break may fire -> candidate.
        "import os\nfor x in [1, 2]:\n    try:\n        os.getcwd()\n    except Exception:\n"
        "        break\nelse:\n    raise RuntimeError()\n",
    ],
)
def test_b8_live_reachability_stays_candidate(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _CAND


# =======================================================================================
# Batch-9 reopen — the cannot-raise / non-raising-guard predicate must fold the SAME
# provably-non-raising literal operators the abort detector's _static_truthiness already
# folds (Compare / BoolOp / `not`).  Before this, `_is_cannot_raise_literal` accepted only
# constants + displays, so `match _ if 1==1: raise`, `try: if 1==1: ... else: raise`, and
# `try: x = 1==1 ... else: raise` were static false candidates (import CRASHES).  The fix is
# a single recursive predicate (rejecting `[1/0]` and left-to-right raises like
# `(1<'a') and False`), consistent with _static_truthiness's non-raising subset.
# =======================================================================================


@pytest.mark.parametrize(
    "main_body",
    [
        # A — a match GUARD that is a non-raising literal operator now folds.
        "match 1:\n    case _ if 1 == 1:\n        raise RuntimeError()\n",
        "match 1:\n    case _ if not False:\n        raise RuntimeError()\n",
        "match 1:\n    case _ if True and True:\n        raise RuntimeError()\n",
        # A literal-False guard SKIPS its case; a later case aborts.
        "match 1:\n    case 1 if 0 == 1:\n        pass\n"
        "    case _:\n        raise RuntimeError()\n",
        # A non-empty tuple guard (already handled) stays flagged.
        "match 1:\n    case _ if (1,):\n        raise RuntimeError()\n",
        # More literal-operator guard forms locking the invariant.
        "match 1:\n    case _ if 2 > 1:\n        raise RuntimeError()\n",
        "match 1:\n    case _ if False or True:\n        raise RuntimeError()\n",
        "match 1:\n    case _ if not (1 == 2):\n        raise RuntimeError()\n",
        "match 1:\n    case _ if 1 < 2 < 3:\n        raise RuntimeError()\n",
        # B — an if-TEST in a try body that is a non-raising literal operator now folds.
        "try:\n    if 1 == 1:\n        x = 1\nexcept Exception:\n    pass\n"
        "else:\n    raise RuntimeError()\n",
        # C — an assignment VALUE that is a non-raising literal operator now folds.
        "try:\n    x = 1 == 1\nexcept Exception:\n    pass\nelse:\n    raise RuntimeError()\n",
        "try:\n    x = not False\nexcept Exception:\n    pass\nelse:\n    raise RuntimeError()\n",
        "try:\n    x = True and True\nexcept Exception:\n    pass\n"
        "else:\n    raise RuntimeError()\n",
    ],
)
def test_b9_nonraising_literal_operators_are_needs_review(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _NR


@pytest.mark.parametrize(
    "main_body",
    [
        # A Name in the guard is dynamic -> the taken case is unproven -> candidate.
        "x = 0\nmatch 1:\n    case _ if x == 1:\n        raise RuntimeError()\n",
        # A short-circuit over a Name may raise (NameError on x) -> not foldable -> candidate.
        "x = 1\nmatch 1:\n    case _ if x and []:\n        raise RuntimeError()\n",
        # A TypeError-raising compare is DEFERRED (consistent with module-scope `if 1<'a': raise`).
        "match 1:\n    case _ if 1 < 'a':\n        raise RuntimeError()\n",
        # A BinOp value stays "may raise" (1/0 is caught -> else skipped -> module runs).
        "try:\n    x = 1 / 0\nexcept Exception:\n    pass\nelse:\n    raise RuntimeError()\n",
        # A TypeError compare value stays "may raise" (caught -> else skipped -> module runs).
        "try:\n    x = 1 < 'a'\nexcept Exception:\n    pass\nelse:\n    raise RuntimeError()\n",
        # An unhashable set build stays "may raise" (TypeError caught -> else skipped -> runs).
        "try:\n    x = {[1]}\nexcept Exception:\n    pass\nelse:\n    raise RuntimeError()\n",
    ],
)
def test_b9_may_raise_operators_stay_candidate(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _CAND


# =======================================================================================
# Batch-10 reopen — the LAST fold-parity gap: `_is_cannot_raise_literal` gained
# not/Compare/BoolOp arms in Batch-9 but had NO `ast.Subscript` arm, while _static_truthiness
# folds literal subscripts.  So the if/while abort arm caught `if (1,2)[0]: raise` but the
# match-guard / try-else / for-else siblings deferred it -> false candidates (import CRASHES).
# The fix mirrors `_literal_subscript_truthiness` AND additionally requires the container build
# to be safe (so `(1/0,)[0]` stays deferred).  The parity-lock test below prevents future drift.
# =======================================================================================


@pytest.mark.parametrize(
    "main_body",
    [
        "match 1:\n    case _ if (1, 2)[0]:\n        raise RuntimeError()\n",
        "import os\nmatch 1:\n    case _ if [10][0]:\n        os._exit(9)\n",
        "try:\n    y = (1, 2)[0]\nexcept Exception:\n    pass\nelse:\n    raise RuntimeError()\n",
        "for x in [1, 2]:\n    try:\n        y = (1, 2)[0]\n    except Exception:\n"
        "        break\nelse:\n    raise RuntimeError()\n",
    ],
)
def test_b10_literal_subscript_no_longer_hides_abort(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _NR


@pytest.mark.parametrize(
    "main_body",
    [
        # A raising container element -> the whole build may raise -> deferred (parity with if-arm).
        "match 1:\n    case _ if (1 / 0,)[0]:\n        raise RuntimeError()\n",
        # A negative index parses as UnaryOp (not Constant) -> deferred.
        "match 1:\n    case _ if (9,)[-1]:\n        raise RuntimeError()\n",
        # An out-of-range index -> deferred.
        "match 1:\n    case _ if (1,)[5]:\n        raise RuntimeError()\n",
        # An out-of-range subscript VALUE stays "may raise" (IndexError caught -> app RUNS).
        "try:\n    y = (1,)[5]\nexcept Exception:\n    pass\nelse:\n    raise RuntimeError()\n",
    ],
)
def test_b10_unsafe_subscript_stays_candidate(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _CAND


# ---- Parity lock: `_is_cannot_raise_literal` must mirror EVERY foldable `_static_truthiness`
# arm, so the three siblings agree.  The if-arm/match-guard gate on TRUTHINESS while the
# try-else gates only on non-raising, so the "all agree" battery uses TRUTHY forms (a falsy
# foldable like `[0][0]` correctly diverges: if/match candidate, try-else needs_review).


def _fold_siblings(form: str) -> tuple[str, str, str]:
    """The module ``if``, ``match`` guard, and ``try``-``else`` siblings that fold ``form``."""
    return (
        f"if {form}:\n    raise RuntimeError()\n",
        f"match 1:\n    case _ if {form}:\n        raise RuntimeError()\n",
        f"try:\n    y = {form}\nexcept Exception:\n    pass\nelse:\n    raise RuntimeError()\n",
    )


@pytest.mark.parametrize(
    "form",
    [
        "(1, 2)[0]",  # literal subscript (the Batch-10 arm)
        "[1][0]",  # list subscript
        "1 == 1",  # Compare
        "2 > 1",  # ordering Compare
        "not 0",  # UnaryOp Not
        "True and True",  # BoolOp And
        "True or undefined",  # BoolOp Or short-circuiting past a dead Name
        "True or (1 / 0)",  # BoolOp Or short-circuiting past a dead raising branch
        "(1,)",  # non-empty tuple display
        "{1}",  # non-empty set display
        "{1: 2}",  # non-empty dict display
        "not (1 and 0)",  # nested not/BoolOp
    ],
)
def test_b10_fold_parity_truthy_forms_all_three_needs_review(form: str) -> None:
    verdicts = {_detect(_e3_top(body)) for body in _fold_siblings(form)}
    assert verdicts == {_NR}, (form, verdicts)


@pytest.mark.parametrize(
    "form",
    [
        "(1 / 0,)[0]",  # subscript into a container that raises on build
        "(9,)[-1]",  # non-constant (UnaryOp) index
        "(1,)[5]",  # out-of-range index
        "1 + 1",  # BinOp — not a `_static_truthiness` arm
        "1 in (1, 2)",  # Compare with `in` — deferred by `_static_truthiness`
        "1 is 1",  # Compare with `is` — deferred by `_static_truthiness`
    ],
)
def test_b10_fold_parity_deferred_forms_all_three_candidate(form: str) -> None:
    verdicts = {_detect(_e3_top(body)) for body in _fold_siblings(form)}
    assert verdicts == {_CAND}, (form, verdicts)


# =======================================================================================
# Batch-11 reopen — two more static-false-candidate classes an eighth adversary found.
# Finding #1 — set/dict fold-parity was NOT closed.  `_static_truthiness` folded a set/dict by
#   emptiness while `_is_cannot_raise_literal` required `ast.Constant` elements/keys, so a set or
#   dict holding a HASHABLE non-Constant literal (a tuple like ``(1, 2)``) was CAUGHT by the if-arm
#   yet DEFERRED by the match-guard / try-else -> a static false candidate (``try: x = {(1, 2)}
#   ... else: os._exit()`` aborts on import but returned candidate).  Symmetrically the if-arm
#   OVER-rejected an unhashable / raising build (``if {[1]}: ...`` folded truthy though the set
#   build TypeErrors).  A shared `_is_hashable_cannot_raise_literal` now makes both folders gate on
#   identical "builds without raising, statically sized" logic, so the if-arm and the guard AGREE.
# Finding #2 — the terminating-call detector matched fixed name sets WITHOUT resolving module-scope
#   import aliases, so ``import os as _o; _o._exit()``, ``from sys import exit as _e; _e()``, and
#   ``e = os._exit; e()`` (plus an aliased ``os.kill(os.getpid())``) slipped through as candidates
#   though they terminate the process.  The callee is now canonicalized through the SAME exact-once
#   alias map the app-constructor detector uses before matching; a genuinely dynamic callee
#   (``getattr``, a multiply-bound / conditionally-assigned name) stays deferred, not over-rejected.
# =======================================================================================


# ---- Finding #1: set/dict displays holding hashable, non-Constant literals ----


@pytest.mark.parametrize(
    "display",
    [
        "{()}",  # a hashable EMPTY-tuple element (non-Constant) — the core hole
        "{(1, 2)}",  # a hashable tuple element
        "{(): 0}",  # a hashable tuple KEY
        "{1}",  # constant element (already caught; locked for completeness)
        "{1, 2}",
        "{1: 2}",
    ],
)
def test_b11_foldable_set_dict_display_aborts_agree(display: str) -> None:
    # The if-arm, the match-guard, and the try-else fold the same BUILDABLE display -> all NR.
    verdicts = {_detect(_e3_top(body)) for body in _fold_siblings(display)}
    assert verdicts == {_NR}, (display, verdicts)


@pytest.mark.parametrize(
    "display",
    [
        "{[1]}",  # unhashable element -> build TypeErrors -> deferred (was a false if-arm NR)
        "{1 / 0}",  # a raising element -> build raises -> deferred
        "{1: 1 / 0}",  # a raising dict VALUE -> build raises -> deferred
        "{(1, [2])}",  # a tuple holding an unhashable element -> unhashable -> deferred
    ],
)
def test_b11_unbuildable_set_dict_display_defers_agree(display: str) -> None:
    # All three siblings defer the same non-buildable display -> all candidate (parity preserved).
    verdicts = {_detect(_e3_top(body)) for body in _fold_siblings(display)}
    assert verdicts == {_CAND}, (display, verdicts)


@pytest.mark.parametrize("display", ["{(1, 2)}", "{()}", "{(): 0}"])
def test_b11_hashable_literal_guard_terminator_if_and_tryelse_both_needs_review(
    display: str,
) -> None:
    # The concrete reported false candidate: the try/else builds the buildable display (which
    # cannot raise) then ``os._exit`` fires; the bare if-arm folds the same display.  BOTH NR now.
    if_arm = _e3_top(f"import os\nif {display}:\n    os._exit(3)\n")
    guard = _e3_top(
        f"import os\ntry:\n    _v = {display}\nexcept Exception:\n    pass\n"
        f"else:\n    os._exit(3)\n"
    )
    assert _detect(if_arm) is _NR
    assert _detect(guard) is _NR


# ---- Finding #2: process terminators hidden behind module-scope import aliases ----


@pytest.mark.parametrize(
    "main_body",
    [
        "import os as _o\n_o._exit(4)\n",  # import X as Y ; Y.term()
        "import sys as _s\n_s.exit(1)\n",  # aliased module attribute
        "from sys import exit as _e\n_e(1)\n",  # from X import term as Y ; Y()
        "from os import _exit\n_exit(4)\n",  # from X import term ; term()
        "import os\ne = os._exit\ne()\n",  # Y = <dotted import expr> ; Y()
        "import os as _o\n_o.kill(_o.getpid(), 9)\n",  # aliased os.kill(os.getpid(), ...)
        "import os\nos._exit(2)\n",  # control: the unaliased spelling still fires
    ],
)
def test_b11_aliased_terminating_call_is_needs_review(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _NR


@pytest.mark.parametrize(
    "main_body",
    [
        "import os as _o\n_o.getcwd()\n",  # an aliased NON-terminating call
        "import os\ngetattr(os, '_exit')()\n",  # dynamic getattr -> the documented boundary
        "import os\ne = os._exit if 1 else os.getcwd\ne()\n",  # non-dotted value -> deferred
        "import os as _o\n_o = os\n_o._exit(1)\n",  # multiply-bound alias not trusted -> deferred
    ],
)
def test_b11_dynamic_or_benign_alias_stays_candidate(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _CAND


# =======================================================================================
# Batch-12 reopen — ``except*`` / ``ast.TryStar`` was the LONE straggler in the abort-tier
# detector.  `_statement_abort_tier` dispatched only ``ast.Try``, so a module-scope ``try*`` /
# ``except*`` / ``finally`` that unconditionally aborts import returned _TIER_NONE -> a static
# FALSE candidate (``except*`` is real Py3.11+ syntax, so this is a realistic gap).  The two
# sibling walkers already paired them (`_contains_break` matches ``(ast.Try, ast.TryStar)``;
# `_statement_cannot_raise` treats both via its conservative "a try may raise -> False" catch-all).
# The fix makes `_try_abort_tier` accept ``ast.Try | ast.TryStar`` and dispatches both.
#
# SOUNDNESS of the shared shape: ``ast.TryStar`` has the identical body/handlers/orelse/finalbody
# structure and its handlers are the SAME ``ast.ExceptHandler`` nodes matching the SAME types, so
# for the single unconditional abort the detector models an ``except*`` catches a lone exception
# iff a regular ``except`` of that type would (the ExceptionGroup wrapping changes DELIVERY, not
# whether it is caught).  The Try/TryStar PARITY battery below locks that equivalence permanently.
# NOTE: a NARROW ``except* ValueError`` swallowing ``raise ValueError`` is needs_review — identical
# to its regular ``except ValueError`` twin — because the tier model credits only the tier
# REPRESENTATIVE catcher (``Exception`` / ``BaseException``); that is a pre-existing conservative
# fail-closed over-rejection (never a false candidate), NOT introduced here.
# =======================================================================================


@pytest.mark.parametrize(
    "main_body",
    [
        # uncaught body raise (RuntimeError NOT caught by except* ValueError) -> import dies rc 1.
        "try:\n    raise RuntimeError()\nexcept* ValueError:\n    pass\n",
        # finally aborts uncatchably -> rc 3.
        "try:\n    pass\nexcept* ValueError:\n    pass\nfinally:\n    import os\n    os._exit(3)\n",
        # uncaught body sys.exit (BASE tier not caught by except* ValueError) -> rc 2.
        "import sys\ntry:\n    sys.exit(2)\nexcept* ValueError:\n    pass\n",
        # a matching handler that itself re-raises -> aborts -> rc 1.
        "try:\n    raise ValueError()\nexcept* ValueError:\n    raise RuntimeError()\n",
        # nested inside a statically-True if -> entry is certain -> abort is unconditional.
        "if True:\n    try:\n        raise RuntimeError()\n    except* ValueError:\n        pass\n",
        # except* Exception does NOT catch a BASE-tier sys.exit (only BaseException does).
        "import sys\ntry:\n    sys.exit(2)\nexcept* Exception:\n    pass\n",
    ],
)
def test_b12_trystar_unconditional_abort_is_needs_review(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _NR


@pytest.mark.parametrize(
    "main_body",
    [
        # a try* that genuinely runs to completion (no abort) -> RUNS.
        "try:\n    pass\nexcept* ValueError:\n    pass\n",
        # except* Exception (the tier representative) swallows a body ValueError -> RUNS.
        "try:\n    raise ValueError()\nexcept* Exception:\n    pass\n",
        # except* BaseException swallows the same body ValueError -> RUNS.
        "try:\n    raise ValueError()\nexcept* BaseException:\n    pass\n",
    ],
)
def test_b12_trystar_that_completes_or_swallows_stays_candidate(main_body: str) -> None:
    assert _detect(_e3_top(main_body)) is _CAND


# ---- Try/TryStar parity lock: the abort detector must give a ``try*`` the SAME verdict as its
# regular ``try`` twin over the same single-statement body, so TryStar can never drift from Try.


@pytest.mark.parametrize(
    ("body_stmt", "handler_type", "handler_stmt", "expected"),
    [
        ("raise RuntimeError()", "ValueError", "pass", _NR),  # uncaught -> both NR
        ("raise ValueError()", "ValueError", "pass", _NR),  # narrow catch NOT credited -> both NR
        ("raise ValueError()", "Exception", "pass", _CAND),  # representative catches -> both CAND
        ("raise ValueError()", "BaseException", "pass", _CAND),  # broadest catches -> both CAND
        ("raise ValueError()", "Exception", "raise KeyError()", _NR),  # caught, handler aborts
        ("pass", "ValueError", "pass", _CAND),  # no abort -> both CAND
    ],
)
def test_b12_trystar_verdict_matches_regular_try(
    body_stmt: str, handler_type: str, handler_stmt: str, expected: ReleaseAssessment
) -> None:
    regular = f"try:\n    {body_stmt}\nexcept {handler_type}:\n    {handler_stmt}\n"
    star = f"try:\n    {body_stmt}\nexcept* {handler_type}:\n    {handler_stmt}\n"
    assert _detect(_e3_top(regular)) is expected  # the regular-except twin
    assert _detect(_e3_top(star)) is expected  # the except* form must AGREE


@pytest.mark.parametrize(
    ("guard", "expected"),
    [
        ("Exception", _NR),  # Exception does NOT catch a BASE-tier SystemExit -> both NR
        ("BaseException", _CAND),  # BaseException DOES catch it -> both CAND
    ],
)
def test_b12_trystar_base_tier_catch_matches_regular_try(
    guard: str, expected: ReleaseAssessment
) -> None:
    regular = f"import sys\ntry:\n    sys.exit(2)\nexcept {guard}:\n    pass\n"
    star = f"import sys\ntry:\n    sys.exit(2)\nexcept* {guard}:\n    pass\n"
    assert _detect(_e3_top(regular)) is expected
    assert _detect(_e3_top(star)) is expected


def test_b12_trystar_finally_abort_matches_regular_try() -> None:
    # An abort in a finally is unconditional for try AND try* alike -> both needs_review.
    tail = " ValueError:\n    pass\nfinally:\n    import os\n    os._exit(3)\n"
    regular = "try:\n    pass\nexcept" + tail
    star = "try:\n    pass\nexcept*" + tail
    assert _detect(_e3_top(regular)) is _NR
    assert _detect(_e3_top(star)) is _NR
