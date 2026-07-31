"""Typed `ReleaseIntent` handling: evidence strings, runtime inference, the
install-command derivation shared by the static/started candidates, and the
started (non-static) typed-intent ladder rung.

``_from_intent`` was originally one function covering the no-start-command
branch, every fail-closed blocker check, and the accepted-candidate assembly;
each phase is now a small helper it delegates to.
"""

from __future__ import annotations

from collections.abc import Mapping

from disco.core.release.spec import (
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseService,
    RuntimeStrategy,
    ServiceRole,
)

from ._blockers import (
    _command_grammar_blocker,
    _health_route_present,
    _missing_fields,
    _resource_topology_blocker,
    _toolchain_blocker,
)
from ._constants import (
    _CONVENTIONAL_HEALTH_PATHS,
    _INGRESS_ID,
    _NODE_TOKENS,
    _PY_TOKENS,
    _UNSUPPORTED_NODE_PM,
    _UNSUPPORTED_PY_PM,
)
from ._env import _intent_build_env, _intent_env_result
from ._models import DetectionResult, _DetectBlocker
from ._node import (
    _lockfile_conflict,
    _node_detect,
    _node_has_dependencies,
    _node_install,
    _root_package_json,
    _secret_build_blocker,
    _unsupported_node_pm_declaration,
    _unsupported_pm_blocker,
)
from ._python import _python_detect, _uvicorn_start_contract
from ._results import _fail_closed
from ._text import _paths


def _intent_evidence(intent: ReleaseIntent) -> str:
    if intent.start_cmd:
        return "typed release intent (start_cmd: " + " ".join(intent.start_cmd) + ")"
    return "typed release intent declared"


def _container_evidence(manifest: str) -> str:
    return f"existing container manifest: {manifest}"


def _runtime_from_argv(argv: tuple[str, ...]) -> RuntimeStrategy:
    """Infer a declared process's runtime from its start argv's interpreter token;
    an unrecognized/absent interpreter falls back to `node`, the conservative base
    for a declared long-running process on this platform."""
    if argv:
        head = argv[0].rsplit("/", 1)[-1].lower()
        if head in _NODE_TOKENS:
            return RuntimeStrategy.node
        # `startswith("python")` maps versioned interpreters (`python3.12`,
        # `python3.13`) to python too, not just the bare `python`/`python3` tokens.
        if head.startswith("python") or head in _PY_TOKENS:
            return RuntimeStrategy.python
    return RuntimeStrategy.node


def _intent_install(
    intent: ReleaseIntent,
    files: Mapping[str, str | bytes],
    runtime: RuntimeStrategy,
    *,
    force_node: bool = False,
) -> tuple[tuple[str, ...], str | None, str | None] | _DetectBlocker:
    """The `(install_cmd, package_manager, lockfile)` for a typed-intent candidate, or a
    `_DetectBlocker` when the package manager is ambiguous/unprovisioned (R2 / G04).

    An EXPLICIT `install_cmd` wins (carrying the declared package_manager / lockfile).
    Otherwise a SAFE default is derived ONLY where the manager + manifest are unambiguous:
    a node `package.json` (with declared dependencies, OR `force_node` for a build that
    always installs) → `npm ci` when a lockfile is present else `npm install`; a python
    `requirements.txt` → `pip install -r requirements.txt` (a bare `pyproject.toml` →
    `pip install .`). Derivation first re-applies the SOURCE-path toolchain guards — a
    lockfile disagreement, an unsupported bun/pnpm/yarn manager, an unsupported
    poetry/uv, a build-time `.npmrc` secret — so an unprovisioned/ambiguous tree fails
    closed instead of deriving a broken install. A stack with no manifest (or a
    no-dependency node app) installs nothing."""
    if intent.install_cmd:
        return (intent.install_cmd, intent.package_manager, intent.lockfile)
    if runtime is RuntimeStrategy.node or runtime is RuntimeStrategy.static:
        pkg = _root_package_json(files)
        if pkg is None:
            return ((), intent.package_manager, intent.lockfile)
        if not force_node and not _node_has_dependencies(pkg):
            return ((), intent.package_manager, intent.lockfile)
        conflict = _lockfile_conflict(files)
        if conflict is not None:
            return conflict
        toolchain = _unsupported_pm_blocker(files, _UNSUPPORTED_NODE_PM)
        if toolchain is not None:
            return toolchain
        declared = _unsupported_node_pm_declaration(files, pkg)
        if declared is not None:
            return declared
        build_secret = _secret_build_blocker(files)
        if build_secret is not None:
            return build_secret
        lockfile, manager, install = _node_install(files)
        return (install, manager, lockfile)
    if runtime is RuntimeStrategy.python:
        toolchain = _unsupported_pm_blocker(files, _UNSUPPORTED_PY_PM)
        if toolchain is not None:
            return toolchain
        tree = _paths(files)
        if "requirements.txt" in tree:
            return (("pip", "install", "-r", "requirements.txt"), "pip", None)
        if "pyproject.toml" in tree:
            return (("pip", "install", "."), "pip", None)
        return ((), intent.package_manager, intent.lockfile)
    return ((), intent.package_manager, intent.lockfile)


