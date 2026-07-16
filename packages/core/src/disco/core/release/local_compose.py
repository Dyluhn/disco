"""WO-4 — the LocalComposeAdapter: a `ReleaseSpec` → portable self-host bundle.

`emit_local_compose(spec)` lowers the neutral, secret-free `ReleaseSpec` into the
complete overlay a user runs with `docker compose up -d --build`: a `compose.yaml`,
a per-service `Dockerfile` (or `selfhost/<service>.Dockerfile` for a multi-service
release), a `.dockerignore`, a NAMES-ONLY `.env.example`, a `SELFHOST.md`, and
`release.json` (the canonical serialized spec — the runtime contract the bundle
was generated from).

Two guarantees carry over from the spec and are load-bearing here:

* SECRET-FREE — the overlay is a pure function of the spec, and the spec records
  env-var NAMES only. Required runtime vars render as `${VAR:?Set VAR — see
  .env.example}` (a compose interpolation guard that fails loudly if unset);
  optional vars render as a bare `${VAR}` (no guard). No secret VALUE is ever
  emitted — the host injects material out-of-band at `compose up` time.
* DETERMINISTIC — the YAML is emitted BY HAND (the same discipline
  `appkit/generator.py` uses to hand-emit TOML/JSON) so the same spec always
  produces byte-identical bytes, with no YAML runtime dependency for emission.
  A tiny block-style writer (`_emit_yaml`) double-quotes every string scalar so
  the output round-trips through a real YAML parser unambiguously.

Compose shape:

* one published port on the ingress service, bound to loopback only:
  `127.0.0.1:${HOST_PORT:-8080}:8080` (NO host bind mounts anywhere);
* a `healthcheck:` on every ingress / health-path service, hitting its
  `health_path` with an in-image probe (node/python/busybox tooling);
* a one-shot `migrate` service (`restart: "no"`) when the release declares a
  migrate command, with the app waiting on it via
  `depends_on: {migrate: {condition: service_completed_successfully}}`;
* a named volume per persistent resource path (a SQLite resource →
  `DATABASE_URL=file:/data/app.db` + a named volume mounted at `/data`);
* `restart: unless-stopped` on every long-running service.

Runtime strategies covered here: `node`, `python`, `static`, `container`. A
`container` service ships its OWN Dockerfile (referenced via `build.dockerfile`),
so this adapter never emits — and never overwrites — one for it. `dev_server`
(AppKit) takes a dedicated INTERIM path (`_emit_dev_server_overlay`): the app runs
on the workerd dev runtime (`wrangler dev`) with a persistent, volume-backed local
D1 and a container entrypoint that writes the dev-secrets file at start, so no
secret ever lands in the exported bundle. A compose-native AppKit self-host (a
standalone server image, no dev runtime) lands with AppKit v2.

Path-collision safety: `emit_local_compose` never overwrites — the caller who has
an existing workspace tree passes it to `emit_local_compose_checked(spec,
existing_paths)`, which returns a typed `OverlayConflict` (naming every colliding
path) instead of an overlay when any overlay path already exists.

Layering: `disco.core` is the leaf package — this imports ONLY the stdlib +
pydantic + the sibling `.spec` module. No `disco.tools.*`, no IO, no clock/random.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from disco.core.release.command_grammar import (
    flag_env_ref,
    looks_like_credential_literal,
    public_bind_env_ref,
    whole_env_ref,
)
from disco.core.release.spec import (
    EnvScope,
    EnvVarDecl,
    ReleaseService,
    ReleaseSpec,
    ResourceDecl,
    ResourceKind,
    RuntimeStrategy,
    SecretClass,
    ServiceRole,
    local_mount_target,
    serialize_release_spec,
)
from pydantic import BaseModel, ConfigDict

# ---- emission constants -------------------------------------------------------
#
# The single fixed container port. The spec carries the port-env NAME (the `$PORT`
# contract), never a number; the adapter owns the concrete in-container port and
# injects it as `<port_env>=8080` so the app binds it, then publishes it on the
# host as `${HOST_PORT:-8080}`.
_CONTAINER_PORT = 8080
_HOST_PORT_VAR = "HOST_PORT"

_MIGRATE_SERVICE = "migrate"
_MIGRATE_SERVICE_FALLBACK = "release_migrate"
_SELFHOST_DIR = "selfhost"

# Base images, pinned to a major so the bundle is reproducible without chasing
# `latest`. Neutral, widely-mirrored images (no vendor lock-in).
_NODE_IMAGE = "node:22-bookworm-slim"
_PYTHON_IMAGE = "python:3.13-slim-bookworm"
_STATIC_IMAGE = "busybox:1.37"

# AppKit interim local-run (`dev_server`): the app is served by the workerd dev
# runtime (`wrangler dev`) with a persistent, volume-backed local D1. The init
# service applies `schema.sql`; the app binds all interfaces on this port. The
# container entrypoint materializes the workerd dev-secrets file at START from the
# host-injected secret env, so no secret ever lands in the exported bundle.
_DEV_SERVER_IMAGE = "node:22-slim"
_DEV_SERVER_PORT = 8787
_DEV_INIT_SERVICE = "init"
_DEV_INIT_SERVICE_FALLBACK = "release_init"
_ENTRYPOINT_PATH = "/usr/local/bin/disco-entrypoint.sh"
# The workerd dev-secrets file the entrypoint writes at container start. This is
# the ONLY place the bundle references it — it is created at runtime inside the
# container, never emitted into the exported tree.
_DEV_VARS_FILE = "/app/.dev.vars"

_DOCKERFILE_HEADER = (
    "# syntax=docker/dockerfile:1\n"
    "# Generated by Disco (local_compose) — portable, secret-free self-host image.\n"
)

# ---- overlay path names -------------------------------------------------------
COMPOSE_PATH = "compose.yaml"
DOCKERFILE_PATH = "Dockerfile"
DOCKERIGNORE_PATH = ".dockerignore"
ENV_EXAMPLE_PATH = ".env.example"
SELFHOST_DOC_PATH = "SELFHOST.md"
RELEASE_JSON_PATH = "release.json"


# ---- the conflict result ------------------------------------------------------


class OverlayConflict(BaseModel):
    """Returned by `emit_local_compose_checked` when one or more overlay paths
    already exist in the caller-supplied workspace tree.

    A DATA result, not an exception: emission never silently overwrites. `paths`
    names every overlay path that collided (sorted, deduplicated), so the caller
    can report exactly what would have been clobbered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    paths: tuple[str, ...]

    @property
    def message(self) -> str:
        joined = ", ".join(self.paths)
        return (
            "emit_local_compose would overwrite existing workspace path(s): "
            f"{joined}. Refusing to clobber; remove/rename them or emit into a "
            "clean directory."
        )


