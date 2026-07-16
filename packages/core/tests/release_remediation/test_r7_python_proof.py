"""Authoritative Python release-proof attack matrix (R7 Round-5, Batch-1 reopen).

Two single sources of truth are exercised here:

* the one PEP 508/440 dependency grammar — ``active_requirement_name`` (names/extras/
  specifiers/URLs via :mod:`packaging`, invariance-aware markers) and
  ``requires_python_supported``; and
* the one Python target reachability proof — ``python_entrypoint_error``.

Every reproduced adversarial case and every over-rejection positive control is a permanent
regression here, and the end-to-end rows drive the real ``detect_release`` (no mocks).
"""

from __future__ import annotations

from typing import Any

import pytest
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.python_proof import (
    active_requirement_name,
    python_entrypoint_error,
    requires_python_supported,
)
from disco.core.release.spec import ReleaseAssessment, ReleaseIntent, RuntimeStrategy

# ---------------------------------------------------------------------------------------
# One dependency grammar: valid PEP 508/440 forms are accepted.
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [
        # names / normalization / extras
        ("FastAPI", "fastapi"),
        ("zope.interface", "zope-interface"),
        ("my_pkg", "my-pkg"),
        ("A---B", "a-b"),
        ("uvicorn[standard]", "uvicorn"),
        ("pkg[one,two_three,dotted.extra]", "pkg"),
        ("pkg [one]", "pkg"),
        ("pkg[]", "pkg"),
        # ordinary specifiers
        ("flask==3", "flask"),
        ("flask != 2.0", "flask"),
        ("flask>=2.0,<4.0", "flask"),
        ("flask (>=2.0, !=3.0.1)", "flask"),
        ("flask~=3.0", "flask"),
        ("flask==0003.000", "flask"),
        ("pkg>=1.0,!=1.5,<2.0", "pkg"),
        # valid wildcards (only with == / !=) and prereleases/post/dev/epoch/local
        ("uvicorn==0.30.*", "uvicorn"),
        ("uvicorn==1.*", "uvicorn"),
        ("uvicorn!=1.*", "uvicorn"),
        ("pkg==1.0rc1", "pkg"),
        ("pkg==1.0a1", "pkg"),
        ("pkg==1.0b1", "pkg"),
        ("pkg==1.0.dev1", "pkg"),
        ("pkg==1.0.post1", "pkg"),
        ("pkg==1!1.0", "pkg"),
        ("pkg==1.0+local", "pkg"),
        ("pkg===1.0", "pkg"),
        ("uvicorn[standard]==0.30.*", "uvicorn"),
        # requirements-file comment
        ("fastapi  # ordinary requirements comment", "fastapi"),
        # invariant markers that hold
        ('uvicorn; python_version == "3.13"', "uvicorn"),
        ('uvicorn; python_version == "3.13.0"', "uvicorn"),
        ('uvicorn; python_version >= "3.12"', "uvicorn"),
        ('uvicorn; python_version < "4"', "uvicorn"),
        ('uvicorn; python_version ~= "3.13"', "uvicorn"),
        ('uvicorn; sys_platform == "linux"', "uvicorn"),
        ('uvicorn; os_name != "nt"', "uvicorn"),
        ('uvicorn; platform_system == "Linux"', "uvicorn"),
        ('uvicorn; platform_python_implementation == "CPython"', "uvicorn"),
        ('uvicorn; implementation_name == "cpython"', "uvicorn"),
        ('uvicorn; "linux" == sys_platform', "uvicorn"),
        ('uvicorn; "lin" in sys_platform', "uvicorn"),
        ('uvicorn; "win" not in sys_platform', "uvicorn"),
        ('uvicorn; python_version >= "3.13" and sys_platform == "linux"', "uvicorn"),
        (
            'uvicorn; (python_version < "3" or python_version >= "3.13") and os_name == "posix"',
            "uvicorn",
        ),
        ('uvicorn; sys_platform == "linux"  # fixed target', "uvicorn"),
    ],
)
def test_requirement_grammar_accepts_valid_forms(requirement: str, expected: str) -> None:
    assert active_requirement_name(requirement) == expected


@pytest.mark.parametrize(
    "requirement",
    [
        'uvicorn; python_version < "3.13"',
        'uvicorn; python_version > "3.13.0"',
        'uvicorn; python_version ~= "3.13.1"',
        'uvicorn; sys_platform == "win32"',
        'uvicorn; os_name == "nt"',
        'uvicorn; platform_system != "Linux"',
        'uvicorn; platform_python_implementation == "PyPy"',
        'uvicorn; implementation_name != "cpython"',
        'uvicorn; "darwin" in sys_platform',
        'uvicorn; python_version < "3" or sys_platform == "win32"',
    ],
)
def test_requirement_grammar_returns_none_for_a_provably_false_marker(requirement: str) -> None:
    assert active_requirement_name(requirement) is None


