"""Terminal `DetectionResult` builders for every rung of the detection ladder:
the runtime-conflict / fail-closed / intent-vs-container-conflict results, the
AppKit / container-review / imported / not-web results, and the deterministic
detected-service result (database + build-env folding).
"""

from __future__ import annotations

from collections.abc import Mapping

from disco.core.release.spec import (
    EnvScope,
    EnvVarDecl,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseService,
    ResourceDecl,
    RuntimeStrategy,
    SecretClass,
    ServiceRole,
)

from ._blockers import _missing_fields
from ._constants import _APPKIT_FILES, _APPKIT_STATE_PATH, _CONTRACT_FIELDS, _DB_ID, _INGRESS_ID
from ._database import (
    _appkit_db_name,
    _appkit_secret_names,
    _appkit_sqlite_resource,
    _detect_database,
    _sqlite_resource,
)
from ._env import _build_env_decls, _unrepresentable_build_env_blocker
from ._models import (
    DetectionBlocker,
    DetectionResult,
    MissingField,
    Provenance,
    _DbKind,
    _DetectBlocker,
)


def _runtime_conflict_result() -> DetectionResult:
    """Fail closed when TWO different runtime detectors match the same tree.

    A project that carries BOTH a node server start-script AND a python
    web-framework entrypoint declares two competing runtimes; short-circuiting to
    one silently discards the other. Return `needs_review` naming BOTH pieces of
    evidence — carried verbatim in the typed `runtime_conflict` blocker so an owner
    can see WHY the runtimes conflict and declare which one releases the app."""
    node_evidence = "node runtime evidence: package.json declares a server 'start' script"
    python_evidence = (
        "python runtime evidence: a python manifest with a web-framework (fastapi/flask) entrypoint"
    )
    return DetectionResult(
        assessment=ReleaseAssessment.needs_review,
        evidence=(node_evidence, python_evidence),
        reasons=(
            "two different runtime stacks were detected — a node server start "
            "script AND a python web-framework entrypoint; this conflicting "
            "evidence cannot be reconciled automatically, so an owner must declare "
            "which runtime releases this app.",
        ),
        blockers=(
            DetectionBlocker(
                code="runtime_conflict",
                field="runtime",
                message=(
                    "conflicting runtime evidence — " + node_evidence + "; " + python_evidence
                ),
            ),
        ),
    )


def _fail_closed(blocker: _DetectBlocker) -> DetectionResult:
    """Build a `needs_review` result carrying an EXACT typed `DetectionBlocker`
    (and its evidence) from a detector's fail-closed outcome — no ingress, no
    overlay, so the release API returns `self_host:false` with the precise code."""
    return DetectionResult(
        assessment=ReleaseAssessment.needs_review,
        evidence=blocker.evidence,
        reasons=(blocker.message,),
        blockers=(
            DetectionBlocker(code=blocker.code, message=blocker.message, field=blocker.field),
        ),
    )


def _conflict_result(intent: ReleaseIntent, manifest: str) -> DetectionResult:
    # Deferred: `_intent` imports `_fail_closed` from this module at module scope, so
    # this module must not import `_intent` back at module scope (a genuine two-way
    # dependency the original single-file layout never had to name). Resolved lazily —
    # by call time both modules are fully loaded.
    from ._intent import _container_evidence, _intent_evidence

    return DetectionResult(
        assessment=ReleaseAssessment.needs_review,
        evidence=(_intent_evidence(intent), _container_evidence(manifest)),
        reasons=(
            "a typed release intent and an existing container manifest both declare "
            "how to run this app; the two sources conflict and an owner must "
            "reconcile them before release.",
        ),
        missing=(
            MissingField(
                field="release_source",
                detail=f"typed intent vs. existing {manifest} — reconcile the two",
            ),
        ),
    )


