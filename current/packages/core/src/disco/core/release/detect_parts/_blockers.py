"""Typed-intent fail-closed blockers shared by the intent/static ladder rungs:
unprovisioned-manager checks, the runtime command grammar, resource-topology
and health-route checks, and the repairable-diagnostic field list.
"""

from __future__ import annotations

from collections.abc import Mapping

from disco.core.release import spec as _spec
from disco.core.release.command_grammar import (
    check_declaration_argv,
    check_no_inline_secret_cli,
    check_no_positional_credential,
)
from disco.core.release.spec import ReleaseIntent, ResourceDecl, RuntimeStrategy

from ._constants import _UNPROVISIONED_MANAGERS, _UNVERIFIABLE
from ._models import MissingField, _DetectBlocker
from ._node import _is_supported_toolchain_head
from ._python import _declared_python_deps
from ._text import _scan_text, _strip_comments


def _declared_manager_blocker(intent: ReleaseIntent) -> _DetectBlocker | None:
    """A `toolchain_unsupported` fail-closed outcome (R2 / G05) when a TYPED intent names
    a package manager the neutral base images do NOT provision — pnpm / yarn / bun / deno
    / poetry / uv (or an `x`-runner like `bunx`/`pnpx`) — as its `build_cmd` /
    `install_cmd` HEAD, or as its declared `package_manager`. The intent-path analogue of
    the source-driven `_unsupported_pm_blocker` / `_unsupported_node_pm_declaration`
    rejects: the manager is NEVER silently mapped onto npm/pip and lowered into an image
    that cannot run it (§8.8). `None` when no unprovisioned manager is named. The start
    HEAD is covered separately by the `_is_supported_toolchain_head` allowlist."""
    for field_name, argv in (("build_cmd", intent.build_cmd), ("install_cmd", intent.install_cmd)):
        if not argv:
            continue
        head = argv[0].rsplit("/", 1)[-1].lower()
        if head in _UNPROVISIONED_MANAGERS:
            return _DetectBlocker(
                code="toolchain_unsupported",
                message=(
                    f"the declared {field_name} heads with {head!r}, a package manager the "
                    "neutral base image does not provision; the detector will NOT silently "
                    "map it onto npm/pip. Use npm (package-lock.json) / pip "
                    "(requirements.txt), or vendor the toolchain explicitly."
                ),
                field=field_name,
                evidence=(f"toolchain evidence: {field_name} head {head!r} not on the base image",),
            )
    if intent.package_manager is not None:
        manager = intent.package_manager.strip().lower()
        if manager in _UNPROVISIONED_MANAGERS:
            return _DetectBlocker(
                code="toolchain_unsupported",
                message=(
                    f"the declared package_manager {manager!r} is not provisioned in the "
                    "neutral base image; the detector will NOT silently map it onto "
                    "npm/pip. Declare npm or pip, or vendor the toolchain explicitly."
                ),
                field="package_manager",
                evidence=(
                    f"toolchain evidence: package_manager {manager!r} not on the base image",
                ),
            )
    return None


def _toolchain_blocker(
    intent: ReleaseIntent, files: Mapping[str, str | bytes]
) -> _DetectBlocker | None:
    """A `toolchain_unsupported` fail-closed outcome when the declared start command
    cannot run on the neutral base image: a head that is NOT a supported toolchain
    (anything outside the allowlist — ruby/go/php/caddy/`./server`/bun/deno/uv/poetry
    AND pnpm/yarn all fail here, never a Node fallback), a python start executable that
    is a pip package absent from the declared dependencies, or an unprovisioned package
    manager named in build_cmd/install_cmd/package_manager (R2 / G05). `None` when the
    toolchain is supported."""
    # Deferred: `_intent` imports `_toolchain_blocker` from this module at module scope,
    # so this module must not import `_intent` back at module scope (a genuine two-way
    # dependency the original single-file layout never had to name). Resolved lazily —
    # by call time both modules are fully loaded.
    from ._intent import _runtime_from_argv

    head = intent.start_cmd[0].rsplit("/", 1)[-1].lower()
    if not _is_supported_toolchain_head(head):
        return _DetectBlocker(
            code="toolchain_unsupported",
            message=(
                f"the declared start command head {head!r} is not a supported toolchain "
                "on the neutral base image; the detector will NOT guess a runtime from an "
                "executable name (pnpm/yarn/bun/deno/poetry/uv are not provisioned). "
                "Declare a supported start (node/npm/npx, or a python interpreter / "
                "uvicorn / gunicorn / hypercorn), or vendor the runtime explicitly."
            ),
            field="start_cmd",
            evidence=(f"toolchain evidence: unsupported start head {head!r} (not on allowlist)",),
        )
    if _runtime_from_argv(intent.start_cmd) is RuntimeStrategy.python and not head.startswith(
        "python"
    ):
        if head not in _declared_python_deps(files):
            return _DetectBlocker(
                code="toolchain_unsupported",
                message=(
                    f"the declared python start executable {head!r} is absent from the "
                    "declared dependencies (requirements.txt / pyproject.toml), so the "
                    "base image cannot run it; declare it as a dependency."
                ),
                field="start_cmd",
                evidence=(f"toolchain evidence: python start executable {head!r} not declared",),
            )
    return _declared_manager_blocker(intent)