@pytest.mark.parametrize(
    "requirement",
    [
        # empty / comment-only / directives
        "",
        "   ",
        "# comment only",
        "-r requirements.txt",
        "--requirement requirements.txt",
        "-c constraints.txt",
        "-e .",
        "--index-url https://example.invalid/simple",
        # URLs / paths (unsupported registry sources)
        "git+https://example.invalid/repo",
        "https://example.invalid/pkg.whl",
        "pkg @ https://example.invalid/pkg.whl",
        "pkg@file:///tmp/pkg",
        ".",
        "./pkg",
        "../pkg",
        "/tmp/pkg",
        "pkg/path",
        "pkg\\path",
        "pkg-",
        "-pkg",
        # malformed extras (note: bare ``pkg[]`` is a VALID benign form -> see accept list)
        "pkg[one,]",
        "pkg[one two]",
        "pkg[,]",
        # invalid version comparators / wildcards
        "pkg==latest",
        "pkg~=1",
        "pkg>=1.0,,<2",
        "pkg>=1.*",
        "pkg<=2.*",
        "pkg~=1.*",
        # contradictory / unsatisfiable constraints
        "pkg>2,<1",
        "pkg==1.0,==2.0",
        "pkg>=2,<=1",
        "pkg==1.5,!=1.5",
        # moving / non-invariant / unknown markers
        'pkg; python_full_version == "3.13.1"',
        'pkg; implementation_version >= "3.13"',
        'pkg; platform_machine == "x86_64"',
        'pkg; platform_release == "6"',
        'pkg; platform_version == "1"',
        'pkg; extra == "feature"',
        'pkg; unknown_marker == "value"',
        # marker grammar outside PEP 508
        "pkg; python_version == 3.13",
        'pkg; python_version == "3.13.*"',
        'pkg; python_version in "3.13"',
        'pkg; sys_platform === "linux"',
        'pkg; sys_platform == "linux" == os_name',
        'pkg; python_version < "4" < "5"',
        'pkg; not (python_version == "2")',
        'pkg; not sys_platform == "win32"',
        "pkg; sys_platform == 'linux\n'",
        'pkg; __import__("os") == "x"',
        "pkg#fragment",
    ],
)
def test_requirement_grammar_rejects_invalid_unprovable_and_unsatisfiable(requirement: str) -> None:
    with pytest.raises(ValueError) as raised:
        active_requirement_name(requirement)
    assert len(str(raised.value)) <= 100


def test_requirement_limits_are_bounded_and_errors_are_value_free() -> None:
    secret = "SECRET_DO_NOT_ECHO"
    for requirement in (
        f"{secret} @ https://example.invalid/{secret}",
        "pkg; " + "(" * 20 + 'sys_platform == "linux"' + ")" * 20,
        "pkg; " + " or ".join(['sys_platform == "linux"'] * 70),
        "x" * 2050,
    ):
        with pytest.raises(ValueError) as raised:
            active_requirement_name(requirement)
        assert secret not in str(raised.value)
        assert len(str(raised.value)) <= 100


# ---------------------------------------------------------------------------------------
# The same engine decides requires-python against the image's invariant version band.
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("specifier", "supported"),
    [
        (">=3.9", True),
        (">=3.8,<4", True),
        ("==3.13.*", True),
        ("~=3.13", True),
        (">=3.13", True),
        ("<3.13", False),
        (">=3.14", False),
        ("<3.13.5", False),  # patch-dependent -> undecidable from the invariant minor
        # Mid-range patch hole: a 2-point {min,max} band false-certifies this because both
        # endpoints satisfy it; only a DENSE sample over the plausible patch range rejects it.
        ("!=3.13.5", False),
        ("!=3.13.30", False),
        ("==3.13.4", False),  # a single patch pin cannot cover an unknown-patch image
        ("==3.12.*", False),
    ],
)
def test_requires_python_supported(specifier: str, supported: bool) -> None:
    assert requires_python_supported(specifier) is supported


def test_requires_python_invalid_specifier_is_rejected() -> None:
    for specifier in ("not-a-spec", ">=>3.13", "3.13"):
        with pytest.raises(ValueError):
            requires_python_supported(specifier)


# ---------------------------------------------------------------------------------------
# One target reachability proof: closed framework/callable/factory shapes are accepted.
# ---------------------------------------------------------------------------------------


def _proof(
    source: str,
    attribute: str = "app",
    *,
    server: str = "uvicorn",
    factory: bool = False,
    packages: set[str] | None = None,
    local_modules: set[str] | None = None,
    local_module_paths: set[str] | None = None,
    target_package: tuple[str, ...] = (),
    local_module_sources: dict[str, str] | None = None,
) -> str | None:
    return python_entrypoint_error(
        source,
        attribute,
        server=server,
        factory=factory,
        installed_packages=frozenset(packages or set()),
        local_modules=frozenset(local_modules or set()),
        local_module_paths=frozenset(local_module_paths or set()),
        target_package=target_package,
        local_module_sources=local_module_sources,
    )


_FA = "from fastapi import FastAPI\n"