# ---- hand-rolled block-style YAML emitter -------------------------------------
#
# A minimal, DETERMINISTIC YAML writer over the closed value shape the compose
# document uses: nested mappings, sequences of scalars, and string/int scalars.
# Every string scalar is double-quoted (with `\`/`"`/control-char escaping) so a
# value like `"no"`, `"127.0.0.1:${HOST_PORT:-8080}:8080"`, or a `${VAR:?...}`
# guard is never re-interpreted by a YAML parser as a bool/int/flow-collection.

type _Yaml = str | int | list[str] | dict[str, _Yaml]


def _scalar(value: str | int) -> str:
    if isinstance(value, bool):  # pragma: no cover - bool is not used, guard for safety
        raise TypeError("bool is not a supported YAML scalar here")
    if isinstance(value, int):
        return str(value)
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _emit_mapping(mapping: dict[str, _Yaml], indent: int) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    for key, value in mapping.items():
        if isinstance(value, dict):
            if not value:
                lines.append(f"{pad}{key}: {{}}")
            else:
                lines.append(f"{pad}{key}:")
                lines.extend(_emit_mapping(value, indent + 1))
        elif isinstance(value, list):
            if not value:
                lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}:")
                for item in value:
                    lines.append(f"{pad}  - {_scalar(item)}")
        else:
            lines.append(f"{pad}{key}: {_scalar(value)}")
    return lines


def _emit_yaml(document: dict[str, _Yaml]) -> str:
    return "\n".join(_emit_mapping(document, 0)) + "\n"


# ---- small shared helpers -----------------------------------------------------


def _norm_root(root: str) -> str:
    stripped = root.strip()
    if stripped.startswith("./"):
        stripped = stripped[2:]
    stripped = stripped.rstrip("/")
    return stripped


def _is_subroot(root: str) -> bool:
    return _norm_root(root) not in ("", ".")


def _required_guard(name: str) -> str:
    # A compose interpolation guard: unset -> `compose` errors with our message.
    return f"${{{name}:?Set {name} — see .env.example}}"


def _shell_quote_literal(token: str) -> str:
    """POSIX single-quote a literal so EVERY character in it is inert to the shell
    (no metacharacter, `$`, quote, or space is ever interpreted). A single quote
    inside the token is closed, escaped, and reopened (`'\\''`)."""
    return "'" + token.replace("'", "'\\''") + "'"


def _token_expands(token: str) -> bool:
    """Whether a token carries an env reference the shell must resolve — a WHOLE
    `${NAME}` reference OR the `--flag=${NAME}` value form (WO-C5 #2 F4). A token
    without one is a pure literal that needs no shell."""
    return (
        whole_env_ref(token) is not None
        or flag_env_ref(token) is not None
        or public_bind_env_ref(token) is not None
    )


def _shell_expand(argv: tuple[str, ...]) -> str:
    """Render an argv as a `sh -c` program body that is SAFE BY CONSTRUCTION (WO-C5
    §9.10). A token is expandable ONLY as a validated env reference: a WHOLE `${NAME}`
    token renders as a double-quoted `"${NAME}"`; a `--flag=${NAME}` token renders as
    the shell concatenation `'--flag='"${NAME}"` (the literal flag prefix single-quoted,
    the reference double-quoted) so `${NAME}` resolves at runtime instead of shipping
    broken (F4). EVERY other token is single-quoted as an inert literal. There is no
    `if "$" in token: paste it unquoted` path — a `$` can reach the shell source solely
    as a validated reference, never as a substitution/partial interpolation."""
    parts: list[str] = []
    for token in argv:
        name = whole_env_ref(token)
        if name is not None:
            parts.append(f'"${{{name}}}"')
            continue
        flag = flag_env_ref(token)
        if flag is not None:
            prefix, ref_name = flag
            parts.append(_shell_quote_literal(prefix) + f'"${{{ref_name}}}"')
            continue
        bind_name = public_bind_env_ref(token)
        if bind_name is not None:
            # Preserve argv as ONE host:port value while expanding only the validated
            # provider-owned port reference. The host prefix is inert literal data.
            parts.append(_shell_quote_literal("0.0.0.0:") + f'"${{{bind_name}}}"')
            continue
        parts.append(_shell_quote_literal(token))
    return " ".join(parts)