def _command_grammar_blocker(intent: ReleaseIntent) -> _DetectBlocker | None:
    """A `toolchain_unsupported` fail-closed outcome when a DECLARED start/build command
    does not parse through the runtime grammar (WO-C5 #2 F1).

    The `release_declare` TOOL already gates every declaration through
    `check_declaration_argv`, but a raw `release-intent.json` reaches emission via this
    path without the tool. This applies the SAME grammar here — authoritative and
    independent of the declaration source, mirroring the build-secret gate C4 added at
    `validate_release` — so a secret CLI form (`--token VALUE` / `--password VALUE` /
    URL userinfo) or any unsupported executable / unknown flag fails closed to
    needs_review and is NEVER lowered into the emitted Dockerfile CMD + release.json.
    The message is value-free: it names the field, never the offending argv (which can
    carry the secret). The full runtime-HEAD grammar gates only the start/build argv — a
    `migrate_cmd` legitimately heads with a non-runtime migration tool (alembic /
    wrangler) — but the head-agnostic credential rails DO apply to the migrate_cmd too
    (`check_no_inline_secret_cli` for a flag secret, `check_no_positional_credential` for
    a bare positional / config-role credential), so no migrate secret ships either.

    POSITIONAL-CREDENTIAL RAIL (G02): a BARE POSITIONAL literal credential
    (`node server.js npm_...`, a `config set //host/:_authToken <literal>`) is REJECTED
    here now — `check_declaration_argv` (start/build) and `check_no_positional_credential`
    (migrate) flag it by credential SHAPE (recognized secret token formats / an opaque
    high-entropy blob) and by the config-set operand ROLE. RESIDUAL INHERENT LIMIT: a
    credential that is genuinely INDISTINGUISHABLE from a benign operand — a low-entropy,
    dotted, or path-shaped literal secret — cannot be told apart from a real command
    argument by shape alone and is still accepted; declaring such a value inline remains a
    caller error the representation cannot detect. Credentials must be passed as a whole
    `${NAME}` reference to a declared env var."""
    declared = frozenset(intent.required_env) | {var.name for var in intent.env} | {intent.port_env}
    for field, argv in (("start_cmd", intent.start_cmd), ("build_cmd", intent.build_cmd)):
        try:
            check_declaration_argv(argv, declared_names=declared, field=field)
        except ValueError:
            return _DetectBlocker(
                code="toolchain_unsupported",
                message=(
                    f"the declared {field} does not parse through the runtime command "
                    "grammar: an accepted command heads with a supported runtime and uses "
                    "known flags only, and a flag value must be a whole ${NAME} reference "
                    "to a declared env var — an unsupported executable, an unknown flag, "
                    "or an inline literal secret value (e.g. '--token VALUE') is rejected "
                    "so it is never lowered into the exported bundle. Reference secrets by "
                    "declared env NAME, never inline."
                ),
                field=field,
                evidence=(f"command-grammar evidence: {field} rejected by the runtime grammar",),
            )
    # R2 (G03): a declared `install_cmd` heads with a NON-runtime-start executable (`pip`,
    # `npm`, `yarn`), so the runtime-HEAD grammar does NOT apply (it would false-reject
    # `pip install`) — but the SAME head-agnostic credential rails DO: an install command
    # is lowered verbatim into the emitted Dockerfile `RUN`, so a positional / config-role
    # / flag literal credential there must fail closed exactly like a migrate_cmd secret.
    try:
        check_no_inline_secret_cli(intent.install_cmd, declared_names=declared, field="install_cmd")
        check_no_positional_credential(
            intent.install_cmd, declared_names=declared, field="install_cmd"
        )
    except ValueError:
        return _DetectBlocker(
            code="toolchain_unsupported",
            message=(
                "the declared install_cmd carries a secret on a credential-bearing flag "
                "(e.g. '--token VALUE') or a bare positional literal credential; an install "
                "credential must be a whole ${NAME} reference to a declared env var, never "
                "an inline literal that would be lowered into the exported Dockerfile RUN."
            ),
            field="install_cmd",
            evidence=("command-grammar evidence: install_cmd carries an inline secret",),
        )
    # WO-C5 #3 F5: a resource `migrate_cmd` is lowered VERBATIM into the compose
    # migrate-service command + serialized into release.json, so an inline secret CLI
    # value there would ship in the bundle. The runtime-head grammar does NOT apply (a
    # migration legitimately heads with alembic / wrangler), but the SAME head-agnostic
    # inline-secret hygiene must — a credential-bearing flag's value must be a whole
    # declared ${NAME} reference, never an inline literal.
    for resource in intent.resources:
        try:
            check_no_inline_secret_cli(
                resource.migrate_cmd, declared_names=declared, field="migrate_cmd"
            )
            # GAP G02 (migrate_cmd): a migration tool runs OUTSIDE the runtime-head
            # grammar, but a bare POSITIONAL literal credential (or a `config set
            # <credential-key> <literal>` role form) in a migrate_cmd is lowered verbatim
            # into the compose migrate-service command + release.json, so it must fail
            # closed here exactly as a start/build positional credential does.
            check_no_positional_credential(
                resource.migrate_cmd, declared_names=declared, field="migrate_cmd"
            )
        except ValueError:
            return _DetectBlocker(
                code="toolchain_unsupported",
                message=(
                    "a resource migrate_cmd carries a secret on a credential-bearing flag "
                    "(e.g. '--token VALUE') or a bare positional literal credential; a "
                    "migration credential must be a whole ${NAME} reference to a declared "
                    "env var, never an inline literal that would be lowered into the "
                    "exported migrate command + release.json."
                ),
                field="migrate_cmd",
                evidence=("command-grammar evidence: migrate_cmd carries an inline secret",),
            )
    return None


