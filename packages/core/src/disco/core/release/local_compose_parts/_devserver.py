"""AppKit `dev_server` interim local-run overlay (the `wrangler dev` compose path).

Extracted from ``local_compose.py`` to reduce module size; the public facade
re-imports ``_emit_dev_server_overlay`` unchanged.

AppKit apps target the Cloudflare workerd runtime. WO-6 gives them an INTERIM
local host: `docker compose up -d --build` runs the app on the workerd DEV
runtime (`wrangler dev`) with a persistent, volume-backed local D1 — zero
changes to AppKit generation or the Cloudflare deploy path. A compose-native
self-host (a standalone server image, no dev runtime) lands with AppKit v2.

``_emit_dev_server_overlay`` needs the public overlay-path constants that stay
defined in the parent ``local_compose`` module. Since that module imports this
one (to build the public facade), a module-level import back would be a
genuine load-time circular import — so it is imported LAZILY, inside the
function body, at the point of use.
"""

from __future__ import annotations

from disco.core.release.spec import (
    ReleaseService,
    ReleaseSpec,
    ResourceDecl,
    ResourceKind,
    SecretClass,
    serialize_release_spec,
)

from ._dockerfile import _DOCKERFILE_HEADER, _build_block, _dockerignore
from ._docs import _env_example, _host_supplied_env, _md_title
from ._services import _mount_dir, _service_runtime_env
from ._shell import _HOST_PORT_VAR, _required_guard, _shell_quote_literal
from ._yaml import _emit_yaml, _Yaml

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
    Engine + Compose v2, no BuildKit/Buildx). Resolved through the parent
    `local_compose` module's own binding (rather than this module's own
    function) so it is always the SAME live binding external callers observe on
    `disco.core.release.local_compose._entrypoint_install_line` — the facade
    attribute this materialization step has always been driven through."""
    from disco.core.release import local_compose

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
        local_compose._entrypoint_install_line(script),
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
    from disco.core.release.local_compose import (
        COMPOSE_PATH,
        DOCKERFILE_PATH,
        DOCKERIGNORE_PATH,
        ENV_EXAMPLE_PATH,
        RELEASE_JSON_PATH,
        SELFHOST_DOC_PATH,
    )

    overlay: dict[str, str] = {}
    overlay[COMPOSE_PATH] = _emit_yaml(_dev_server_compose_document(spec, ingress))
    overlay[DOCKERFILE_PATH] = _dev_server_dockerfile(spec)
    overlay[DOCKERIGNORE_PATH] = _dockerignore()
    overlay[ENV_EXAMPLE_PATH] = _env_example(spec, _DEV_SERVER_PORT)
    overlay[SELFHOST_DOC_PATH] = _dev_server_selfhost_doc(spec)
    overlay[RELEASE_JSON_PATH] = serialize_release_spec(spec)
    return overlay
