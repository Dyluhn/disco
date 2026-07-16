"""Batch-5 (adversarial non-bypass hardening) — the gunicorn ``--config`` syntax proof.

One detection defect the frozen adversarial harness pins (SERVER-06):

* SERVER-06 — a gunicorn start whose ``--config <file>`` is existence-checked but never
  PARSED is a false ``candidate``. gunicorn EXECUTES its ``--config`` file AS PYTHON at
  startup (the arbiter runs the module body to read the settings), so a shipped
  ``gunicorn.conf.py`` whose body is not valid Python is a boot-time ``SyntaxError`` — the
  master aborts before any worker binds and the deployment is unreachable. Existence is not
  runnability: the config must additionally ``ast.parse`` (fail closed on binary/oversized
  bytes), mirroring the migrate-script proof (``_python_migrate_blocker``).

Scope discipline (proven by the controls below): the parse is STRICTLY gunicorn ``--config``.
uvicorn ``--ssl-keyfile``/``--ssl-certfile`` are PEM/key material (not Python) and the
else-branch (hypercorn) ``--config`` is an unknown format (hypercorn also accepts TOML);
ast.parsing either would over-reject a valid deploy, so neither is parsed.

These live OUTSIDE the frozen ``export_track1_closeout`` dirs (no marker), so they never
perturb the acceptance manifest.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from disco.core.release.command_grammar import GUNICORN_BUILTIN_WORKER_CLASSES
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.spec import ReleaseAssessment, ReleaseIntent, RuntimeStrategy

# ---- shared fixtures (the exact WSGI/ASGI app shapes the gunicorn fixtures use) ----

_WSGI_APP = (
    "def app(environ, start_response):\n"
    "    start_response('200 OK', [('Content-Type', 'text/plain')])\n"
    "    return [b'ok']\n"
)
_ASGI_APP = "async def app(scope, receive, send):\n    pass\n"

_GUNICORN_BASE: dict[str, str] = {"requirements.txt": "gunicorn\n", "main.py": _WSGI_APP}

# THE textbook gunicorn.conf.py — a stdlib import that MUST resolve (over-rejecting it is a
# real regression), reused by the explicit and auto-discovery over-rejection guards below.
_CANONICAL_CFG = "import multiprocessing\nworkers = multiprocessing.cpu_count() * 2 + 1\n"


def _assess(files: Mapping[str, str | bytes], start_cmd: tuple[str, ...]):  # noqa: ANN202
    result = detect_release(
        files,
        intent=ReleaseIntent(runtime=RuntimeStrategy.python, start_cmd=start_cmd),
        provenance=Provenance(),
    )
    return result.assessment, {b.code for b in result.blockers}, result.blockers


# ===========================================================================
# SERVER-06 — the gunicorn --config file must PARSE as Python (not merely exist).
# ===========================================================================


@pytest.mark.parametrize(
    "config_body",
    [
        "%%% not python\n",  # the exact frozen-harness SERVER-06 body
        "bind = = \n",  # a plausible-looking but unparseable settings line
        "workers = \n",  # a truncated assignment (RHS missing)
        "import\n",  # a bare keyword
    ],
    ids=["percent-garbage", "double-equals", "truncated-assign", "bare-keyword"],
)
def test_unparseable_gunicorn_config_fails_closed(config_body: str) -> None:
    """SERVER-06: a shipped ``gunicorn.conf.py`` whose body is not valid Python fails
    closed to ``needs_review`` with the ``entrypoint_unresolved`` code — gunicorn executes
    the config at startup, so an unparseable body aborts the worker before it can bind.
    Baseline over-accepts it as a ``candidate`` (existence-only proof)."""
    files = dict(_GUNICORN_BASE, **{"gunicorn.conf.py": config_body})
    assessment, codes, blockers = _assess(
        files, ("gunicorn", "--config", "gunicorn.conf.py", "main:app")
    )
    assert assessment is ReleaseAssessment.needs_review, (
        f"an unparseable gunicorn --config must fail closed; saw {assessment!r}"
    )
    assert "entrypoint_unresolved" in codes, f"expected entrypoint_unresolved, saw {sorted(codes)}"
    # The message must name the real defect so a future edit that drops the parse is caught.
    assert any("does not parse" in b.message for b in blockers), (
        f"the blocker must explain the config does not parse; saw {[b.message for b in blockers]}"
    )


def test_binary_gunicorn_config_fails_closed_before_parse() -> None:
    """SERVER-06 (bounded): a ``gunicorn.conf.py`` whose bytes cannot be scanned completely
    (a NUL-laden binary blob) fails closed BEFORE ``ast.parse`` — validity as executable
    Python is unproven, so the config cannot be certified. Mirrors the migrate proof's
    scan-complete guard."""
    files: dict[str, str | bytes] = dict(_GUNICORN_BASE)
    files["gunicorn.conf.py"] = b"bind = '0.0.0.0:8000'\x00\x00\n"
    assessment, codes, _ = _assess(files, ("gunicorn", "--config", "gunicorn.conf.py", "main:app"))
    assert assessment is ReleaseAssessment.needs_review, (
        f"a binary gunicorn --config must fail closed; saw {assessment!r}"
    )
    assert "entrypoint_unresolved" in codes, f"expected entrypoint_unresolved, saw {sorted(codes)}"


@pytest.mark.parametrize(
    "config_body",
    [
        'bind = "0.0.0.0:8000"\nworkers = 2\n',  # the canonical valid config
        "# only a comment\n",  # a comment-only file parses as an empty module
        "",  # an empty config file is valid Python
    ],
    ids=["canonical", "comment-only", "empty"],
)
def test_valid_gunicorn_config_stays_candidate(config_body: str) -> None:
    """SERVER-06 positive control: a ``gunicorn.conf.py`` that IS valid Python stays a
    ``candidate`` — the parse gate rejects only unparseable configs, never a legitimate one."""
    files = dict(_GUNICORN_BASE, **{"gunicorn.conf.py": config_body})
    assessment, _, _ = _assess(files, ("gunicorn", "--config", "gunicorn.conf.py", "main:app"))
    assert assessment is ReleaseAssessment.candidate, (
        f"a valid gunicorn --config must stay a candidate; saw {assessment!r}"
    )


def test_gunicorn_without_config_stays_candidate() -> None:
    """SERVER-06 control: a gunicorn start with NO ``--config`` is unaffected by the parse
    gate (there is no config to parse) and stays a ``candidate``."""
    assessment, _, _ = _assess(_GUNICORN_BASE, ("gunicorn", "main:app"))
    assert assessment is ReleaseAssessment.candidate, (
        f"gunicorn without --config must stay a candidate; saw {assessment!r}"
    )


# ===========================================================================
# Scope discipline — the parse is gunicorn `--config` ONLY. A non-Python file
# reached through a DIFFERENT option/server must never be ast.parsed by this fix.
# ===========================================================================


def test_uvicorn_ssl_material_is_not_parsed_as_python() -> None:
    """SERVER-06 scope: uvicorn ``--ssl-keyfile`` PEM material must never be ast.parsed by
    the gunicorn config gate. The grammar already rejects TLS material upstream (the ratified
    SERVER-03 oracle), so the result is ``needs_review`` with ``toolchain_unsupported`` —
    NOT an ``entrypoint_unresolved`` "does not parse" blocker. This proves the fix does not
    reach (let alone parse) the PEM: a valid TLS deploy is not over-rejected FOR THE WRONG
    REASON by this change."""
    pem = "-----BEGIN PRIVATE KEY-----\nMIIBVQ==\n-----END PRIVATE KEY-----\n"
    files = {"requirements.txt": "uvicorn\n", "main.py": _ASGI_APP, "cert.pem": pem}
    assessment, codes, blockers = _assess(
        files, ("uvicorn", "main:app", "--ssl-keyfile", "cert.pem")
    )
    assert assessment is ReleaseAssessment.needs_review
    assert "toolchain_unsupported" in codes, (
        f"uvicorn TLS material is rejected by the grammar ceiling, not our parse; "
        f"saw {sorted(codes)}"
    )
    assert not any("does not parse" in b.message for b in blockers), (
        "the PEM must NOT be ast.parsed by the gunicorn config gate"
    )


@pytest.mark.parametrize(
    "config_body",
    [
        'bind = ["0.0.0.0:8000"]\n',  # valid TOML that also happens to be valid Python
        "%%% not valid python but an opaque hypercorn config\n",  # NOT valid Python
    ],
    ids=["toml-ish", "non-python"],
)
def test_hypercorn_config_is_existence_only_not_parsed(config_body: str) -> None:
    """SERVER-06 scope: the else-branch (hypercorn) ``--config`` has an unknown format
    (hypercorn also accepts TOML), so it is existence-only and must NEVER be ast.parsed.
    Even a body that is NOT valid Python stays a ``candidate`` — assuming Python there would
    over-reject a valid hypercorn deploy."""
    files = {"requirements.txt": "hypercorn\n", "main.py": _ASGI_APP, "hypercorn.toml": config_body}
    assessment, _, _ = _assess(files, ("hypercorn", "main:app", "--config", "hypercorn.toml"))
    assert assessment is ReleaseAssessment.candidate, (
        f"the hypercorn --config must stay existence-only (not parsed); saw {assessment!r}"
    )


def test_detection_is_deterministic_for_the_config_parse_gate() -> None:
    """The parse gate is a pure function of committed contents: two runs of the same
    unparseable-config workspace yield equal typed results."""
    files = dict(_GUNICORN_BASE, **{"gunicorn.conf.py": "%%% not python\n"})
    intent = ReleaseIntent(
        runtime=RuntimeStrategy.python,
        start_cmd=("gunicorn", "--config", "gunicorn.conf.py", "main:app"),
    )
    first = detect_release(files, intent=intent, provenance=Provenance())
    second = detect_release(files, intent=intent, provenance=Provenance())
    assert first == second, "the gunicorn config parse gate must be deterministic across two runs"


# ===========================================================================
# CORRECTION ROUND 2 — SERVER-06 reaches EVERY config-resolution path gunicorn
# uses, not only the explicit ``--config``. gunicorn executes the config file as a
# Python module at startup; it loads that config from (1) explicit ``--config``/``-c``
# and (2) AUTO-DISCOVERY of ``<cwd>/gunicorn.conf.py`` (the ``--chdir`` dir, else the
# repo root) when NO ``--config``/``-c`` is given. A config that does not parse OR whose
# module-scope import is unbacked aborts the master before any worker binds.
# ===========================================================================


def test_autodiscovered_broken_root_config_fails_closed() -> None:
    """DEFECT A: bare ``gunicorn main:app`` with NO ``--config`` still auto-loads a shipped
    root ``gunicorn.conf.py`` (gunicorn ``get_default_config_file()`` = ``os.path.join(cwd,
    'gunicorn.conf.py')``). An unparseable auto-discovered config aborts the master before any
    worker binds → ``needs_review``. Round 1 (explicit-``--config`` only) over-accepted it."""
    files = dict(_GUNICORN_BASE, **{"gunicorn.conf.py": "%%% not python\n"})
    assessment, codes, blockers = _assess(files, ("gunicorn", "main:app"))
    assert assessment is ReleaseAssessment.needs_review, (
        f"an auto-discovered unparseable gunicorn.conf.py must fail closed; saw {assessment!r}"
    )
    assert "entrypoint_unresolved" in codes, f"expected entrypoint_unresolved, saw {sorted(codes)}"
    assert any("does not parse" in b.message for b in blockers), (
        f"the blocker must explain the config does not parse; saw {[b.message for b in blockers]}"
    )


def test_autodiscovered_broken_config_under_chdir_fails_closed() -> None:
    """DEFECT B: ``--chdir app`` makes gunicorn chdir into ``app/`` FIRST, then auto-load
    ``app/gunicorn.conf.py``. A broken config there aborts the master before bind →
    ``needs_review``. The auto-discovery base is the effective ``--chdir`` dir, not the root."""
    files: dict[str, str | bytes] = {
        "requirements.txt": "gunicorn\n",
        "app/main.py": _WSGI_APP,
        "app/gunicorn.conf.py": "%%% not python\n",
    }
    assessment, codes, blockers = _assess(files, ("gunicorn", "--chdir", "app", "main:app"))
    assert assessment is ReleaseAssessment.needs_review, (
        f"an auto-discovered broken config under --chdir must fail closed; saw {assessment!r}"
    )
    assert "entrypoint_unresolved" in codes, f"expected entrypoint_unresolved, saw {sorted(codes)}"
    assert any("does not parse" in b.message for b in blockers), (
        f"the blocker must explain the config does not parse; saw {[b.message for b in blockers]}"
    )


def test_explicit_config_with_missing_import_fails_closed() -> None:
    """DEFECT C (deeper — the Batch-3 import-boundary class): an explicit ``--config`` that
    PARSES cleanly but ``import``s a distribution absent from the exact install plan is a
    ``ModuleNotFoundError`` at exec — gunicorn runs the config module body at startup, so the
    master aborts before any worker binds → ``needs_review``. A clean ``ast.parse`` is not
    runnability; the config's module-scope imports must resolve, exactly like the migrate
    script's."""
    files = dict(
        _GUNICORN_BASE,
        **{"gunicorn.conf.py": "import nonexistent_pkg_xyz\nbind = '0.0.0.0:8000'\n"},
    )
    assessment, codes, blockers = _assess(
        files, ("gunicorn", "--config", "gunicorn.conf.py", "main:app")
    )
    assert assessment is ReleaseAssessment.needs_review, (
        f"a config that imports a missing module must fail closed; saw {assessment!r}"
    )
    assert "entrypoint_unresolved" in codes, f"expected entrypoint_unresolved, saw {sorted(codes)}"
    assert any("import" in b.message for b in blockers), (
        f"the blocker must name the unbacked import; saw {[b.message for b in blockers]}"
    )