def _exec_or_shell(argv: tuple[str, ...]) -> list[str]:
    """A container command as an exec array. When NO token carries an env reference,
    keep the pure exec form (no shell involved). When a token DOES (a whole `${NAME}`
    or a `--flag=${NAME}`), wrap in `sh -c 'exec ...'` whose body is built by
    `_shell_expand` — literals single-quoted, only the validated reference expanded —
    so a reference is never shipped as an inert exec-array literal (F4)."""
    if not any(_token_expands(token) for token in argv):
        return list(argv)
    return ["sh", "-c", "exec " + _shell_expand(argv)]


def _run_line(argv: tuple[str, ...]) -> str:
    return "RUN " + json.dumps(list(argv), ensure_ascii=False)


def _cmd_line(argv: tuple[str, ...]) -> str:
    return "CMD " + json.dumps(_exec_or_shell(argv), ensure_ascii=False)


def _copy_line(root: str, dest: str) -> str:
    if _is_subroot(root):
        return f"COPY {_norm_root(root)}/ {dest}"
    return f"COPY ./ {dest}"


# ---- resource / env lowering --------------------------------------------------


def _mount_dir(resource: ResourceDecl) -> str:
    """The directory a resource's named volume mounts at — the parent dir of its
    persistent path (a SQLite `file:/data/app.db` → `/data`). Delegates to the
    SHARED `spec.local_mount_target` so the emitter and the schema's
    duplicate-mount-target validator can never disagree (WO-C7)."""
    return local_mount_target(resource)


def _resource_volume_names(spec: ReleaseSpec) -> dict[str, str]:
    """Map each resource id to a UNIQUE compose volume name, ORDER-INDEPENDENTLY
    (WO-C7 §11.9).

    A named volume per distinct persistent path is load-bearing: two resources that
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
            entries.append((local_mount_target(resource), volume_names[resource.id]))
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
        return [f"{volume_names[migrated.id]}:{local_mount_target(migrated)}"]
    return _service_mounts(spec, ingress.id, volume_names)


def _migrate_service_name(spec: ReleaseSpec) -> str:
    taken = {service.id for service in spec.services}
    if _MIGRATE_SERVICE not in taken:
        return _MIGRATE_SERVICE
    return _MIGRATE_SERVICE_FALLBACK


# ---- build block + Dockerfile paths -------------------------------------------


def _dockerfile_ref(service: ReleaseService, *, multi: bool) -> str:
    """The `build.dockerfile` a service's compose block points at."""
    if service.runtime is RuntimeStrategy.container:
        # The app ships its own Dockerfile at its root; reference, never emit.
        if _is_subroot(service.root):
            return f"{_norm_root(service.root)}/{DOCKERFILE_PATH}"
        return DOCKERFILE_PATH
    if multi:
        return f"{_SELFHOST_DIR}/{service.id}.Dockerfile"
    return DOCKERFILE_PATH


def _build_block(service: ReleaseService, *, multi: bool) -> dict[str, _Yaml]:
    return {"context": ".", "dockerfile": _dockerfile_ref(service, multi=multi)}


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
    environment: dict[str, _Yaml] = {}
    for name, value in _service_runtime_env(spec, service).items():
        environment[name] = value
    # Port ownership is adapter-authoritative. ReleaseSpec rejects every collision, and
    # assigning this last is a defensive belt for future internal callers.
    environment[service.port_env] = str(_CONTAINER_PORT)
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


# ---- Dockerfile emission ------------------------------------------------------


def _effective_install_cmd(service: ReleaseService) -> tuple[str, ...]:
    """The install command to emit before a build. A build-requiring service MUST
    install its dependencies first, or the image builds against no dependencies. If
    the spec supplied an explicit `install_cmd`, use it; otherwise, when the service
    declares a `build_cmd` but no install, derive a conservative default from the
    runtime (npm ci when a lockfile is declared, else npm install; pip for python)
    so any build-requiring bundle is buildable. A service with neither an install
    nor a build gets no install line."""
    if service.install_cmd:
        return service.install_cmd
    if not service.build_cmd:
        return ()
    if service.runtime is RuntimeStrategy.python:
        return ("pip", "install", ".")
    # node / static build toolchains are npm-based in this adapter.
    if service.lockfile:
        return ("npm", "ci")
    return ("npm", "install")


def _arg_lines(spec: ReleaseSpec) -> list[str]:
    """`ARG <NAME>` declarations for every build-scope PUBLIC env var, placed after
    `WORKDIR` so the build var is visible to the subsequent install/build `RUN`
    steps. Emitted as a bare `ARG` (no default) — the value flows in from the Compose
    `build.args` guard, never persisted with `ENV`, so it is absent from the final
    runtime environment."""
    return [f"ARG {name}" for name in _build_arg_names(spec)]


def _node_dockerfile(spec: ReleaseSpec, service: ReleaseService) -> str:
    lines = [_DOCKERFILE_HEADER.rstrip("\n"), f"FROM {_NODE_IMAGE}", "WORKDIR /app"]
    lines.extend(_arg_lines(spec))
    lines.append(_copy_line(service.root, "./"))
    install = _effective_install_cmd(service)
    if install:
        lines.append(_run_line(install))
    if service.build_cmd:
        lines.append(_run_line(service.build_cmd))
    lines.append("ENV NODE_ENV=production")
    lines.append(f"EXPOSE {_CONTAINER_PORT}")
    start = service.start_cmd or ("npm", "start")
    lines.append(_cmd_line(start))
    return "\n".join(lines) + "\n"