@pytest.mark.parametrize(
    ("source", "attribute", "server", "factory", "packages", "locals_"),
    [
        (_FA + "app = FastAPI()\n", "app", "uvicorn", False, {"fastapi"}, {}),
        (_FA + "app: FastAPI = FastAPI()\n", "app", "hypercorn", False, {"fastapi"}, {}),
        ("import fastapi\napp = fastapi.FastAPI()\n", "app", "uvicorn", False, {"fastapi"}, {}),
        (
            "from starlette.applications import Starlette\napp = Starlette()\n",
            "app",
            "uvicorn",
            False,
            {"starlette"},
            {},
        ),
        (
            "from flask import Flask\napp = Flask(__name__)\n",
            "app",
            "gunicorn",
            False,
            {"flask"},
            {},
        ),
        ("async def app(scope, receive, send):\n    pass\n", "app", "uvicorn", False, set(), {}),
        # positive control: a GUARDED abort (nested under a conditional, not module-scope) does
        # not abort import at runtime, so it must NOT be over-rejected as unbootable.
        (
            _FA + "app = FastAPI()\nif __name__ == '__main__':\n    raise SystemExit(0)\n",
            "app",
            "uvicorn",
            False,
            {"fastapi"},
            {},
        ),
        (
            "def app(environ, start_response):\n    start_response('200 OK', [])\n    return []\n",
            "app",
            "gunicorn",
            False,
            set(),
            {},
        ),
        (
            "import orjson\nasync def app(scope, receive, send):\n    orjson.dumps(scope)\n",
            "app",
            "uvicorn",
            False,
            {"orjson"},
            {},
        ),
        # genuine factory functions (direct return, via a single local binding, defaulted arg)
        (_FA + "def create():\n    return FastAPI()\n", "create", "uvicorn", True, {"fastapi"}, {}),
        (
            _FA + "def create():\n    a = FastAPI()\n    return a\n",
            "create",
            "hypercorn",
            True,
            {"fastapi"},
            {},
        ),
        (
            "from flask import Flask\ndef create(cfg=None):\n    return Flask(__name__)\n",
            "create",
            "gunicorn",
            True,
            {"flask"},
            {},
        ),
        # valid workspace-local absolute import in a multi-file app
        (
            "from models import Base\n" + _FA + "app = FastAPI()\n",
            "app",
            "uvicorn",
            False,
            {"fastapi"},
            {"roots": {"models"}, "dotted": {"models"}},
        ),
        (
            "import myapp.db\n" + _FA + "app = FastAPI()\n",
            "app",
            "uvicorn",
            False,
            {"fastapi"},
            {"roots": {"myapp"}, "dotted": {"myapp", "myapp.db"}},
        ),
    ],
)
def test_target_proof_accepts_reachable_apps_and_factories(
    source: str,
    attribute: str,
    server: str,
    factory: bool,
    packages: set[str],
    locals_: dict[str, set[str]],
) -> None:
    assert (
        _proof(
            source,
            attribute,
            server=server,
            factory=factory,
            packages=packages,
            local_modules=locals_.get("roots", set()),
            local_module_paths=locals_.get("dotted", set()),
        )
        is None
    )


def test_target_proof_accepts_valid_relative_import_in_a_package() -> None:
    # entry module app.main: `from .config import settings` resolves to app.config (present).
    assert (
        _proof(
            "from .config import settings\n" + _FA + "app = FastAPI()\n",
            "app",
            packages={"fastapi"},
            local_modules={"app"},
            local_module_paths={"app", "app.main", "app.config"},
            target_package=("app",),
        )
        is None
    )
    # `from ..config` from app.sub.main resolves to app.config (one package above): valid.
    assert (
        _proof(
            "from ..config import settings\n" + _FA + "app = FastAPI()\n",
            "app",
            packages={"fastapi"},
            local_modules={"app"},
            local_module_paths={"app", "app.sub", "app.sub.main", "app.config"},
            target_package=("app", "sub"),
        )
        is None
    )


def test_target_proof_rejects_relative_import_without_a_parent_package() -> None:
    # A TOP-LEVEL module (target_package == ()) has no parent package, so ANY relative import
    # raises "attempted relative import with no known parent package" at import time. Certifying
    # it would be a false candidate for a deterministically-unrunnable target.
    for source in (
        "from .config import settings\n" + _FA + "app = FastAPI()\n",
        "from . import config\n" + _FA + "app = FastAPI()\n",
    ):
        assert (
            _proof(
                source,
                "app",
                packages={"fastapi"},
                local_modules={"config"},
                local_module_paths={"main", "config"},
                target_package=(),
            )
            is not None
        )
    # `from ..config` from app.main goes ABOVE the top-level package `app` -> beyond top level.
    assert (
        _proof(
            "from ..config import settings\n" + _FA + "app = FastAPI()\n",
            "app",
            packages={"fastapi"},
            local_modules={"app", "config"},
            local_module_paths={"app", "app.main", "config"},
            target_package=("app",),
        )
        is not None
    )