def _resource_topology_blocker(resources: tuple[ResourceDecl, ...]) -> _DetectBlocker | None:
    """A `persistent_path_unbackable` fail-closed outcome (R3 / G07) when a declared
    resource persists at a FILESYSTEM-ROOT path — its parent directory is `/` (e.g.
    `/app.db`). A named volume can back a persistent path only by mounting the path's
    containing directory; for a root-level file that directory is `/` itself, and a volume
    mounted at `/` would shadow the whole container root. Such a layout cannot be
    represented portably — the emitter historically fell back to a `/data` mount that does
    NOT contain the file, so the data would live on the container's EPHEMERAL layer while a
    `self_host:true` bundle promised it survives a restart. Fail closed instead,
    instructing the caller to declare the path under a subdirectory (e.g. `/data/app.db`)
    so a volume backs it exactly. `None` when every resource's persistent path sits under a
    real subdirectory. Deterministic: the first offending resource (in declared order)
    names the blocker. Value-free: the message names the RULE, never the offending path."""
    for resource in resources:
        # `local_mount_target` returns the resource's REAL parent directory; a root-level
        # path resolves to `/` — the single unbackable case. Deriving the check from the
        # SAME source of truth the emitter mounts at keeps the two from ever drifting: if a
        # restored `/data` fallback ever made a root path resolve to `/data` again, this
        # would stop firing and the emitted mount would once more not back the file.
        # Module-qualified (not name-imported) so a test double substituted onto
        # `disco.core.release.spec` is honored here too.
        if _spec.local_mount_target(resource) == "/":
            return _DetectBlocker(
                code="persistent_path_unbackable",
                message=(
                    "a declared resource persists at a filesystem-root path whose parent "
                    "directory is '/'; a named volume cannot back it without mounting at the "
                    "container root, so its data would not survive a restart. Declare the "
                    "persistent path under a subdirectory (for example '/data/app.db') so a "
                    "volume can back it exactly."
                ),
                field="persistent_path",
                evidence=(
                    "persistence evidence: a resource persists at an unbackable "
                    "filesystem-root path",
                ),
            )
    return None


def _health_route_present(files: Mapping[str, str | bytes], health_path: str) -> bool:
    """Whether a DECLARED health path corresponds to an explicit route in the source.

    The root `/` is always considered served (any bound web server answers it); a
    non-root path must appear as a quoted route literal somewhere in the (bounded,
    binary-safe) source. This backs `health_path_unresolved`: a declared health path
    with no corresponding route is a broken contract."""
    if health_path == "/":
        return True
    needles = (f'"{health_path}"', f"'{health_path}'")
    for path in sorted(files):
        # Strip comments first: a decoy `// '/healthz'` must not count as a real route.
        text = _strip_comments(_scan_text(files[path]))
        if any(needle in text for needle in needles):
            return True
    return False


def _missing_fields(fields: tuple[str, ...]) -> tuple[MissingField, ...]:
    return tuple(MissingField(field=name, detail=_UNVERIFIABLE[name]) for name in fields)