def test_autodiscovered_config_with_missing_import_fails_closed() -> None:
    """DEFECT C on the AUTO-DISCOVERY path: an auto-loaded ``gunicorn.conf.py`` that parses
    but imports a missing module aborts the master the same way. The import proof applies to
    the auto-discovered config, not only the explicit one."""
    files = dict(
        _GUNICORN_BASE,
        **{"gunicorn.conf.py": "import nonexistent_pkg_xyz\nworkers = 2\n"},
    )
    assessment, codes, blockers = _assess(files, ("gunicorn", "main:app"))
    assert assessment is ReleaseAssessment.needs_review, (
        f"an auto-discovered config importing a missing module must fail closed; saw {assessment!r}"
    )
    assert "entrypoint_unresolved" in codes, f"expected entrypoint_unresolved, saw {sorted(codes)}"


def test_short_c_flag_broken_config_stays_needs_review() -> None:
    """REGRESSION GUARD (round-1 behavior kept): the grammar normalizes ``-c`` → ``--config``,
    so a broken config passed via ``-c`` must still fail closed to ``needs_review``."""
    files = dict(_GUNICORN_BASE, **{"gunicorn.conf.py": "%%% not python\n"})
    assessment, codes, _ = _assess(files, ("gunicorn", "-c", "gunicorn.conf.py", "main:app"))
    assert assessment is ReleaseAssessment.needs_review, (
        f"a broken -c config must fail closed like --config; saw {assessment!r}"
    )
    assert "entrypoint_unresolved" in codes, f"expected entrypoint_unresolved, saw {sorted(codes)}"