def _python_dockerfile(spec: ReleaseSpec, service: ReleaseService) -> str:
    lines = [_DOCKERFILE_HEADER.rstrip("\n"), f"FROM {_PYTHON_IMAGE}", "WORKDIR /app"]
    lines.append("ENV PYTHONUNBUFFERED=1")
    lines.extend(_arg_lines(spec))
    lines.append(_copy_line(service.root, "./"))
    install = _effective_install_cmd(service)
    if install:
        lines.append(_run_line(install))
    if service.build_cmd:
        lines.append(_run_line(service.build_cmd))
    lines.append(f"EXPOSE {_CONTAINER_PORT}")
    start = service.start_cmd or ("python", "-m", "http.server", str(_CONTAINER_PORT))
    lines.append(_cmd_line(start))
    return "\n".join(lines) + "\n"


def _dir_token(value: str | None) -> str:
    """Normalize a path fragment to a bare directory token (`""` for the root)."""
    if value is None:
        return ""
    token = value.strip().rstrip("/")
    if token.startswith("./"):
        token = token[2:]
    return "" if token in ("", ".") else token


def _static_src(root: str, output_dir: str | None) -> str:
    """The build-context source dir for a prebuilt static site — `root/output_dir`
    normalized, or `./` for the workspace root."""
    parts = [p for p in (_dir_token(root), _dir_token(output_dir)) if p]
    return "./" if not parts else "/".join(parts) + "/"


def _static_dockerfile(spec: ReleaseSpec, service: ReleaseService) -> str:
    lines = [_DOCKERFILE_HEADER.rstrip("\n")]
    out_token = _dir_token(service.output_dir)
    if service.build_cmd:
        # Two-stage: build the assets with node, serve them from a tiny static
        # image so the shipped image carries no build toolchain.
        lines.append(f"FROM {_NODE_IMAGE} AS build")
        lines.append("WORKDIR /app")
        lines.extend(_arg_lines(spec))
        lines.append(_copy_line(service.root, "./"))
        install = _effective_install_cmd(service)
        if install:
            lines.append(_run_line(install))
        lines.append(_run_line(service.build_cmd))
        lines.append(f"FROM {_STATIC_IMAGE}")
        lines.append("WORKDIR /site")
        built = f"/app/{out_token}/" if out_token else "/app/"
        lines.append(f"COPY --from=build {built} /site/")
    else:
        lines.append(f"FROM {_STATIC_IMAGE}")
        lines.append("WORKDIR /site")
        lines.append(f"COPY {_static_src(service.root, service.output_dir)} /site/")
    lines.append(f"EXPOSE {_CONTAINER_PORT}")
    lines.append(
        "CMD "
        + json.dumps(
            ["busybox", "httpd", "-f", "-v", "-p", str(_CONTAINER_PORT), "-h", "/site"],
            ensure_ascii=False,
        )
    )
    return "\n".join(lines) + "\n"


def _dockerfile_for(spec: ReleaseSpec, service: ReleaseService) -> str | None:
    """The Dockerfile text for a service, or `None` for a `container` service
    (which ships its own Dockerfile — we reference, never emit/overwrite it)."""
    if service.runtime is RuntimeStrategy.node:
        return _node_dockerfile(spec, service)
    if service.runtime is RuntimeStrategy.python:
        return _python_dockerfile(spec, service)
    if service.runtime is RuntimeStrategy.static:
        return _static_dockerfile(spec, service)
    if service.runtime is RuntimeStrategy.container:
        return None
    # `dev_server` (AppKit) is handled BEFORE this generic loop, by
    # `_emit_dev_server_overlay` (its whole compose shape differs). Reaching here
    # with one is a bug — fail loud rather than emit a silently-wrong bundle.
    raise ValueError(
        f"local_compose has no generic Dockerfile for runtime strategy "
        f"{service.runtime.value!r} (dev_server is emitted via its own overlay); "
        f"got service {service.id!r}"
    )


# ---- .dockerignore ------------------------------------------------------------


def _dockerignore() -> str:
    entries = [
        "# Generated by Disco (local_compose).",
        "# Keeps secrets and build cruft out of the image build context.",
        ".git",
        ".gitignore",
        ".gitattributes",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "*.pyc",
        "*.pyo",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".disco",
        "*.log",
        ".DS_Store",
        ".env",
        ".env.*",
        "!.env.example",
        # workerd dev secrets (`.dev.vars`, `.dev.vars.production`, …): a stray host
        # one — if the owner ran `wrangler dev` before building — must never enter an
        # image layer. The committed `.dev.vars.example` placeholder stays included.
        ".dev.vars",
        ".dev.vars.*",
        "!.dev.vars.example",
    ]
    return "\n".join(entries) + "\n"


# ---- .env.example (NAMES ONLY) ------------------------------------------------


def _host_supplied_env(spec: ReleaseSpec) -> list[EnvVarDecl]:
    """The env vars the HOST must supply — every declared var that is not bound to
    a resource (bound vars are auto-provided by compose). Sorted by NAME."""
    supplied = [var for var in spec.env if var.binding is None]
    return sorted(supplied, key=lambda v: v.name)