# ---------------------------------------------------------------------------------------
# Workspace-local re-exports: the target is bound by ``from <module> import <name>`` and the
# proof recurses the SAME strict rules into the referenced module. A genuinely-runnable
# re-export becomes a candidate; every unrunnable/unprovable re-export stays needs_review.
# ---------------------------------------------------------------------------------------

_REAL_APP = "from fastapi import FastAPI\napp = FastAPI()\n"


def test_reexport_accepts_absolute_sibling() -> None:
    # main.py: `from real import app`; real.py defines a real FastAPI app; `uvicorn main:app`.
    assert (
        _proof(
            "from real import app\n",
            "app",
            packages={"fastapi"},
            local_modules={"main", "real"},
            local_module_paths={"main", "real"},
            target_package=(),
            local_module_sources={"real": _REAL_APP},
        )
        is None
    )


def test_reexport_accepts_package_relative() -> None:
    # pkg/main.py: `from .real import app`; pkg/real.py defines app; `uvicorn pkg.main:app`.
    assert (
        _proof(
            "from .real import app\n",
            "app",
            packages={"fastapi"},
            local_modules={"pkg"},
            local_module_paths={"pkg", "pkg.main", "pkg.real"},
            target_package=("pkg",),
            local_module_sources={"pkg.real": _REAL_APP},
        )
        is None
    )


def test_reexport_accepts_absolute_package() -> None:
    # pkg/main.py: `from pkg.real import app`; pkg/real.py defines app; `uvicorn pkg.main:app`.
    assert (
        _proof(
            "from pkg.real import app\n",
            "app",
            packages={"fastapi"},
            local_modules={"pkg"},
            local_module_paths={"pkg", "pkg.main", "pkg.real"},
            target_package=("pkg",),
            local_module_sources={"pkg.real": _REAL_APP},
        )
        is None
    )


def test_reexport_accepts_two_hop_chain() -> None:
    # main -> b -> c, all workspace modules, c defines the real app. Two hops, well within depth.
    assert (
        _proof(
            "from b import app\n",
            "app",
            packages={"fastapi"},
            local_modules={"main", "b", "c"},
            local_module_paths={"main", "b", "c"},
            local_module_sources={"b": "from c import app\n", "c": _REAL_APP},
        )
        is None
    )


def test_reexport_accepts_aliased_and_factory() -> None:
    # Aliased re-export (`from real import realapp as app`) resolves the ORIGINAL name in `real`.
    assert (
        _proof(
            "from real import realapp as app\n",
            "app",
            packages={"fastapi"},
            local_modules={"main", "real"},
            local_module_paths={"main", "real"},
            local_module_sources={"real": "from fastapi import FastAPI\nrealapp = FastAPI()\n"},
        )
        is None
    )
    # A `--factory` re-export carries the factory flag into the referenced module's proof.
    assert (
        _proof(
            "from real import create\n",
            "create",
            factory=True,
            packages={"fastapi"},
            local_modules={"main", "real"},
            local_module_paths={"main", "real"},
            local_module_sources={
                "real": "from fastapi import FastAPI\ndef create():\n    return FastAPI()\n"
            },
        )
        is None
    )


def test_reexport_rejects_top_level_relative() -> None:
    # A top-level module (target_package == ()) has no parent package, so `from .real import app`
    # raises "attempted relative import with no known parent package" -> must STAY needs_review,
    # even though `real` exists and defines a real app.
    assert (
        _proof(
            "from .real import app\n",
            "app",
            packages={"fastapi"},
            local_modules={"main", "real"},
            local_module_paths={"main", "real"},
            target_package=(),
            local_module_sources={"real": _REAL_APP},
        )
        is not None
    )


def test_reexport_rejects_non_workspace_module() -> None:
    # The referenced module is not a workspace file (no supplied source) -> fail closed, whether
    # or not the distribution happens to be installed. Recursion never leaves the workspace.
    for packages in ({"fastapi"}, {"fastapi", "starlette"}):
        assert (
            _proof(
                "from starlette.applications import app\n",
                "app",
                packages=packages,
                local_modules={"main"},
                local_module_paths={"main"},
                local_module_sources={},
            )
            is not None
        )


def test_reexport_rejects_cycle() -> None:
    # main -> a -> main -> ... . The visited-set catches the cycle and fails closed.
    assert (
        _proof(
            "from a import app\n",
            "app",
            packages={"fastapi"},
            local_modules={"main", "a"},
            local_module_paths={"main", "a"},
            local_module_sources={"main": "from a import app\n", "a": "from main import app\n"},
        )
        is not None
    )


def test_reexport_rejects_chain_to_unprovable_target() -> None:
    # The chain ultimately binds a non-app (None) or leaves the name undefined -> needs_review.
    assert (
        _proof(
            "from real import app\n",
            "app",
            packages={"fastapi"},
            local_modules={"main", "real"},
            local_module_paths={"main", "real"},
            local_module_sources={"real": "app = None\n"},
        )
        is not None
    )
    assert (
        _proof(
            "from real import app\n",
            "app",
            packages={"fastapi"},
            local_modules={"main", "real"},
            local_module_paths={"main", "real"},
            local_module_sources={"real": "x = 1\n"},
        )
        is not None
    )