@pytest.mark.parametrize(
    "config_body",
    [
        'bind = "0.0.0.0:8000"\n',  # a plain valid config
        _CANONICAL_CFG,  # THE textbook config — stdlib import must resolve
        "import gunicorn\n",  # the server package is in requirements
        "# only a comment\n",
        "",
    ],
    ids=["plain-valid", "canonical-multiprocessing", "import-gunicorn", "comment-only", "empty"],
)
def test_autodiscovered_valid_config_stays_candidate(config_body: str) -> None:
    """OVER-REJECTION GUARD (auto-discovery): a bare ``gunicorn main:app`` whose auto-loaded
    ``gunicorn.conf.py`` is valid, runnable Python stays a ``candidate``. This ESPECIALLY covers
    the canonical ``import multiprocessing`` config (stdlib MUST resolve) and ``import gunicorn``
    (the server package is installed) — over-rejecting either is a real regression."""
    files = dict(_GUNICORN_BASE, **{"gunicorn.conf.py": config_body})
    assessment, _, _ = _assess(files, ("gunicorn", "main:app"))
    assert assessment is ReleaseAssessment.candidate, (
        f"a bare gunicorn with a valid auto-loaded config must stay a candidate; saw {assessment!r}"
    )


def test_canonical_multiprocessing_config_via_explicit_config_stays_candidate() -> None:
    """OVER-REJECTION GUARD (explicit): the textbook ``import multiprocessing`` config passed
    via ``--config`` resolves (stdlib) and stays a ``candidate`` — the import proof must not
    over-reject the single most common real-world gunicorn.conf.py."""
    files = dict(_GUNICORN_BASE, **{"gunicorn.conf.py": _CANONICAL_CFG})
    assessment, _, _ = _assess(files, ("gunicorn", "--config", "gunicorn.conf.py", "main:app"))
    assert assessment is ReleaseAssessment.candidate, (
        f"the canonical multiprocessing config must stay a candidate; saw {assessment!r}"
    )