def _env_example(spec: ReleaseSpec, host_port: int) -> str:
    lines = [
        "# .env.example — generated by Disco (local_compose).",
        "#",
        "# Copy this file to `.env` and fill in the values. It is SECRET-FREE: it",
        "# lists only variable NAMES, never values. Never commit real secrets.",
        "#",
        f"# HOST_PORT — host port to publish the app on (optional; default {host_port}).",
        f"# HOST_PORT={host_port}",
    ]
    for var in _host_supplied_env(spec):
        secrecy = "secret" if var.secret is SecretClass.secret else "public"
        need = "required" if var.required else "optional"
        lines.append("#")
        lines.append(
            f"# {var.name} — {need} {secrecy} ({var.scope.value}). "
            "Supplied by the host; never commit its value."
        )
        if var.required:
            lines.append(f"{var.name}=")
        else:
            lines.append(f"# {var.name}=")
    return "\n".join(lines) + "\n"


# ---- SELFHOST.md --------------------------------------------------------------
#
# WO-C5 #2 F3 (documented accepted limit, out of the §9 executed-artifact charter):
# `spec.name` is the user's conversation title and is rendered into SELFHOST.md
# markdown. That is a DOCS surface — it never reaches `compose.yaml` / `Dockerfile`
# (the executed artifacts), and Markdown is not executed — so a metacharacter in the
# title is at most docs-content, not command/path injection, and is intentionally NOT
# in the §9 injection boundary. As cheap defense-in-depth we still strip CR/LF and
# control characters so a crafted title cannot forge new Markdown lines/structure.


def _md_title(name: str) -> str:
    """A one-line Markdown-safe rendering of a user-supplied name for a heading —
    CR/LF and other control characters (ord < 0x20, and DEL) removed so the title
    cannot forge new Markdown lines/structure. Spaces and printable text are kept."""
    return "".join(ch for ch in name if ch >= " " and ch != "\x7f")


def _selfhost_doc(spec: ReleaseSpec) -> str:
    ingress = _ingress_service(spec)
    health = ingress.health_path or "/"
    lines = [
        f"# Self-hosting {_md_title(spec.name)}",
        "",
        "This bundle was generated by Disco (`local_compose`). It is **secret-free**:",
        "it contains only environment-variable NAMES, never values.",
        "",
        "## Prerequisites",
        "",
        "- Docker Engine with the Compose v2 plugin (`docker compose`).",
        "",
        "## Run it",
        "",
        "```sh",
        "cp .env.example .env      # then fill in the required values",
        "docker compose up -d --build",
        "```",
        "",
        f"The app is published on `127.0.0.1:${{{_HOST_PORT_VAR}:-{_CONTAINER_PORT}}}`",
        f"(override the host port by setting `{_HOST_PORT_VAR}` in `.env`).",
        "",
        "## Required environment",
        "",
    ]
    supplied = [var for var in _host_supplied_env(spec) if var.required]
    if supplied:
        for var in supplied:
            secrecy = "secret" if var.secret is SecretClass.secret else "public"
            lines.append(f"- `{var.name}` — {secrecy} ({var.scope.value}).")
    else:
        lines.append("- (none — the app needs no host-supplied environment.)")
    lines.append("")
    lines.append("## Persistent data")
    lines.append("")
    if spec.resources:
        volume_names = _resource_volume_names(spec)
        for resource in spec.resources:
            volume = volume_names[resource.id]
            lines.append(
                f"- `{resource.kind.value}` — named volume `{volume}` mounted at "
                f"`{_mount_dir(resource)}`."
            )
    else:
        lines.append("- (none — the app is stateless.)")
    lines.append("")
    lines.append("## Health")
    lines.append("")
    lines.append(f"The app is healthy when `GET {health}` returns a non-error status.")
    lines.append("")
    lines.append("## Contract")
    lines.append("")
    lines.append("`release.json` is the canonical release spec this bundle was generated from")
    lines.append("— the runtime contract. Regenerate the bundle if you edit it.")
    return "\n".join(lines) + "\n"


# ---- dev_server (AppKit interim local run) ------------------------------------
#
# AppKit apps target the Cloudflare workerd runtime. WO-6 gives them an INTERIM
# local host: `docker compose up -d --build` runs the app on the workerd DEV
# runtime (`wrangler dev`) with a persistent, volume-backed local D1 — zero
# changes to AppKit generation or the Cloudflare deploy path. A compose-native
# self-host (a standalone server image, no dev runtime) lands with AppKit v2.


def _dev_init_service_name(spec: ReleaseSpec) -> str:
    """The one-shot D1 init service name, avoiding a collision with any service
    id (the AppKit topology only declares `web`, so `init` is normally free)."""
    taken = {service.id for service in spec.services}
    if _DEV_INIT_SERVICE not in taken:
        return _DEV_INIT_SERVICE
    return _DEV_INIT_SERVICE_FALLBACK


def _first_sqlite_resource(spec: ReleaseSpec) -> ResourceDecl:
    """The sqlite-class resource an AppKit dev_server release persists (its local
    D1 state). Required — the init/app services and the volume derive from it."""
    for resource in spec.resources:
        if resource.kind is ResourceKind.sqlite:
            return resource
    raise ValueError(
        "a dev_server (AppKit) release requires a sqlite-class resource for its "
        "persistent local D1 state; the spec declares none"
    )


