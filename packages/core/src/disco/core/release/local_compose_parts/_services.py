"""Resource/env lowering + per-service and migrate compose blocks.

Extracted from ``local_compose.py`` to reduce module size; the public facade
re-imports ``_ingress_service`` / ``_compose_document`` unchanged (indirectly,
via ``emit_local_compose``).

Imports ``_build_block`` from the sibling ``_dockerfile`` module at module
scope: that module's own (reverse) need for this module's ``_build_arg_names``
is a LAZY, function-body import there specifically to make this top-level
import here safe (see ``_dockerfile._arg_lines``'s docstring).
"""

from __future__ import annotations

from disco.core.release.spec import (
    EnvScope,
    EnvVarDecl,
    ReleaseService,
    ReleaseSpec,
    ResourceDecl,
    RuntimeStrategy,
    SecretClass,
    ServiceRole,
)

from ._dockerfile import _build_block
from ._shell import _CONTAINER_PORT, _HOST_PORT_VAR, _exec_or_shell, _required_guard
from ._yaml import _Yaml

_MIGRATE_SERVICE = "migrate"
_MIGRATE_SERVICE_FALLBACK = "release_migrate"


def _mount_dir(resource: ResourceDecl) -> str:
    """The directory a resource's named volume mounts at — the parent dir of its
    persistent path (a SQLite `file:/data/app.db` → `/data`). Delegates to the
    SHARED `spec.local_mount_target` so the emitter and the schema's
    duplicate-mount-target validator can never disagree (WO-C7).

    Resolved through the parent `local_compose` module's own binding (rather
    than this module's own `local_mount_target` import) so the emitter's
    mount-target resolution is always the SAME live binding external callers
    observe on `disco.core.release.local_compose.local_mount_target` — the
    public facade attribute the emitter has always been driven through."""
    from disco.core.release import local_compose

    return local_compose.local_mount_target(resource)


def _resource_volume_names(spec: ReleaseSpec) -> dict[str, str]:
    """Map each resource id to a UNIQUE compose volume name, ORDER-INDEPENDENTLY
    (WO-C7 §11.9).

    A named volume per distinct persistent path is essential: two resources that
    (legally, per the schema) declare the SAME `local.volume` name would otherwise
    collapse to one volume mounted at two locations and silently clobber each other's
    state. On a collision the declared name is disambiguated deterministically by
    suffixing.

    The assignment is a pure function of the resource SET, not its declaration
    order: resources are processed in `persistent_path` order (a total order — the
    schema rejects duplicate persistent paths), so a given persistent path always
    receives the SAME volume identity regardless of the order the resources were
    declared in. A deployed service therefore never binds to a different volume just
    because the spec listed its resources in another order."""
    assigned: dict[str, str] = {}
    used: set[str] = set()
    for resource in sorted(spec.resources, key=lambda r: r.persistent_path):
        base = resource.profiles.local.volume
        name = base
        suffix = 2
        while name in used:
            name = f"{base}-{suffix}"
            suffix += 1
        used.add(name)
        assigned[resource.id] = name
    return assigned


def _resource_consumers(spec: ReleaseSpec, resource_id: str) -> tuple[str, ...]:
    """The consumer service ids of a resource (referential integrity guaranteed by
    the `ReleaseSpec` validators). Empty tuple if the id is unknown (unreachable via
    a validated spec)."""
    for resource in spec.resources:
        if resource.id == resource_id:
            return resource.consumers
    return ()


def _service_mounts(spec: ReleaseSpec, service_id: str, volume_names: dict[str, str]) -> list[str]:
    """The `<volume>:<dir>` mount entries for ONE service — ONLY the resources that
    declare it a consumer (WO-C7 §11.1/§11.2). Sorted by mount target (a total order
    — the schema rejects duplicate targets) for a deterministic, order-independent
    emission."""
    entries: list[tuple[str, str]] = []
    for resource in spec.resources:
        if service_id in resource.consumers:
            entries.append((_mount_dir(resource), volume_names[resource.id]))
    entries.sort()
    return [f"{volume}:{target}" for target, volume in entries]