def test_broken_config_in_subdir_is_not_autoloaded() -> None:
    """OVER-REJECTION GUARD (scope): gunicorn auto-loads ONLY ``<cwd>/gunicorn.conf.py``, never a
    subdirectory's. A broken ``conf/gunicorn.conf.py`` under a bare ``gunicorn main:app`` (no
    ``--chdir``) is never loaded, so it must stay a ``candidate`` — the fix does not over-reach
    into subdirs."""
    files = dict(_GUNICORN_BASE, **{"conf/gunicorn.conf.py": "%%% not python\n"})
    assessment, _, _ = _assess(files, ("gunicorn", "main:app"))
    assert assessment is ReleaseAssessment.candidate, (
        f"a broken subdir config is not auto-loaded and must stay a candidate; saw {assessment!r}"
    )


def test_broken_root_config_ignored_when_chdir_points_elsewhere() -> None:
    """OVER-REJECTION GUARD (chdir base): with ``--chdir app`` gunicorn auto-loads
    ``app/gunicorn.conf.py`` — NOT the root ``gunicorn.conf.py``. A broken config at the ROOT is
    outside the effective cwd and must not block; the valid config in ``app/`` is what runs."""
    files: dict[str, str | bytes] = {
        "requirements.txt": "gunicorn\n",
        "app/main.py": _WSGI_APP,
        "app/gunicorn.conf.py": 'bind = "0.0.0.0:8000"\n',
        "gunicorn.conf.py": "%%% not python\n",
    }
    assessment, _, _ = _assess(files, ("gunicorn", "--chdir", "app", "main:app"))
    assert assessment is ReleaseAssessment.candidate, (
        f"a broken ROOT config is outside the --chdir cwd and must not block; saw {assessment!r}"
    )