def _dev_server_entrypoint_script(spec: ReleaseSpec) -> str:
    """The container entrypoint body (materialized into the Dockerfile by a
    heredoc-free ``RUN`` — see ``_entrypoint_install_line``).

    It writes the workerd dev-secrets file AT CONTAINER START from the
    host-injected, required secret env vars — each guarded with `${NAME:?...}` so a
    missing secret fails the container loudly — then `exec "$@"` runs the app
    command. The secret VALUE therefore exists only inside the running container
    and is NEVER written into the exported bundle."""
    secrets = [var for var in _host_supplied_env(spec) if var.required]
    lines = ["#!/bin/sh", "set -eu", "umask 077"]
    # Fail closed on any missing required secret before writing anything.
    for var in secrets:
        lines.append(f': "{_required_guard(var.name)}"')
    # Materialize the dev-secrets file from the guarded env (NAMES only here).
    lines.append("{")
    for var in secrets:
        lines.append(f"printf '{var.name}=%s\\n' \"${var.name}\"")
    lines.append(f"}} > {_DEV_VARS_FILE}")
    lines.append('exec "$@"')
    return "\n".join(lines)


def _entrypoint_install_line(script: str) -> str:
    """Materialize the entrypoint SCRIPT into the image at build time WITHOUT a
    Dockerfile heredoc.

    A heredoc ``COPY <<EOF`` (or ``RUN <<EOF``) is a BuildKit/Buildx-only Dockerfile
    feature that the LEGACY Docker Engine builder rejects — yet the exported bundle
    declares only "Docker Engine with the Compose v2 plugin" as its prerequisite (NOT
    BuildKit/Buildx). So the (already secret-free, NAMES-only) script is written by a
    single classic-builder-compatible ``RUN printf``: each script line is passed as an
    inert single-quoted argument to ``printf '%s\\n'`` (a shell ``$NAME`` /
    ``${NAME:?}`` therefore reaches the file LITERALLY, unexpanded at build time), the
    output redirected to the entrypoint path, then made executable. The script is NOT
    added to the build context / bundle as a discrete overlay file, so the emitted
    self-host overlay path set is unchanged.

    Each line is quoted with ``_shell_quote_literal`` (the same POSIX single-quote
    discipline the compose command lowering uses), so an embedded single quote in a
    line is closed/escaped/reopened and no metacharacter is interpreted."""
    quoted = " ".join(_shell_quote_literal(line) for line in script.split("\n"))
    return f"RUN printf '%s\\n' {quoted} > {_ENTRYPOINT_PATH} && chmod +x {_ENTRYPOINT_PATH}"


def _dev_server_dockerfile(spec: ReleaseSpec) -> str:
    """The AppKit local-run image: install from the checked-in lockfile, build the
    Vite bundle, then hand off to an entrypoint that writes the dev-secrets file at
    start. Uses shell-form `RUN` for the install/build steps (a portable, readable
    `npm ci` / `npm run build`).

    The entrypoint is materialized by a heredoc-free `RUN printf`
    (`_entrypoint_install_line`) — a classic-builder instruction — NOT a heredoc
    `COPY <<EOF`, so the image builds on exactly the declared prerequisite (Docker
    Engine + Compose v2, no BuildKit/Buildx)."""
    script = _dev_server_entrypoint_script(spec)
    parts = [
        _DOCKERFILE_HEADER.rstrip("\n"),
        f"FROM {_DEV_SERVER_IMAGE}",
        "WORKDIR /app",
        "COPY ./ ./",
        # `npm ci` installs the exact, checked-in AppKit dependency lock; `npm run
        # build` produces the Vite `dist/` the workerd dev runtime serves.
        "RUN npm ci",
        "RUN npm run build",
        # The entrypoint writes the dev-secrets file at CONTAINER START from the
        # host-injected secret env — the secret never lands in the bundle. It is
        # materialized by a heredoc-free `RUN printf` (legacy-builder compatible).
        _entrypoint_install_line(script),
        f"EXPOSE {_DEV_SERVER_PORT}",
        f'ENTRYPOINT ["{_ENTRYPOINT_PATH}"]',
    ]
    return "\n".join(parts) + "\n"


def _dev_server_healthcheck(service: ReleaseService) -> dict[str, _Yaml]:
    """A `GET <health_path>` probe on the wrangler-dev port using the node runtime
    already present in the image."""
    path = service.health_path or "/"
    url = f"http://127.0.0.1:{_DEV_SERVER_PORT}{path}"
    script = (
        "require('http').get("
        f"'{url}',"
        "r=>process.exit(r.statusCode<400?0:1)"
        ").on('error',()=>process.exit(1))"
    )
    return {
        "test": ["CMD", "node", "-e", script],
        "interval": "10s",
        "timeout": "3s",
        "retries": 5,
        # wrangler dev + the one-shot D1 init take a moment to become ready.
        "start_period": "40s",
    }