def _from_intent_no_start(
    intent: ReleaseIntent, files: Mapping[str, str | bytes]
) -> DetectionResult:
    # R2 (G03/G06): a static site (a prebuilt tree, or a build-then-serve bundle) is
    # SERVED, not started, so it has no `start_cmd`. It is recognized when the intent
    # declares a `static` runtime OR an `output_dir` (which only makes sense for a static
    # bundle) — routed to the static shape rather than the no-start needs_review below.
    if intent.runtime is RuntimeStrategy.static or intent.output_dir is not None:
        from ._static import _static_from_intent  # deferred: see `_from_intent`

        return _static_from_intent(intent, files)
    # A release intent with NO start command AND no static shape cannot describe a
    # runnable app: there is no process to launch. Rather than fabricate a runnable
    # candidate (the emitter would default an empty start to `npm start`), fail
    # closed to needs_review naming the missing start command.
    return DetectionResult(
        assessment=ReleaseAssessment.needs_review,
        evidence=("typed release intent declared without a start command",),
        reasons=(
            "the declared release intent has no start command, so there is no "
            "runnable process to release; declare at least a start command "
            "before this app can be a release candidate.",
        ),
        missing=_missing_fields(("start_cmd",)),
    )


def _intent_health_path_blocker(
    intent: ReleaseIntent, files: Mapping[str, str | bytes]
) -> _DetectBlocker | None:
    # A declared CONVENTIONAL health probe (`/healthz`, `/health`, …) must correspond
    # to a real route WHEN the workspace ships a concrete node/python service we can
    # scan: claiming a standard probe the source never registers is a broken contract.
    # A non-conventional owner-specific path is trusted as a declared datum, and an
    # intent over an undetectable stack (no scannable service) is trusted as declared.
    if (
        intent.health_path is not None
        and intent.health_path in _CONVENTIONAL_HEALTH_PATHS
        and (_node_detect(files) is not None or _python_detect(files) is not None)
        and not _health_route_present(files, intent.health_path)
    ):
        return _DetectBlocker(
            code="health_path_unresolved",
            message=(
                f"the declared health path {intent.health_path!r} is a conventional "
                "probe but does not correspond to any route in the detected service's "
                "source; the health check would never pass. Declare a health path the "
                "app actually serves."
            ),
            field="health_path",
            evidence=(f"health evidence: no route for conventional {intent.health_path!r}",),
        )
    return None