def test_reexport_rejects_depth_overflow() -> None:
    # A re-export chain deeper than the hop ceiling fails closed even though the final module
    # defines a real app: bounding recursion is a hard security backstop against unbounded chains.
    chain = {f"m{index}": f"from m{index + 1} import app\n" for index in range(1, 6)}
    chain["m6"] = _REAL_APP
    names = {"main", *chain}
    assert (
        _proof(
            "from m1 import app\n",
            "app",
            packages={"fastapi"},
            local_modules=names,
            local_module_paths=names,
            local_module_sources=chain,
        )
        is not None
    )


def test_reexport_rejects_module_object_bindings() -> None:
    # `import real as app` and `from . import real` bind MODULE objects, never servable apps.
    assert (
        _proof(
            "import real as app\n",
            "app",
            packages={"fastapi"},
            local_modules={"main", "real"},
            local_module_paths={"main", "real"},
            local_module_sources={"real": _REAL_APP},
        )
        is not None
    )
    assert (
        _proof(
            "from . import real\n",
            "real",
            packages={"fastapi"},
            local_modules={"pkg"},
            local_module_paths={"pkg", "pkg.main", "pkg.real"},
            target_package=("pkg",),
            local_module_sources={"pkg.real": _REAL_APP},
        )
        is not None
    )


def test_reexport_stays_closed_without_supplied_sources() -> None:
    # The default (no ``local_module_sources``) preserves the pre-existing behavior EXACTLY:
    # a re-exported target is never proven, so no existing caller can gain a false candidate.
    assert (
        _proof(
            "from real import app\n",
            "app",
            packages={"fastapi"},
            local_modules={"main", "real"},
            local_module_paths={"main", "real"},
        )
        is not None
    )


# ---------------------------------------------------------------------------------------
# Name-collision ceiling (fail closed). A workspace root that is ALSO an installed
# distribution is a sys.path[0] shadow collision: the server puts the app dir first, so
# ``import <name>`` binds the workspace file and shadows the distribution for the whole
# process. The name is never certifiably both, and the shadow path is typically broken
# (self-import circular crash), so a colliding re-export OR a colliding module-scope import
# fails closed. Non-colliding workspace names (not installed distributions) are unaffected.
# ---------------------------------------------------------------------------------------


def test_reexport_rejects_collision_with_installed_distribution() -> None:
    # 8c: main re-exports ``app`` from a workspace ``fastapi.py`` that shadows the installed
    # ``fastapi`` and self-imports it (``from fastapi import FastAPI``) -> circular-import crash
    # at runtime. The resolved re-export root collides with an installed dist -> fail closed.
    assert (
        _proof(
            "from fastapi import app\n",
            "app",
            packages={"fastapi", "uvicorn"},
            local_modules={"main", "fastapi"},
            local_module_paths={"main", "fastapi"},
            local_module_sources={"fastapi": "from fastapi import FastAPI\napp = FastAPI()\n"},
        )
        is not None
    )
    # 8d: same class for the ``uvicorn`` name (workspace ``uvicorn.py``).
    assert (
        _proof(
            "from uvicorn import app\n",
            "app",
            packages={"fastapi", "uvicorn"},
            local_modules={"main", "uvicorn"},
            local_module_paths={"main", "uvicorn"},
            local_module_sources={"uvicorn": "from fastapi import FastAPI\napp = FastAPI()\n"},
        )
        is not None
    )


def test_direct_target_rejects_collision_with_installed_distribution() -> None:
    # RC1 (no re-export): the target module ``fastapi.py`` shadows the installed ``fastapi`` and
    # its own ``from fastapi import FastAPI`` becomes a self-import -> broken at runtime. The
    # colliding module-scope import is not waved through as "installed" -> fail closed.
    assert (
        _proof(
            "from fastapi import FastAPI\napp = FastAPI()\n",
            "app",
            packages={"fastapi", "uvicorn"},
            local_modules={"fastapi"},
            local_module_paths={"fastapi"},
        )
        is not None
    )


def test_collision_ceiling_rejects_raw_asgi_boundary() -> None:
    # BOUNDARY (documented collision ceiling): a workspace ``fastapi.py`` that defines a
    # self-contained raw-ASGI app with NO imports would genuinely run (the shadow needs no
    # installed ``fastapi``), yet re-exporting it still shadows the installed distribution for
    # the whole process. This is a genuine footgun, so the conservative fail-closed guard
    # rejects it too: the resolved re-export root ``fastapi`` collides with an installed dist.
    assert (
        _proof(
            "from fastapi import app\n",
            "app",
            packages={"fastapi", "uvicorn"},
            local_modules={"main", "fastapi"},
            local_module_paths={"main", "fastapi"},
            local_module_sources={"fastapi": "async def app(scope, receive, send):\n    pass\n"},
        )
        is not None
    )