def _bound_literal(spec: ReleaseSpec, binding: str) -> str:
    """The concrete local URL for a resource an env var binds to (the value the
    compose file provides, e.g. `file:/data/app.db`)."""
    for resource in spec.resources:
        if resource.id == binding:
            return resource.profiles.local.url
    # Referential integrity is guaranteed by ReleaseSpec validators, so an unbound
    # binding cannot reach here through the normal (validated) path.
    return ""


def _resolved_runtime_value(spec: ReleaseSpec, var: EnvVarDecl) -> str:
    """The value a runtime env var resolves to in a service `environment`: a
    resource-bound var as its literal local URL, a required host var as a
    `${VAR:?...}` guard, an optional host var as a bare `${VAR}`."""
    if var.binding is not None:
        return _bound_literal(spec, var.binding)
    if var.required:
        return _required_guard(var.name)
    return f"${{{var.name}}}"


def _env_reaches_service(
    spec: ReleaseSpec, var: EnvVarDecl, service: ReleaseService, *, single_service: bool
) -> bool:
    """Whether a RUNTIME env var is injected into `service` under the WO-C7 consumer
    topology:

    * a BOUND var reaches exactly the CONSUMERS of the resource it binds;
    * an unbound var with an explicit `consumers` scope reaches exactly those services;
    * an unbound var with NO declared consumers reaches the sole service of a
      SINGLE-service spec (the documented default) and NO service otherwise — a
      multi-service spec with such a var is rejected at validation, so this never
      fans out.
    """
    if var.binding is not None:
        return service.id in _resource_consumers(spec, var.binding)
    if var.consumers is not None:
        return service.id in var.consumers
    return single_service


def _service_runtime_env(spec: ReleaseSpec, service: ReleaseService) -> dict[str, str]:
    """The RUNTIME env map for ONE service — only the vars the consumer topology
    routes to it (WO-C7). Sorted by NAME for a deterministic emission."""
    single_service = len(spec.services) == 1
    values: dict[str, str] = {}
    for var in spec.env:
        if var.scope is not EnvScope.runtime:
            continue
        if not _env_reaches_service(spec, var, service, single_service=single_service):
            continue
        values[var.name] = _resolved_runtime_value(spec, var)
    return {name: values[name] for name in sorted(values)}


def _build_args(spec: ReleaseSpec) -> dict[str, str]:
    """The build-scope env map folded into a service's Compose ``build.args`` — PUBLIC
    build vars only. A required public build var renders a ``${NAME:?...}`` guard, an
    optional one a bare ``${NAME}``. A build-scope SECRET var is NEVER emitted here: the
    detector fails such a workspace closed at discovery and ``validate_release`` refuses
    the spec (``secret_build_env_unsupported``), and this public-only filter is the
    emit-layer belt to that gate — so a secret can never be smuggled into ``build.args``
    (which would bake it into image build history). Sorted by NAME for deterministic
    emission, and symmetric with ``_build_arg_names`` (the Dockerfile ``ARG`` set)."""
    values: dict[str, str] = {}
    for var in spec.env:
        if var.scope is not EnvScope.build or var.secret is not SecretClass.public:
            continue
        values[var.name] = _required_guard(var.name) if var.required else f"${{{var.name}}}"
    return {name: values[name] for name in sorted(values)}


def _build_arg_names(spec: ReleaseSpec) -> list[str]:
    """The build-scope PUBLIC env NAMES to declare as Dockerfile ``ARG``s (visible to
    the install/build ``RUN``). A build-scope SECRET var never reaches emission — the
    detector fails closed on a build-time secret (`secret_build_env_unsupported`) — so
    a build ``ARG`` is ONLY ever a PUBLIC build var, never a secret smuggled into
    image history via a plain ``ARG``. Sorted for a deterministic emission."""
    return sorted(
        var.name
        for var in spec.env
        if var.scope is EnvScope.build and var.secret is SecretClass.public
    )


def _migrate_env(
    spec: ReleaseSpec, ingress: ReleaseService, migrated: ResourceDecl | None
) -> dict[str, str]:
    """The env the one-shot migrate service reads: ONLY the resource-bound value(s)
    (the DB URL) of the resource it migrates — never a host secret guard, and never
    an UNRELATED resource's binding (WO-C7 §11.3). A SERVICE-level ingress migration
    (`migrated is None`) reads the bound values of the resources the ingress
    consumes. Sorted by NAME for a deterministic emission."""
    values: dict[str, str] = {}
    for var in spec.env:
        if var.scope is not EnvScope.runtime or var.binding is None:
            continue
        if migrated is not None:
            if var.binding == migrated.id:
                values[var.name] = _bound_literal(spec, var.binding)
        elif ingress.id in _resource_consumers(spec, var.binding):
            values[var.name] = _bound_literal(spec, var.binding)
    return {name: values[name] for name in sorted(values)}


