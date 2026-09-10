"""WO-4 — the LocalComposeAdapter: a `ReleaseSpec` → portable self-host bundle.

`emit_local_compose(spec)` lowers the neutral, secret-free `ReleaseSpec` into the
complete overlay a user runs with `docker compose up -d --build`: a `compose.yaml`,
a per-service `Dockerfile` (or `selfhost/<service>.Dockerfile` for a multi-service
release), a `.dockerignore`, a NAMES-ONLY `.env.example`, a `SELFHOST.md`, and
`release.json` (the canonical serialized spec — the runtime contract the bundle
was generated from).

Two guarantees carry over from the spec and are essential here:

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

This module is the state-free public compatibility/export facade. Cohesive
private implementation lives under :mod:`disco.core.release.local_compose_parts`
and is re-imported here so every public symbol keeps its import path.
"""

from __future__ import annotations

from collections.abc import Iterable

from disco.core.release.command_grammar import looks_like_credential_literal

# `local_mount_target` / `_entrypoint_install_line` are explicit re-exports
# (`as`-aliased to themselves, the standard marker for "unused here but part of
# this module's public attribute surface"), not used directly below: the sibling
# parts that resolve a mount target / materialize the dev-server entrypoint do so
# through THIS module's own binding (`disco.core.release.local_compose.<name>`),
# not their own direct import — so this facade's attribute is always the single
# live binding external callers (and tests) observe and may override.
from disco.core.release.spec import (
    ReleaseSpec,
    RuntimeStrategy,
    serialize_release_spec,
)
from disco.core.release.spec import (
    local_mount_target as local_mount_target,
)
from pydantic import BaseModel, ConfigDict

from .local_compose_parts._devserver import (
    _emit_dev_server_overlay,
)
from .local_compose_parts._devserver import (
    _entrypoint_install_line as _entrypoint_install_line,
)
from .local_compose_parts._dockerfile import _SELFHOST_DIR, _dockerfile_for, _dockerignore
from .local_compose_parts._docs import _env_example, _selfhost_doc
from .local_compose_parts._services import _compose_document, _ingress_service
from .local_compose_parts._shell import _CONTAINER_PORT
from .local_compose_parts._yaml import _emit_yaml

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