def test_explicit_config_takes_precedence_over_autodiscovery() -> None:
    """SCOPE: when an explicit ``--config`` is given, gunicorn uses IT and ignores
    auto-discovery. A VALID explicit ``--config`` alongside a broken root ``gunicorn.conf.py``
    stays a ``candidate`` (the auto-discovery path must not fire when ``--config`` is present)."""
    files: dict[str, str | bytes] = dict(_GUNICORN_BASE)
    files["prod.py"] = 'bind = "0.0.0.0:8000"\n'
    files["gunicorn.conf.py"] = "%%% not python\n"
    assessment, _, _ = _assess(files, ("gunicorn", "--config", "prod.py", "main:app"))
    assert assessment is ReleaseAssessment.candidate, (
        f"an explicit valid --config must win over a broken auto file; saw {assessment!r}"
    )


def test_autodiscovery_does_not_reach_uvicorn_or_hypercorn() -> None:
    """SCOPE: config auto-discovery is gunicorn-specific. A ``gunicorn.conf.py`` shipped in a
    uvicorn or hypercorn deployment is NOT loaded by those servers, so a broken one must never
    be parsed or block — the result depends only on those servers' own proofs, and here they
    stay ``candidate``."""
    for server, deps in (("uvicorn", "uvicorn\n"), ("hypercorn", "hypercorn\n")):
        files = {
            "requirements.txt": deps,
            "main.py": _ASGI_APP,
            "gunicorn.conf.py": "%%% not python\n",
        }
        assessment, _, _ = _assess(files, (server, "main:app"))
        assert assessment is ReleaseAssessment.candidate, (
            f"{server} must not auto-load gunicorn.conf.py; saw {assessment!r}"
        )