# ---- migrate command discovery ------------------------------------------------


def _migrate_source(
    spec: ReleaseSpec, ingress: ReleaseService
) -> tuple[tuple[str, ...], ResourceDecl | None]:
    """The migrate command for the one-shot service AND the specific resource it
    migrates, or `((), None)` if none is declared.

    Precedence: an explicit INGRESS `migrate_cmd` is a SERVICE-level migration not
    tied to a single resource (`resource=None`); otherwise the migration belongs to
    the resource that declares a `migrate_cmd`. Among such resources the one with the
    smallest `persistent_path` is chosen, so the pick is ORDER-INDEPENDENT (WO-C7
    §11.9) — normally there is exactly one migrate-bearing resource."""
    # O2 (carried, out of C7 scope — baseline behavior, no isolation impact): a
    # NON-ingress service's own `migrate_cmd` is intentionally IGNORED here; only the
    # ingress-level command and resource-level commands drive the one-shot migrate
    # service. A worker that declares its own migrate step is not wired a migration.
    if ingress.migrate_cmd:
        return ingress.migrate_cmd, None
    for resource in sorted(spec.resources, key=lambda r: r.persistent_path):
        if resource.migrate_cmd:
            return resource.migrate_cmd, resource
    return (), None


def _migrate_mounts(
    spec: ReleaseSpec,
    ingress: ReleaseService,
    migrated: ResourceDecl | None,
    volume_names: dict[str, str],
) -> list[str]:
    """The mounts for the one-shot migrate service — ONLY the resource it migrates
    (WO-C7 §11.3), never an unrelated resource. A SERVICE-level ingress migration
    (`migrated is None`) mounts the resources the INGRESS consumes (it runs as the
    ingress image and legitimately owns that state)."""
    if migrated is not None:
        return [f"{volume_names[migrated.id]}:{_mount_dir(migrated)}"]
    return _service_mounts(spec, ingress.id, volume_names)


def _migrate_service_name(spec: ReleaseSpec) -> str:
    taken = {service.id for service in spec.services}
    if _MIGRATE_SERVICE not in taken:
        return _MIGRATE_SERVICE
    return _MIGRATE_SERVICE_FALLBACK


# ---- healthcheck --------------------------------------------------------------


def _health_test(service: ReleaseService) -> list[str]:
    path = service.health_path or "/"
    url = f"http://127.0.0.1:{_CONTAINER_PORT}{path}"
    if service.runtime is RuntimeStrategy.node:
        script = (
            "require('http').get("
            f"'{url}',"
            "r=>process.exit(r.statusCode<400?0:1)"
            ").on('error',()=>process.exit(1))"
        )
        return ["CMD", "node", "-e", script]
    if service.runtime is RuntimeStrategy.python:
        script = (
            "import urllib.request,sys; "
            f"sys.exit(0 if urllib.request.urlopen('{url}',timeout=3).status<400 else 1)"
        )
        return ["CMD", "python", "-c", script]
    if service.runtime is RuntimeStrategy.static:
        return ["CMD", "wget", "-q", "-O", "-", url]
    # container: base image tooling is unknown — best-effort wget/curl fallback.
    return ["CMD-SHELL", f"wget -q -O - {url} >/dev/null 2>&1 || curl -fsS {url} >/dev/null 2>&1"]


def _healthcheck_block(service: ReleaseService) -> dict[str, _Yaml]:
    return {
        "test": _health_test(service),
        "interval": "10s",
        "timeout": "3s",
        "retries": 5,
        "start_period": "20s",
    }


# ---- per-service compose block ------------------------------------------------