def test_noncolliding_reexport_stays_candidate_unchanged() -> None:
    # Positive control: a re-export through a NON-installed workspace name (``real``) is NOT a
    # collision and must resolve exactly as before -> candidate (error is None). This guards
    # against the collision fix over-rejecting ordinary multi-file re-exports.
    assert (
        _proof(
            "from real import app\n",
            "app",
            packages={"fastapi", "uvicorn"},
            local_modules={"main", "real"},
            local_module_paths={"main", "real"},
            local_module_sources={"real": _REAL_APP},
        )
        is None
    )
    # A workspace module-scope import of a NON-installed name also still resolves.
    assert (
        _proof(
            "from models import Base\nfrom fastapi import FastAPI\napp = FastAPI()\n",
            "app",
            packages={"fastapi"},
            local_modules={"models"},
            local_module_paths={"models"},
        )
        is None
    )


@pytest.mark.parametrize(
    ("source", "attribute", "server", "factory", "packages"),
    [
        # non-callable / literal / collection / expression targets (not a proven app)
        ("app = object()\n", "app", "uvicorn", False, set()),
        ("app = None\n", "app", "uvicorn", False, set()),
        ("app = 42\n", "app", "gunicorn", False, set()),
        ("app = 'main:app'\n", "app", "uvicorn", False, set()),
        ("app = b''\n", "app", "uvicorn", False, set()),
        ("app = [1, 2]\n", "app", "uvicorn", False, set()),
        ("app = {}\n", "app", "uvicorn", False, set()),
        ("app = 1 + 2\n", "app", "uvicorn", False, set()),
        ("app = make_app()\n", "app", "uvicorn", False, set()),
        ("app = make_app()\n", "app", "uvicorn", False, {"fastapi"}),
        # annotation-only binding does not create a runtime object
        (_FA + "app: FastAPI\n", "app", "uvicorn", False, {"fastapi"}),
        # multiple / overwritten / ambiguous bindings
        (_FA + "app = FastAPI()\napp = object()\n", "app", "uvicorn", False, {"fastapi"}),
        (_FA + "if True:\n    app = FastAPI()\n", "app", "uvicorn", False, {"fastapi"}),
        ("app = other = object()\n", "app", "uvicorn", False, set()),
        # uninstalled framework / import
        (_FA + "app = FastAPI()\n", "app", "uvicorn", False, set()),
        ("import orjson\napp = object()\n", "app", "uvicorn", False, set()),
        # missing workspace-local module (helpers absent) and top-level relative import
        (
            "from helpers import x\n" + _FA + "app = FastAPI()\n",
            "app",
            "uvicorn",
            False,
            {"fastapi"},
        ),
        ("from . import cfg\n" + _FA + "app = FastAPI()\n", "app", "uvicorn", False, {"fastapi"}),
        # --factory on an instance / undefined / required arg / non-app return / bare return
        (_FA + "app = FastAPI()\n", "app", "uvicorn", True, {"fastapi"}),
        ("app = make_app()\n", "app", "uvicorn", True, set()),
        (_FA + "def app(x):\n    return FastAPI()\n", "app", "uvicorn", True, {"fastapi"}),
        ("def app():\n    return object()\n", "app", "uvicorn", True, set()),
        (_FA + "def app():\n    x = FastAPI()\n", "app", "uvicorn", True, {"fastapi"}),
        # unconditional import-time abort before the binding
        (
            "raise RuntimeError()\n" + _FA + "app = FastAPI()\n",
            "app",
            "uvicorn",
            False,
            {"fastapi"},
        ),
        (
            "import sys\nsys.exit(1)\n" + _FA + "app = FastAPI()\n",
            "app",
            "uvicorn",
            False,
            {"fastapi"},
        ),
        ("assert False\n" + _FA + "app = FastAPI()\n", "app", "uvicorn", False, {"fastapi"}),
        # unconditional import-time abort AFTER the binding: the module still fails to import,
        # so uvicorn never reaches the (already-bound) app. Scanning only up to the binding
        # would false-certify these -- the proof must reject an abort anywhere at module scope.
        (_FA + "app = FastAPI()\nraise RuntimeError()\n", "app", "uvicorn", False, {"fastapi"}),
        (
            "import sys\n" + _FA + "app = FastAPI()\nsys.exit(1)\n",
            "app",
            "uvicorn",
            False,
            {"fastapi"},
        ),
        (_FA + "app = FastAPI()\nexit(1)\n", "app", "uvicorn", False, {"fastapi"}),
        (_FA + "app = FastAPI()\nassert False\n", "app", "uvicorn", False, {"fastapi"}),
        # wrong-protocol shapes for the server
        ("def app(environ, start_response):\n    return []\n", "app", "uvicorn", False, set()),
        ("async def app(scope, receive, send):\n    pass\n", "app", "gunicorn", False, set()),
        # dynamic / aliased / mutated constructor identities remain rejected
        ("FastAPI = object\napp = FastAPI()\n", "app", "uvicorn", False, set()),
        ("from fake import FastAPI\napp = FastAPI()\n", "app", "uvicorn", False, {"fake"}),
        (_FA + "app = (lambda: FastAPI())()\n", "app", "uvicorn", False, {"fastapi"}),
        (_FA + "args = []\napp = FastAPI(*args)\n", "app", "uvicorn", False, {"fastapi"}),
    ],
)
def test_target_proof_rejects_unreachable_and_unbootable_targets(
    source: str,
    attribute: str,
    server: str,
    factory: bool,
    packages: set[str],
) -> None:
    error = _proof(source, attribute, server=server, factory=factory, packages=packages)
    assert error is not None and len(error) <= 100