# ===========================================================================
# CORRECTION ROUND 3 — a module-scope ``worker_class = "<non-bundled>"`` config
# setting must be held to the SAME ratified ceiling as the CLI ``--worker-class``.
#
# gunicorn loads the worker class at ``Arbiter.setup`` (``util.load_class``) BEFORE it
# binds a socket, and every non-bundled worker module (``gevent``/``eventlet``/``tornado``/
# a custom dotted path) does ``import <extra>`` at module scope. So a config
# ``worker_class = 'gevent'`` whose distribution is unprovable aborts the master before any
# worker binds — unreachable — exactly like the CLI ``--worker-class gevent`` the ratified
# grammar (``command_grammar._validate_server_option_value``) already rejects as "outside the
# built-in proven set". The config had been MORE PERMISSIVE than the CLI; the config proof now
# reads the SAME ``GUNICORN_BUILTIN_WORKER_CLASSES`` constant so the two surfaces cannot drift.
# This is a MIRROR of the ratified ceiling, NOT a resolve-the-dep: gevent is rejected even WITH
# gevent in requirements (the CLI rejects it unconditionally; a looser config would contradict
# the oracle). Only a DIRECT module-scope string-literal is in scope — a dynamic/aliased value
# cannot be statically resolved and stays a candidate.
# ===========================================================================

_EXPLICIT = ("gunicorn", "--config", "gunicorn.conf.py", "main:app")
_AUTODISCOVERY = ("gunicorn", "main:app")


def _gunicorn_cfg(config_body: str, *, reqs: str = "gunicorn\n") -> dict[str, str]:
    """A gunicorn workspace (WSGI ``main:app`` + install plan) carrying ``config_body``."""
    return {"requirements.txt": reqs, "main.py": _WSGI_APP, "gunicorn.conf.py": config_body}


@pytest.mark.parametrize(
    "worker_value",
    ["gevent", "eventlet", "tornado", "myapp.workers.CustomWorker"],
    ids=["gevent", "eventlet", "tornado", "custom-dotted"],
)
@pytest.mark.parametrize(
    "start_cmd", [_EXPLICIT, _AUTODISCOVERY], ids=["explicit", "autodiscovery"]
)
def test_config_worker_class_outside_proven_set_fails_closed(
    worker_value: str, start_cmd: tuple[str, ...]
) -> None:
    """DEFECT: a module-scope ``worker_class`` naming a non-bundled worker (its distribution
    absent from the install plan) fails closed to ``needs_review`` on BOTH the explicit
    ``--config`` and the auto-discovery paths — gunicorn imports the worker class before it
    binds, so the missing dependency aborts the master and the deployment is unreachable. The
    blocker must name ``worker_class`` and the proven set so a future edit dropping the check is
    caught. Baseline (round-2) over-accepted every one of these as a ``candidate``."""
    files = _gunicorn_cfg(f"worker_class = {worker_value!r}\nworkers = 4\n")
    assessment, codes, blockers = _assess(files, start_cmd)
    assert assessment is ReleaseAssessment.needs_review, (
        f"worker_class={worker_value!r} names a non-bundled worker and must fail closed; "
        f"saw {assessment!r}"
    )
    assert "entrypoint_unresolved" in codes, f"expected entrypoint_unresolved, saw {sorted(codes)}"
    assert any("worker_class" in b.message and "proven set" in b.message for b in blockers), (
        f"the blocker must name worker_class and the proven set; "
        f"saw {[b.message for b in blockers]}"
    )