def _service_block(
    spec: ReleaseSpec,
    service: ReleaseService,
    *,
    multi: bool,
    migrate_name: str | None,
    volume_names: dict[str, str],
) -> dict[str, _Yaml]:
    is_ingress = service.role is ServiceRole.ingress
    block: dict[str, _Yaml] = {
        "build": _build_block(service, multi=multi),
        "restart": "unless-stopped",
    }

    # Every declared dependency is emitted: the one-shot migrate ordering (on the
    # ingress) AND every generic `depends_on` relationship the service declares. A
    # generic dependency uses `service_started` (start-order only — the dependency is
    # a long-running service, not a one-shot to wait on for completion).
    depends: dict[str, _Yaml] = {}
    if is_ingress and migrate_name is not None:
        depends[migrate_name] = {"condition": "service_completed_successfully"}
    for dep in service.depends_on:
        depends[dep] = {"condition": "service_started"}
    if depends:
        block["depends_on"] = {name: depends[name] for name in sorted(depends)}

    # WO-C7: inject ONLY the runtime env the consumer topology routes to THIS service
    # (a bound var reaches its resource's consumers; an unbound var reaches its
    # declared consumers, or the sole ingress of a single-service spec).
    environment: dict[str, _Yaml] = {service.port_env: str(_CONTAINER_PORT)}
    for name, value in _service_runtime_env(spec, service).items():
        environment[name] = value
    block["environment"] = {name: environment[name] for name in sorted(environment)}

    build_args = _build_args(spec)
    if build_args:
        # Fold PUBLIC build-scope env into build.args (guarded/bare by rule), symmetric
        # with the Dockerfile `ARG`s (`_arg_lines`). A build-scope SECRET var is filtered
        # out here AND never reaches emission (the detector fails it closed and
        # `validate_release` refuses it with `secret_build_env_unsupported`, §8.4), so a
        # secret is never smuggled into build.args or a plain build ARG.
        build = block["build"]
        assert isinstance(build, dict)
        build["args"] = {name: build_args[name] for name in sorted(build_args)}

    if is_ingress:
        block["ports"] = [f"127.0.0.1:${{{_HOST_PORT_VAR}:-{_CONTAINER_PORT}}}:{_CONTAINER_PORT}"]

    # WO-C7: mount ONLY the resources that declare this service a consumer.
    mounts = _service_mounts(spec, service.id, volume_names)
    if mounts:
        block["volumes"] = mounts

    if is_ingress or service.health_path is not None:
        block["healthcheck"] = _healthcheck_block(service)

    return block


def _migrate_block(
    ingress: ReleaseService,
    *,
    multi: bool,
    argv: tuple[str, ...],
    env: dict[str, str],
    mounts: list[str],
) -> dict[str, _Yaml]:
    block: dict[str, _Yaml] = {
        "build": _build_block(ingress, multi=multi),
        "restart": "no",
        "command": _exec_or_shell(argv),
    }
    if env:
        block["environment"] = {name: env[name] for name in sorted(env)}
    if mounts:
        block["volumes"] = list(mounts)
    return block


# ---- compose document ---------------------------------------------------------


def _ingress_service(spec: ReleaseSpec) -> ReleaseService:
    for service in spec.services:
        if service.role is ServiceRole.ingress:
            return service
    raise ValueError("ReleaseSpec has no ingress service — cannot emit a local compose bundle")


def _compose_document(spec: ReleaseSpec) -> dict[str, _Yaml]:
    ingress = _ingress_service(spec)
    services = list(spec.services)
    multi = len(services) > 1
    volume_names = _resource_volume_names(spec)

    migrate_argv, migrated_resource = _migrate_source(spec, ingress)
    migrate_name = _migrate_service_name(spec) if migrate_argv else None

    service_blocks: dict[str, _Yaml] = {}
    for service in sorted(services, key=lambda s: s.id):
        service_blocks[service.id] = _service_block(
            spec, service, multi=multi, migrate_name=migrate_name, volume_names=volume_names
        )
    if migrate_name is not None:
        service_blocks[migrate_name] = _migrate_block(
            ingress,
            multi=multi,
            argv=migrate_argv,
            env=_migrate_env(spec, ingress, migrated_resource),
            mounts=_migrate_mounts(spec, ingress, migrated_resource, volume_names),
        )

    document: dict[str, _Yaml] = {"services": service_blocks}
    if spec.resources:
        # Exactly one named volume per accepted resource persistent path (WO-C7
        # §11.5), emitted in NAME order so the document is order-independent.
        volumes: dict[str, _Yaml] = {name: {} for name in sorted(set(volume_names.values()))}
        document["volumes"] = volumes
    return document