def test_target_proof_source_and_error_limits_are_bounded() -> None:
    secret = "SECRET_ATTRIBUTE_DO_NOT_ECHO"
    error = python_entrypoint_error(
        "x = 1\n" * 200_000,
        secret,
        server="uvicorn",
        factory=False,
        installed_packages=frozenset(),
    )
    assert error is not None and secret not in error and len(error) <= 100


# ---------------------------------------------------------------------------------------
# End-to-end: the real detector fails closed on every reproduced defect and keeps the
# over-rejection positive controls a candidate. (No mocks — real `detect_release`.)
# ---------------------------------------------------------------------------------------


def _detect_python(
    files: dict[str, str], start_cmd: tuple[str, ...] = ("uvicorn", "main:app"), **intent: Any
) -> ReleaseAssessment:
    data: dict[str, Any] = {"runtime": RuntimeStrategy.python, "start_cmd": start_cmd}
    data.update(intent)
    result = detect_release(dict(files), intent=ReleaseIntent(**data), provenance=Provenance())
    return result.assessment


_FASTAPI_MAIN = "from fastapi import FastAPI\napp = FastAPI()\n"
_PYPROJECT = (
    "[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n"
    "[project]\nname='x'\nversion='1.0.0'\nrequires-python='{rp}'\n"
    "dependencies=['fastapi', 'uvicorn']\n"
)


@pytest.mark.parametrize(
    ("case_id", "files", "start_cmd"),
    [
        # PY-02..05: markers referencing non-invariant variables / non-PEP-440 ordering.
        ("PY-02", {"requirements.txt": 'fastapi\nuvicorn; platform_release == ""\n'}, None),
        ("PY-03", {"requirements.txt": 'fastapi\nuvicorn; platform_machine == "x86_64"\n'}, None),
        (
            "PY-04",
            {"requirements.txt": 'fastapi\nuvicorn; implementation_version == "3.13.0"\n'},
            None,
        ),
        ("PY-05", {"requirements.txt": 'fastapi\nuvicorn; python_version < "3.13.0a1"\n'}, None),
        # PY-06..08: target reachability.
        ("PY-06", {"requirements.txt": "uvicorn\n"}, None),  # main imports uninstalled fastapi
        ("PY-07", {"requirements.txt": "fastapi\nuvicorn\n"}, ("uvicorn", "main:app", "--factory")),
        ("PY-08", {"requirements.txt": "uvicorn\n", "main.py": "app = None\n"}, None),
        # extra target-proof end-to-end negatives
        ("OBJ", {"requirements.txt": "uvicorn\n", "main.py": "app = object()\n"}, None),
        (
            "RAISE",
            {
                "requirements.txt": "fastapi\nuvicorn\n",
                "main.py": "raise SystemExit()\n" + _FASTAPI_MAIN,
            },
            None,
        ),
        # REQ-01..04: dependency grammar.
        ("REQ-01", {"requirements.txt": 'fastapi\nuvicorn; python_version < "4" < "5"\n'}, None),
        ("REQ-02", {"requirements.txt": 'fastapi\nuvicorn; not (python_version == "2")\n'}, None),
        ("REQ-03", {"requirements.txt": "fastapi\nuvicorn[bad,,extra]\n"}, None),
        ("REQ-04", {"requirements.txt": "fastapi\nuvicorn>=1.*\n"}, None),
    ],
)
def test_detection_fails_closed_on_reproduced_python_defects(
    case_id: str, files: dict[str, str], start_cmd: tuple[str, ...] | None
) -> None:
    tree = {"main.py": _FASTAPI_MAIN, **files}
    kwargs = {"start_cmd": start_cmd} if start_cmd is not None else {}
    assert _detect_python(tree, **kwargs) is ReleaseAssessment.needs_review


def test_detection_fails_closed_on_incompatible_requires_python() -> None:
    # REQ-05: pip install . against an incompatible requires-python contract.
    files = {"pyproject.toml": _PYPROJECT.format(rp="<3.13"), "main.py": _FASTAPI_MAIN}
    assert _detect_python(files) is ReleaseAssessment.needs_review