@pytest.mark.parametrize(
    "start_cmd", [_EXPLICIT, _AUTODISCOVERY], ids=["explicit", "autodiscovery"]
)
def test_config_worker_class_gevent_rejected_even_with_dep_present(
    start_cmd: tuple[str, ...],
) -> None:
    """MIRROR-CLI (the decisive control): ``worker_class = 'gevent'`` fails closed EVEN when
    gevent is in requirements. The ratified CLI ceiling rejects ``--worker-class gevent``
    unconditionally ("outside the built-in proven set"); allowing the config form when the dep
    is present would make the config MORE PERMISSIVE than the CLI — the exact asymmetry this
    round closes. This is a mirror, not a resolve-the-dep, so the dep being present is
    irrelevant."""
    files = _gunicorn_cfg("worker_class = 'gevent'\n", reqs="gunicorn\ngevent\n")
    assessment, codes, _ = _assess(files, start_cmd)
    assert assessment is ReleaseAssessment.needs_review, (
        "worker_class='gevent' must be rejected even WITH gevent in requirements (mirror-CLI, "
        f"not resolve-the-dep); saw {assessment!r}"
    )
    assert "entrypoint_unresolved" in codes, f"expected entrypoint_unresolved, saw {sorted(codes)}"


@pytest.mark.parametrize(
    "worker_value",
    sorted(GUNICORN_BUILTIN_WORKER_CLASSES),
    ids=sorted(GUNICORN_BUILTIN_WORKER_CLASSES),
)
@pytest.mark.parametrize(
    "start_cmd", [_EXPLICIT, _AUTODISCOVERY], ids=["explicit", "autodiscovery"]
)
def test_config_bundled_worker_class_stays_candidate(
    worker_value: str, start_cmd: tuple[str, ...]
) -> None:
    """CONTROL: a bundled worker class (``sync``/``gthread`` — gunicorn ships both, no extra
    distribution) stays a ``candidate`` on both paths. Sourced from the shared
    ``GUNICORN_BUILTIN_WORKER_CLASSES`` so this control tracks the ratified set automatically."""
    files = _gunicorn_cfg(f"worker_class = {worker_value!r}\n")
    assessment, _, _ = _assess(files, start_cmd)
    assert assessment is ReleaseAssessment.candidate, (
        f"the bundled worker_class={worker_value!r} must stay a candidate; saw {assessment!r}"
    )


def test_config_annotated_worker_class_literal_fails_closed() -> None:
    """DEFECT (AnnAssign): an annotated ``worker_class: str = 'gevent'`` is still a module-scope
    string-literal setting gunicorn honors, so it must fail closed exactly like the plain
    ``ast.Assign`` form — the check covers both assignment node shapes."""
    files = _gunicorn_cfg("worker_class: str = 'gevent'\n")
    assessment, codes, _ = _assess(files, _EXPLICIT)
    assert assessment is ReleaseAssessment.needs_review, (
        f"an annotated worker_class literal must fail closed; saw {assessment!r}"
    )
    assert "entrypoint_unresolved" in codes, f"expected entrypoint_unresolved, saw {sorted(codes)}"


@pytest.mark.parametrize(
    "config_body",
    [
        "workers = 4\n",  # no worker_class at all
        "import os\nworker_class = os.environ.get('WC', 'sync')\n",  # dynamic env-driven
        "import os\nworker_class = os.environ['WC']\n",  # dynamic subscript
        "wc = 'gevent'\nworker_class = wc\n",  # aliased Name, not a direct literal
        "worker_class = b'gevent'\n",  # bytes constant, not a str
        # gunicorn honors the LAST module-scope binding: a literal overwritten by a dynamic
        # value has a dynamic effective value → must NOT be over-rejected.
        "import os\nworker_class = 'gevent'\nworker_class = os.environ.get('WC', 'sync')\n",
        # worker_class bound INSIDE a hook function is not a module-scope setting gunicorn reads.
        "def post_fork(server, worker):\n    worker_class = 'gevent'\n",
    ],
    ids=[
        "no-worker-class",
        "env-get",
        "env-subscript",
        "aliased-name",
        "bytes-literal",
        "literal-then-dynamic",
        "function-scope",
    ],
)
def test_config_unresolvable_worker_class_stays_candidate(config_body: str) -> None:
    """CONTROL (the documented dynamic/alias boundary): a ``worker_class`` whose effective value
    cannot be resolved to a module-scope string LITERAL — absent, env-driven, aliased, bytes,
    overwritten-by-dynamic, or bound inside a hook — stays a ``candidate`` (safe
    under-attribution). Only a direct module-scope string-literal is in scope; over-rejecting a
    dynamic config would be a false negative on an ordinary app."""
    files = _gunicorn_cfg(config_body)
    assessment, _, _ = _assess(files, _EXPLICIT)
    assert assessment is ReleaseAssessment.candidate, (
        f"an unresolvable/dynamic worker_class must stay a candidate; saw {assessment!r}"
    )