def _dev_server_compose_document(spec: ReleaseSpec, ingress: ReleaseService) -> dict[str, _Yaml]:
    """The compose document for an AppKit dev_server release: a one-shot init
    service that applies `schema.sql` to the persistent local D1, then the app
    service running `wrangler dev` bound to all interfaces, ordered AFTER init via
    `service_completed_successfully`. Both share the image and the state volume."""
    resource = _first_sqlite_resource(spec)
    persist_dir = resource.persistent_path
    mount = f"{resource.profiles.local.volume}:{_mount_dir(resource)}"
    init_name = _dev_init_service_name(spec)

    if not resource.migrate_cmd:
        raise ValueError(
            "a dev_server (AppKit) release requires the sqlite resource to carry a "
            "migrate_cmd (the `wrangler d1 execute` argv); the spec declares none"
        )
    # The init service overrides the image ENTRYPOINT (it needs no admin secret) and
    # applies schema.sql to the SAME persist dir the app reads.
    init_argv: list[str] = list(resource.migrate_cmd) + ["--persist-to", persist_dir]
    app_argv: list[str] = [
        "npx",
        "wrangler",
        "dev",
        "--ip",
        "0.0.0.0",
        "--port",
        str(_DEV_SERVER_PORT),
        "--persist-to",
        persist_dir,
    ]

    init_block: dict[str, _Yaml] = {
        "build": _build_block(ingress, multi=False),
        "restart": "no",
        "entrypoint": init_argv,
        "volumes": [mount],
    }

    app_block: dict[str, _Yaml] = {
        "build": _build_block(ingress, multi=False),
        "restart": "unless-stopped",
        "depends_on": {init_name: {"condition": "service_completed_successfully"}},
        "command": app_argv,
    }
    # AppKit is single-service (the sole `web`/dev_server ingress), so the consumer
    # topology routes every runtime env var to it exactly as before (WO-C7 keeps
    # this byte-deterministic — criterion 10).
    environment: dict[str, _Yaml] = {}
    for name, value in _service_runtime_env(spec, ingress).items():
        environment[name] = value
    if environment:
        app_block["environment"] = {name: environment[name] for name in sorted(environment)}
    app_block["ports"] = [f"127.0.0.1:${{{_HOST_PORT_VAR}:-{_DEV_SERVER_PORT}}}:{_DEV_SERVER_PORT}"]
    app_block["volumes"] = [mount]
    app_block["healthcheck"] = _dev_server_healthcheck(ingress)

    services: dict[str, _Yaml] = {init_name: init_block, ingress.id: app_block}
    document: dict[str, _Yaml] = {"services": services}
    document["volumes"] = {resource.profiles.local.volume: {}}
    return document


def _dev_server_selfhost_doc(spec: ReleaseSpec) -> str:
    resource = _first_sqlite_resource(spec)
    lines = [
        f"# Self-hosting {_md_title(spec.name)} (interim local run)",
        "",
        "This bundle runs the app on the **workerd dev runtime** (`wrangler dev`) as an",
        "**interim** local host. It is **secret-free**: it contains only",
        "environment-variable NAMES, never values. A compose-native self-host (a",
        "standalone server image, no dev runtime) lands with **AppKit v2**.",
        "",
        "## Prerequisites",
        "",
        "- Docker Engine with the Compose v2 plugin (`docker compose`).",
        "",
        "## Run it",
        "",
        "```sh",
        "cp .env.example .env      # then fill in the required values",
        "docker compose up -d --build",
        "```",
        "",
        f"The app is published on `127.0.0.1:${{{_HOST_PORT_VAR}:-{_DEV_SERVER_PORT}}}`",
        f"(override the host port by setting `{_HOST_PORT_VAR}` in `.env`).",
        "",
        "## How it works",
        "",
        "- A one-shot **init** service applies `schema.sql` to a local D1 database that",
        f"  persists under `{resource.persistent_path}` before the app starts.",
        "- The **app** service runs `wrangler dev` bound to all interfaces, persisting",
        "  its local D1 state to a named volume so data survives restarts.",
        "- The admin secret is materialized INSIDE the running container at startup from",
        "  the host-injected environment — it is never written into this bundle.",
        "",
        "## Required environment",
        "",
    ]
    supplied = [var for var in _host_supplied_env(spec) if var.required]
    if supplied:
        for var in supplied:
            secrecy = "secret" if var.secret is SecretClass.secret else "public"
            lines.append(f"- `{var.name}` — {secrecy} ({var.scope.value}).")
    else:
        lines.append("- (none — the app needs no host-supplied environment.)")
    lines.append("")
    lines.append("## Persistent data")
    lines.append("")
    lines.append(
        f"- `{resource.kind.value}` — named volume `{resource.profiles.local.volume}` "
        f"mounted at `{_mount_dir(resource)}` (local D1 state)."
    )
    lines.append("")
    lines.append("## Contract")
    lines.append("")
    lines.append("`release.json` is the canonical release spec this bundle was generated from")
    lines.append("— the runtime contract. Regenerate the bundle if you edit it.")
    return "\n".join(lines) + "\n"


def _emit_dev_server_overlay(spec: ReleaseSpec, ingress: ReleaseService) -> dict[str, str]:
    """The complete AppKit interim local-run overlay: `compose.yaml`, the
    `Dockerfile` (which materializes the secret-writing entrypoint via a heredoc-free
    `RUN printf`), `.dockerignore`, a NAMES-ONLY `.env.example`, a strategy-specific
    `SELFHOST.md`, and `release.json`. PURE + DETERMINISTIC and secret-free by
    construction."""
    overlay: dict[str, str] = {}
    overlay[COMPOSE_PATH] = _emit_yaml(_dev_server_compose_document(spec, ingress))
    overlay[DOCKERFILE_PATH] = _dev_server_dockerfile(spec)
    overlay[DOCKERIGNORE_PATH] = _dockerignore()
    overlay[ENV_EXAMPLE_PATH] = _env_example(spec, _DEV_SERVER_PORT)
    overlay[SELFHOST_DOC_PATH] = _dev_server_selfhost_doc(spec)
    overlay[RELEASE_JSON_PATH] = serialize_release_spec(spec)
    return overlay


# ---- the public entrypoints ---------------------------------------------------