@pytest.mark.parametrize(
    ("case_id", "files", "start_cmd"),
    [
        # Ordinary valid FastAPI service.
        ("valid", {"requirements.txt": "fastapi\nuvicorn\n", "main.py": _FASTAPI_MAIN}, None),
        # REQ-06: a valid recursive `-r` include resolves and is a candidate.
        (
            "REQ-06",
            {
                "requirements.txt": "-r base.txt\n",
                "base.txt": "fastapi\nuvicorn\n",
                "main.py": _FASTAPI_MAIN,
            },
            None,
        ),
        # Valid wildcard / prerelease specifiers do not block a real app.
        (
            "wildcards",
            {"requirements.txt": "fastapi==0.*\nuvicorn!=1.*\n", "main.py": _FASTAPI_MAIN},
            None,
        ),
        # Genuine factory + --factory.
        (
            "factory",
            {
                "requirements.txt": "fastapi\nuvicorn\n",
                "main.py": "from fastapi import FastAPI\ndef create_app():\n    return FastAPI()\n",
            },
            ("uvicorn", "main:create_app", "--factory"),
        ),
        # Multi-file app with a valid workspace-local import.
        (
            "local-import",
            {
                "requirements.txt": "fastapi\nuvicorn\n",
                "models.py": "Base = object\n",
                "main.py": (
                    "from models import Base\nfrom fastapi import FastAPI\napp = FastAPI()\n"
                ),
            },
            None,
        ),
    ],
)
def test_detection_keeps_valid_python_intents_a_candidate(
    case_id: str, files: dict[str, str], start_cmd: tuple[str, ...] | None
) -> None:
    kwargs = {"start_cmd": start_cmd} if start_cmd is not None else {}
    assert _detect_python(files, **kwargs) is ReleaseAssessment.candidate


def test_detection_keeps_compatible_requires_python_a_candidate() -> None:
    files = {"pyproject.toml": _PYPROJECT.format(rp=">=3.9"), "main.py": _FASTAPI_MAIN}
    assert _detect_python(files) is ReleaseAssessment.candidate


# ---------------------------------------------------------------------------------------
# End-to-end name-collision ceiling: a workspace module whose ROOT collides with an
# installed distribution shadows it on sys.path[0] and the emitted ``uvicorn <mod>:<name>``
# deterministically fails to import. The real detector must fail closed on every such case
# (8c, 8d, RC1, and the raw-ASGI collision boundary) while keeping NON-colliding re-exports a
# candidate. (No mocks -- real ``detect_release``.)
# ---------------------------------------------------------------------------------------

_REQS_FASTAPI = "fastapi\nuvicorn\n"
_REEXPORT_SELF = "from fastapi import FastAPI\napp = FastAPI()\n"


@pytest.mark.parametrize(
    ("case_id", "files", "start_cmd"),
    [
        # 8c: main re-exports app from a workspace fastapi.py that self-imports FastAPI ->
        # partially-initialized-module circular import at runtime.
        (
            "8c",
            {
                "requirements.txt": _REQS_FASTAPI,
                "main.py": "from fastapi import app\n",
                "fastapi.py": _REEXPORT_SELF,
            },
            ("uvicorn", "main:app"),
        ),
        # 8d: same class for the uvicorn name (workspace uvicorn.py).
        (
            "8d",
            {
                "requirements.txt": _REQS_FASTAPI,
                "main.py": "from uvicorn import app\n",
                "uvicorn.py": _REEXPORT_SELF,
            },
            ("uvicorn", "main:app"),
        ),
        # RC1: direct target (no re-export) -- workspace fastapi.py shadows installed fastapi
        # and self-imports it. Pre-existing false candidate, same root cause.
        (
            "RC1",
            {"requirements.txt": _REQS_FASTAPI, "fastapi.py": _REEXPORT_SELF},
            ("uvicorn", "fastapi:app"),
        ),
        # Boundary (documented collision ceiling): a self-contained raw-ASGI shadow with no
        # imports would run, but shadowing an installed distribution is a genuine footgun, so the
        # conservative guard fails closed here too. Verdict: needs_review.
        (
            "raw-asgi-collision-boundary",
            {
                "requirements.txt": _REQS_FASTAPI,
                "main.py": "from fastapi import app\n",
                "fastapi.py": "async def app(scope, receive, send):\n    pass\n",
            },
            ("uvicorn", "main:app"),
        ),
    ],
)
def test_detection_fails_closed_on_name_collision_with_installed_distribution(
    case_id: str, files: dict[str, str], start_cmd: tuple[str, ...]
) -> None:
    assert _detect_python(files, start_cmd=start_cmd) is ReleaseAssessment.needs_review


def test_detection_keeps_noncolliding_reexport_a_candidate() -> None:
    # Positive control against over-rejection: ``real`` is NOT an installed distribution, so a
    # re-export through it is not a collision and stays a candidate exactly as before.
    files = {
        "requirements.txt": _REQS_FASTAPI,
        "main.py": "from real import app\n",
        "real.py": _REEXPORT_SELF,
    }
    assert _detect_python(files, start_cmd=("uvicorn", "main:app")) is ReleaseAssessment.candidate