def test_config_dynamic_then_literal_defect_is_still_caught() -> None:
    """DEFECT (last-binding-wins the OTHER way): a dynamic value OVERWRITTEN by a non-bundled
    literal has that literal as its effective module-scope value, so it must fail closed. Proves
    the resolver tracks the last binding, not merely the presence of any literal."""
    body = "import os\nworker_class = os.environ.get('WC', 'sync')\nworker_class = 'gevent'\n"
    assessment, codes, _ = _assess(_gunicorn_cfg(body), _EXPLICIT)
    assert assessment is ReleaseAssessment.needs_review, (
        f"a dynamic value overwritten by a non-bundled literal must fail closed; saw {assessment!r}"
    )
    assert "entrypoint_unresolved" in codes, f"expected entrypoint_unresolved, saw {sorted(codes)}"


@pytest.mark.parametrize(
    "worker_value,expect_review",
    [
        ("gevent", True),
        ("eventlet", True),
        ("tornado", True),
        ("uvicorn.workers.UvicornWorker", True),
        ("sync", False),
        ("gthread", False),
    ],
    ids=["gevent", "eventlet", "tornado", "uvicorn-worker", "sync", "gthread"],
)
def test_config_and_cli_worker_class_now_agree(worker_value: str, expect_review: bool) -> None:
    """AGREEMENT (the whole point of the round): for the same worker-class VALUE, the config
    ``worker_class = '<v>'`` and the CLI ``--worker-class <v>`` now reach the SAME assessment —
    the asymmetry (config candidate vs CLI needs_review) is closed. Both draw the proven set from
    ``GUNICORN_BUILTIN_WORKER_CLASSES``, so the equality holds by construction, not coincidence.

    NOTE: ``uvicorn.workers.UvicornWorker`` (FastAPI-under-gunicorn) is rejected by the RATIFIED
    CLI ceiling too, so the config agreeing with it is CONSISTENT, not a new over-rejection —
    loosening either surface for ASGI workers is an owner decision to widen the shared set."""
    expected = ReleaseAssessment.needs_review if expect_review else ReleaseAssessment.candidate
    config_files = _gunicorn_cfg(f"worker_class = {worker_value!r}\n")
    config_assessment, _, _ = _assess(config_files, _EXPLICIT)
    cli_assessment, _, _ = _assess(
        _GUNICORN_BASE, ("gunicorn", "--worker-class", worker_value, "main:app")
    )
    assert config_assessment is cli_assessment, (
        f"config and CLI must agree for worker_class={worker_value!r}; "
        f"config={config_assessment!r} cli={cli_assessment!r}"
    )
    assert config_assessment is expected, (
        f"worker_class={worker_value!r} expected {expected!r}; saw {config_assessment!r}"
    )


def test_config_worker_class_gate_is_deterministic() -> None:
    """The worker_class gate is a pure function of committed contents: two runs of the same
    non-bundled-worker_class workspace yield equal typed results."""
    files = _gunicorn_cfg("worker_class = 'gevent'\n")
    intent = ReleaseIntent(runtime=RuntimeStrategy.python, start_cmd=_EXPLICIT)
    first = detect_release(files, intent=intent, provenance=Provenance())
    second = detect_release(files, intent=intent, provenance=Provenance())
    assert first == second, "the worker_class gate must be deterministic across two runs"