def _revalidated(spec: ReleaseSpec) -> ReleaseSpec:
    """Re-run the full `ReleaseSpec` schema validation before emission (WO-C5).

    A `ReleaseSpec` can be built through a NON-validating path — pydantic's
    `model_copy(update=...)` or `model_construct(...)` — that skips the field and
    cross-field validators (the same path `serialize_release_spec`'s docstring says
    must be judged honestly). Re-validating the dumped spec re-applies every guard
    (safe `root`/`output_dir` paths, argv token hygiene, the restricted health-path
    grammar, resource url safety), so a smuggled injection is rejected here — BEFORE
    any Dockerfile / compose byte is produced — rather than lowered into the overlay.
    A genuinely-valid spec round-trips unchanged."""
    return ReleaseSpec.model_validate(spec.model_dump(mode="json"))


def _reject_positional_command_secrets(spec: ReleaseSpec) -> None:
    """Defense in depth (GAP G02): refuse to EMIT a spec whose service start / build /
    migrate command — or a resource migrate_cmd — carries a bare POSITIONAL literal
    credential, even after detection's command-grammar gate. A credential-capable operand
    must be a whole ${NAME} reference; a literal secret is never lowered into the
    Dockerfile CMD / compose migrate command / release.json.
    Flags carry their own value-hygiene upstream (a flag inline secret is rejected at
    declaration), so only bare operands are scanned. Value-free: the message names the
    rule, never the offending token (which can be the secret)."""
    commands: list[tuple[str, tuple[str, ...]]] = []
    for service in spec.services:
        commands.append(("service start_cmd", service.start_cmd))
        commands.append(("service build_cmd", service.build_cmd))
        commands.append(("service install_cmd", service.install_cmd))
        commands.append(("service migrate_cmd", service.migrate_cmd))
    for resource in spec.resources:
        commands.append(("resource migrate_cmd", resource.migrate_cmd))
    for command_field, argv in commands:
        for token in argv:
            if token.startswith("-"):
                continue
            if looks_like_credential_literal(token):
                raise ValueError(
                    f"cannot emit a self-host overlay: a {command_field} carries a bare "
                    "positional literal credential; reference secrets by a whole ${NAME} "
                    "env var, never inline."
                )


def emit_local_compose(spec: ReleaseSpec) -> dict[str, str]:
    """Lower a `ReleaseSpec` into the complete, secret-free self-host overlay:
    `{path: content}`.

    Emits `compose.yaml`, a per-service `Dockerfile` (single service) or
    `selfhost/<service>.Dockerfile` (multi-service; `container` services ship
    their own and are referenced, not emitted), `.dockerignore`, a NAMES-ONLY
    `.env.example`, `SELFHOST.md`, and `release.json` (the canonical serialized
    spec). PURE + DETERMINISTIC: same spec in → byte-identical overlay out.

    An AppKit `dev_server` ingress takes the interim local-run path
    (`_emit_dev_server_overlay`): `wrangler dev` on the workerd dev runtime with a
    persistent, volume-backed local D1 and a secret-writing container entrypoint.

    Raises `ValueError`/`ValidationError` if the spec has no ingress service OR if the
    spec is not schema-valid (WO-C5: emission RE-VALIDATES first, so a spec built
    through a non-construction path — `model_copy(update=...)` / `model_construct(...)`
    — that smuggled a malicious `root` / `output_dir` / command / health path past the
    field validators is REJECTED before ANY byte is emitted; validation fails BEFORE
    emission)."""
    spec = _revalidated(spec)
    _reject_positional_command_secrets(spec)
    ingress = _ingress_service(spec)
    if ingress.runtime is RuntimeStrategy.dev_server:
        return _emit_dev_server_overlay(spec, ingress)

    services = list(spec.services)
    multi = len(services) > 1

    overlay: dict[str, str] = {}
    overlay[COMPOSE_PATH] = _emit_yaml(_compose_document(spec))

    for service in services:
        dockerfile = _dockerfile_for(spec, service)
        if dockerfile is None:
            continue  # container service ships its own Dockerfile
        if multi:
            overlay[f"{_SELFHOST_DIR}/{service.id}.Dockerfile"] = dockerfile
        else:
            overlay[DOCKERFILE_PATH] = dockerfile

    overlay[DOCKERIGNORE_PATH] = _dockerignore()
    overlay[ENV_EXAMPLE_PATH] = _env_example(spec, _CONTAINER_PORT)
    overlay[SELFHOST_DOC_PATH] = _selfhost_doc(spec)
    overlay[RELEASE_JSON_PATH] = serialize_release_spec(spec)
    return overlay


def emit_local_compose_checked(
    spec: ReleaseSpec, existing_paths: Iterable[str]
) -> dict[str, str] | OverlayConflict:
    """Like `emit_local_compose`, but collision-safe against a caller-supplied set
    of paths already present in the workspace.

    Returns an `OverlayConflict` (naming every colliding path) INSTEAD of the
    overlay when any overlay path already exists — emission never silently
    overwrites. When there is no collision, returns the same overlay
    `emit_local_compose` would."""
    overlay = emit_local_compose(spec)
    existing = set(existing_paths)
    collisions = sorted(path for path in overlay if path in existing)
    if collisions:
        return OverlayConflict(paths=tuple(collisions))
    return overlay


__all__ = [
    "COMPOSE_PATH",
    "DOCKERFILE_PATH",
    "DOCKERIGNORE_PATH",
    "ENV_EXAMPLE_PATH",
    "OverlayConflict",
    "RELEASE_JSON_PATH",
    "SELFHOST_DOC_PATH",
    "emit_local_compose",
    "emit_local_compose_checked",
]