def _from_intent_blockers(
    intent: ReleaseIntent, files: Mapping[str, str | bytes]
) -> tuple[str, ...] | None | DetectionResult:
    """Run every fail-closed check gating a started (non-static) typed-intent
    candidate. Returns a `DetectionResult` to return immediately on failure, else
    the uvicorn-normalized start argv (or `None` for a non-uvicorn command)."""
    # C9-04: a RECOGNIZABLY-uvicorn start is adjudicated by its own supported-shape/arity
    # contract FIRST, so an unsupported uvicorn option gets the typed
    # `uvicorn_start_incoherent` blocker rather than the older generic grammar's
    # `toolchain_unsupported` (independent verification demonstrated the ordering defect:
    # e.g. `uvicorn main:app --uds app.sock`). This runs before the generic toolchain /
    # command-grammar blockers, which then only see NON-uvicorn or already-accepted
    # uvicorn commands. The accepted argv is re-derived (and normalized) below.
    start_contract = _uvicorn_start_contract(intent.start_cmd, intent.port_env)
    if isinstance(start_contract, _DetectBlocker):
        return _fail_closed(start_contract)
    # The declared start command must run on the neutral base image (a supported
    # runner + any pip-package start executable declared as a dependency), and must not
    # name an unprovisioned package manager (pnpm/yarn/bun/…) in start/build/install.
    toolchain = _toolchain_blocker(intent, files)
    if toolchain is not None:
        return _fail_closed(toolchain)
    # WO-C5 #2 F1: the declared start/build argv must parse through the SAME runtime
    # command grammar the release_declare tool enforces, so a raw sidecar cannot bake a
    # secret CLI value (or an unsupported/opaque command) into the emitted bundle.
    grammar = _command_grammar_blocker(intent)
    if grammar is not None:
        return _fail_closed(grammar)
    # R3 (G07): a declared resource whose persistent_path is a filesystem-ROOT file cannot
    # be volume-backed (its mount dir is `/`, which would shadow the container root), so
    # fail closed rather than emit a self-host bundle whose state silently does not persist.
    topology = _resource_topology_blocker(intent.resources)
    if topology is not None:
        return _fail_closed(topology)
    health_blocker = _intent_health_path_blocker(intent, files)
    if health_blocker is not None:
        return _fail_closed(health_blocker)
    return start_contract


def _from_intent_candidate(
    intent: ReleaseIntent,
    files: Mapping[str, str | bytes],
    runtime: RuntimeStrategy,
    start_contract: tuple[str, ...] | None,
) -> DetectionResult:
    # R2 (G04): a runtime dependency install no longer depends on a build step — an
    # interpreted candidate (declared deps, no build) installs them, or fails closed.
    install_outcome = _intent_install(intent, files, runtime)
    if isinstance(install_outcome, _DetectBlocker):
        return _fail_closed(install_outcome)
    install_cmd, package_manager, lockfile = install_outcome
    env = _intent_env_result(intent)
    if isinstance(env, _DetectBlocker):
        return _fail_closed(env)
    env = _intent_build_env(env, intent, files)
    if isinstance(env, _DetectBlocker):
        return _fail_closed(env)
    # The uvicorn contract was already adjudicated above (a blocker returned early); here
    # ``start_contract`` is the accepted+normalized argv (uvicorn) or ``None`` (non-uvicorn,
    # left verbatim).
    service = ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=runtime,
        package_manager=package_manager,
        lockfile=lockfile,
        install_cmd=install_cmd,
        build_cmd=intent.build_cmd,
        start_cmd=start_contract if start_contract is not None else intent.start_cmd,
        port_env=intent.port_env,
        health_path=intent.health_path,
    )
    return DetectionResult(
        assessment=ReleaseAssessment.candidate,
        services=(service,),
        resources=intent.resources,
        env=env,
        evidence=(_intent_evidence(intent),),
        reasons=(
            "release shape declared by a typed release intent "
            "(build/start argv, port, health path and env names supplied).",
        ),
    )


def _from_intent(intent: ReleaseIntent, files: Mapping[str, str | bytes]) -> DetectionResult:
    # Deferred: `_static` imports `_intent_install`/`_intent_evidence` from this module
    # at module scope, so this module must not import `_static` back at module scope (a
    # genuine two-way dependency the original single-file layout never had to name).
    # Resolved lazily inside `_from_intent_no_start` — by call time both modules are
    # fully loaded.
    if not intent.start_cmd:
        return _from_intent_no_start(intent, files)
    blocked_or_contract = _from_intent_blockers(intent, files)
    if isinstance(blocked_or_contract, DetectionResult):
        return blocked_or_contract
    start_contract = blocked_or_contract
    # R2 (G03): the runtime strategy is the declared one, else inferred from the start
    # argv's interpreter token.
    runtime = intent.runtime if intent.runtime is not None else _runtime_from_argv(intent.start_cmd)
    return _from_intent_candidate(intent, files, runtime, start_contract)