def _appkit_result(files: Mapping[str, str | bytes]) -> DetectionResult:
    service = ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.dev_server,
        start_cmd=("npm", "run", "cf:dev"),
        port_env="PORT",
        health_path="/",
    )
    env = tuple(
        EnvVarDecl(
            name=name,
            scope=EnvScope.runtime,
            required=True,
            secret=SecretClass.secret,
        )
        for name in _appkit_secret_names(files)
    )
    resource = _appkit_sqlite_resource(_appkit_db_name(files))
    return DetectionResult(
        assessment=ReleaseAssessment.candidate,
        services=(service,),
        resources=(resource,),
        env=env,
        evidence=tuple(f"AppKit contract file: {name}" for name in _APPKIT_FILES),
        reasons=(
            "AppKit contract files present; a single dev_server ingress on the "
            "workerd dev runtime with a sqlite-class resource persisted at "
            f"{_APPKIT_STATE_PATH}.",
        ),
    )


def _container_review(manifest: str) -> DetectionResult:
    from ._intent import _container_evidence  # deferred: see `_conflict_result`

    return DetectionResult(
        assessment=ReleaseAssessment.needs_review,
        evidence=(_container_evidence(manifest),),
        reasons=(
            f"an existing {manifest} defines the runtime opaquely; the release "
            "contract fields below cannot be verified statically and need owner "
            "review.",
        ),
        missing=_missing_fields(("start_cmd", "port_env", "health_path", "required_env")),
    )


def _detected_result(service: ReleaseService, files: Mapping[str, str | bytes]) -> DetectionResult:
    database = _detect_database(files)
    base_evidence = (f"deterministic detector: {service.runtime.value} ingress service",)
    if database.kind is _DbKind.unknown:
        return DetectionResult(
            assessment=ReleaseAssessment.needs_review,
            services=(service,),
            evidence=base_evidence + database.evidence,
            reasons=(
                f"detected a {service.runtime.value} ingress service, but the project "
                f"references an unrecognized database engine '{database.engine}' that "
                "cannot be auto-provisioned; an owner must declare it.",
            ),
            missing=(
                MissingField(
                    field="resources",
                    detail=f"unrecognized database engine '{database.engine}'",
                ),
            ),
        )
    resources: tuple[ResourceDecl, ...] = ()
    env: tuple[EnvVarDecl, ...] = ()
    evidence = base_evidence
    if database.kind is _DbKind.sqlite:
        resources = (_sqlite_resource(service.id),)
        evidence = base_evidence + database.evidence
        if database.has_url:
            env = (
                EnvVarDecl(
                    name="DATABASE_URL",
                    scope=EnvScope.runtime,
                    required=True,
                    binding=_DB_ID,
                ),
            )
    # Statically discovered BUILD-scope env NAMES (a Vite `import.meta.env.VITE_*`
    # client build var) are conserved in the spec so they lower to a build arg +
    # Dockerfile ARG (§8.1 / §8.2); a name already present (a runtime DB var) is not
    # duplicated. Only added for a build-requiring service — a served-as-is static
    # site or a plain runtime process has no build step to inject them into.
    if service.build_cmd:
        unrepresentable = _unrepresentable_build_env_blocker(files)
        if unrepresentable is not None:
            return _fail_closed(unrepresentable)
        existing = {var.name for var in env}
        env = env + tuple(decl for decl in _build_env_decls(files) if decl.name not in existing)
    return DetectionResult(
        assessment=ReleaseAssessment.candidate,
        services=(service,),
        resources=resources,
        env=env,
        evidence=evidence,
        reasons=(
            f"detected a {service.runtime.value} ingress service from the immutable "
            "project contents.",
        ),
    )


def _imported_review(provenance: Provenance) -> DetectionResult:
    evidence = ("imported project without a typed release intent",) + tuple(provenance.evidence)
    return DetectionResult(
        assessment=ReleaseAssessment.needs_review,
        evidence=evidence,
        reasons=(
            "this project was imported, no release-shaping stack was detected, and "
            "no typed intent was supplied; every release-contract field below must "
            "be declared before release.",
        ),
        missing=_missing_fields(_CONTRACT_FIELDS),
    )


def _not_web_result() -> DetectionResult:
    return DetectionResult(
        assessment=ReleaseAssessment.not_web,
        reasons=(
            "no HTTP entrypoint was found: no package.json server script, no python "
            "web framework, no root index.html, and no container manifest — this "
            "workspace does not describe a web app to release.",
        ),
    )
