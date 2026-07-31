"""Node runtime detection: package.json/lockfile reading, the node ingress
detector, and the package-manager/toolchain-support blockers it shares with
the static and python detectors.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from disco.core.release.spec import ReleaseService, RuntimeStrategy, ServiceRole

from ._constants import (
    _ENV_ASSIGN_RE,
    _INGRESS_ID,
    _JS_CATCHALL_RE,
    _JS_ENV_DOT_RE,
    _JS_ENV_DYNAMIC_RE,
    _JS_ENV_STR_RE,
    _JS_ROOT_ROUTE_RE,
    _NODE_LOCKFILES,
    _NODE_WORKSPACE_MARKERS,
    _SCRIPT_HEAD_FAMILY,
    _SHELL_CHAIN_RE,
    _SUPPORTED_NODE_HEADS,
    _SUPPORTED_PY_SERVER_HEADS,
    _TOOLCHAIN_SCRIPT_KEYS,
    _TRANSPARENT_WRAPPERS,
    _UNSUPPORTED_NODE_PM,
    _UNSUPPORTED_NODE_PM_NAMES,
)
from ._env import _declared_env_names, _secret_build_blocker, _secret_build_env_blocker
from ._models import _DetectBlocker
from ._text import _as_text, _norm, _scan_text, _strip_comments


def _root_package_json(files: Mapping[str, str | bytes]) -> object:
    """Parse a root `package.json` (Any laundered to `object`), or `None`."""
    for path in files:
        if _norm(path) == "package.json":
            try:
                return json.loads(_as_text(files[path]))
            except (json.JSONDecodeError, ValueError):
                return None
    return None


def _script(pkg: object, name: str) -> str | None:
    """A non-empty string `scripts.<name>` from a parsed package.json, else None."""
    if not isinstance(pkg, dict):
        return None
    scripts = pkg.get("scripts")
    if not isinstance(scripts, dict):
        return None
    value = scripts.get(name)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _node_install(files: Mapping[str, str | bytes]) -> tuple[str | None, str, tuple[str, ...]]:
    """(lockfile, package_manager, install argv) inferred from the root lockfile."""
    roots = {_norm(p) for p in files if "/" not in _norm(p)}
    if "pnpm-lock.yaml" in roots:
        return ("pnpm-lock.yaml", "pnpm", ("pnpm", "install", "--frozen-lockfile"))
    if "yarn.lock" in roots:
        return ("yarn.lock", "yarn", ("yarn", "install", "--frozen-lockfile"))
    if "package-lock.json" in roots:
        return ("package-lock.json", "npm", ("npm", "ci"))
    return (None, "npm", ("npm", "install"))


def _node_source(files: Mapping[str, str | bytes]) -> str:
    """The bounded, binary-safe concatenation of the workspace's scannable content —
    where `process.env` reads and route registrations live. Each file is capped and
    binary files are skipped (§7.11), so an out-of-bounds file injects no signal."""
    return "\n".join(_scan_text(files[path]) for path in sorted(files))


def _js_env_reads(source: str) -> frozenset[str]:
    """The env NAMES read via a DIRECT `process.env.NAME` / `process.env['NAME']`
    form in JS/TS source."""
    return frozenset(_JS_ENV_DOT_RE.findall(source)) | frozenset(_JS_ENV_STR_RE.findall(source))


def _js_has_dynamic_env(source: str) -> bool:
    """Whether the source reads env through a DYNAMIC computed index
    (`process.env[name]`) whose NAME cannot be resolved statically."""
    return _JS_ENV_DYNAMIC_RE.search(source) is not None


def _js_references_port(source: str, port_env: str) -> bool:
    """Whether the source binds the `$PORT` contract — a `process.env.<port_env>`
    (or quoted-bracket) read. A server that binds only a literal port never honors
    the host's `$PORT` and fails the port contract."""
    pattern = re.compile(
        r"process\.env(?:\." + re.escape(port_env) + r"\b"
        r"|\[\s*['\"]" + re.escape(port_env) + r"['\"]\s*\])"
    )
    # Strip comments first: a decoy `// … process.env.PORT` must not satisfy the bind.
    return pattern.search(_strip_comments(source)) is not None


def _js_serves_root(source: str) -> bool:
    """Whether a node server POSITIVELY serves `GET /` — a raw `http.createServer`
    catch-all handler, or an explicit root route registration. Used to establish
    the `/` health contract rather than assuming it."""
    return (
        _JS_CATCHALL_RE.search(source) is not None or _JS_ROOT_ROUTE_RE.search(source) is not None
    )


def _lockfile_conflict(files: Mapping[str, str | bytes]) -> _DetectBlocker | None:
    """A `package_manager_conflict` fail-closed outcome when the ROOT declares two or
    more node lockfiles (e.g. `package-lock.json` AND `yarn.lock`): two package
    managers, an irreconcilable disagreement. Resolving it by precedence would
    silently install the wrong dependency graph, so detection fails closed (§8.9).
    `None` when at most one lockfile is present."""
    roots = {_norm(p) for p in files if "/" not in _norm(p)}
    present = [name for name in _NODE_LOCKFILES if name in roots]
    if len(present) < 2:
        return None
    return _DetectBlocker(
        code="package_manager_conflict",
        message=(
            "the project declares two or more package-manager lockfiles ("
            + ", ".join(present)
            + "), an irreconcilable disagreement about which package manager builds "
            "the app; this is NOT resolved by precedence — remove all but one lockfile "
            "or declare the package manager via a typed release intent."
        ),
        field="package_manager",
        evidence=(f"lockfile conflict: {', '.join(present)}",),
    )


def _unsupported_pm_blocker(
    files: Mapping[str, str | bytes], table: Mapping[str, str]
) -> _DetectBlocker | None:
    """A `toolchain_unsupported` fail-closed outcome when the ROOT declares a
    package-manager lockfile naming a toolchain the neutral base image does NOT
    provision (bun/pnpm/yarn for node, poetry/uv for python — `table` selects which).
    The lockfile is NOT silently mapped onto npm/pip (§8.8); its presence rejects the
    release with the exact typed code so the defect is diagnosable. `None` when no such
    lockfile is present. Deterministic: the first matching lockfile in sorted order
    names the blocker."""
    roots = {_norm(p) for p in files if "/" not in _norm(p)}
    for lockfile in sorted(table):
        if lockfile in roots:
            manager = table[lockfile]
            return _DetectBlocker(
                code="toolchain_unsupported",
                message=(
                    f"the project declares a {lockfile!r} lockfile, which indicates the "
                    f"{manager!r} package manager; that toolchain is NOT provisioned in "
                    "the neutral base image and the detector will NOT silently map it "
                    "onto npm/pip. Vendor the toolchain explicitly, or use npm "
                    "(package-lock.json) / pip (requirements.txt)."
                ),
                field="package_manager",
                evidence=(
                    f"toolchain evidence: {lockfile} indicates unsupported package "
                    f"manager {manager!r} (not on the base image)",
                ),
            )
    return None


def _package_manager_field(pkg: object) -> str | None:
    """The corepack `packageManager` manager NAME declared in a parsed `package.json`
    (the part before `@`), lowercased — `"pnpm@8.0.0"` -> `"pnpm"`. `None` when the
    field is absent or not a non-empty string. This is an EXPLICIT, authoritative
    declaration of which package manager builds the app."""
    if not isinstance(pkg, dict):
        return None
    value = pkg.get("packageManager")
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip().split("@", 1)[0].strip().lower()
    return name or None


def _node_toolchain_blocker(manager: str, why: str, evidence: str) -> _DetectBlocker:
    """A `toolchain_unsupported` fail-closed outcome for an authoritative non-npm
    package-manager declaration (`why` states which signal named `manager`)."""
    return _DetectBlocker(
        code="toolchain_unsupported",
        message=(
            f"{why}; the {manager!r} package manager is NOT provisioned in the neutral "
            "base image and the detector will NOT silently map it onto npm. Vendor the "
            "toolchain explicitly, or use npm (package-lock.json)."
        ),
        field="package_manager",
        evidence=(evidence,),
    )


def _effective_head(sub_command: str) -> str | None:
    """The effective interpreter head of ONE shell sub-command: the first token that is
    neither a leading `NAME=VALUE` env-assignment nor a TRANSPARENT-PREFIX wrapper
    (`_TRANSPARENT_WRAPPERS` — `corepack`/`cross-env`/`env`/`exec`/`dotenv`, each of which
    execs the REST of the line), path-stripped and lowercased (`/usr/bin/bunx` -> `bunx`).

    Env-assignment and wrapper stripping REPEAT, so a stacked/prefixed form resolves to the
    real command — `cross-env FOO=1 bunx x` and `env cross-env bunx x` both yield `bunx`; a
    single `--` argv separator after a wrapper (`dotenv -- bunx x`) is skipped. BOUNDED: it
    does NOT skip arbitrary `-flag` / value-arg forms (`dotenv -e .env`, `nice -n 10`), which
    remain the documented opaque-wrapper ceiling. `None` when nothing but env-assignments /
    bare wrappers remains."""
    tokens = sub_command.split()
    idx = 0
    while True:
        while idx < len(tokens) and _ENV_ASSIGN_RE.match(tokens[idx]):
            idx += 1
        if idx >= len(tokens):
            return None
        head = tokens[idx].rsplit("/", 1)[-1].lower()
        if head not in _TRANSPARENT_WRAPPERS:
            return head
        idx += 1
        if idx < len(tokens) and tokens[idx] == "--":
            idx += 1


def _is_supported_toolchain_head(head: str) -> bool:
    """Whether a start-command interpreter head is a runtime the base images run."""
    return (
        head in _SUPPORTED_NODE_HEADS
        or head in _SUPPORTED_PY_SERVER_HEADS
        or head.startswith("python")
    )


# ACCEPTED HEURISTIC CEILING (closeout #2e) — `_launcher_family_in_script` resolves DIRECT,
# env-prefixed, shell-chained, and transparent-wrapper (`_TRANSPARENT_WRAPPERS`) launcher
# heads. It DELIBERATELY does NOT recover a launcher buried by an opaque or quoted
# sub-grammar — the static-parse ceiling of shell, and not a shape a real production start
# script uses:
#   * a quoted shell string:               `sh -c "bunx x"`, `bash -lc "bunx x"`
#   * a task runner exec'ing a quoted arg: `concurrently "bunx x"`, `npm-run-all -p bunx:*`
#   * command substitution:                `$(bunx x)`, backticks
#   * a quoted command head:               `"bunx" vite`
#   * a flag-argument wrapper:             `nice -n 10 bunx x`, `time bunx x`, `xargs bunx`,
#                                          `dotenv -e .env bunx x`
# Recovering these needs a real shell parser (quote / word-splitting / substitution grammar).
# The reject line is drawn at direct / env-prefixed / chained / transparent-wrapper launcher
# heads; wrapping a launcher any deeper is not a shape a real npm start script emits.
def _launcher_family_in_script(script: str) -> tuple[str, str] | None:
    """The first `(head, family)` non-npm launcher match across a script string's shell
    sub-commands, else `None` (closeout #2d — robust against a launcher hidden behind an env
    prefix, a shell chain, or a corepack wrapper).

    Splits the string on shell operators, derives each sub-command's effective head
    (`_effective_head` strips env-assignment / `corepack` prefixes), and matches it against
    `_SCRIPT_HEAD_FAMILY`. So `NODE_ENV=production bunx vite`, `cd app && bunx vite`,
    `true ; pnpm dev`, and `corepack pnpm start` are all caught — while a launcher name that
    appears only as a non-head ARGUMENT (`echo "use bun" && node x`) is NOT, since only a
    command HEAD names a runtime. Deterministic: the first launcher in sub-command order."""
    for sub_command in _SHELL_CHAIN_RE.split(script):
        head = _effective_head(sub_command)
        if head is not None:
            family = _SCRIPT_HEAD_FAMILY.get(head)
            if family is not None:
                return (head, family)
    return None


def _has_authoritative_npm_signal(files: Mapping[str, str | bytes], pkg: object) -> bool:
    """Whether the tree carries an AUTHORITATIVE declaration that npm — the ONE node
    manager the neutral base image provisions — builds the app: a committed ROOT
    `package-lock.json`, OR a corepack `packageManager:"npm@…"` field. When the owner has
    authoritatively named npm, a co-present STRAY PASSIVE config marker (a leftover
    `.yarnrc` / `pnpm-workspace.yaml` / `bunfig.toml` from a yarn/pnpm/bun→npm migration) is
    a migration remnant, not a live toolchain declaration — npm wins over it (closeout #1).
    This override is SCOPED to passive markers: it does NOT reach an ACTIVE launcher in a
    script head (closeout #2c), which is runtime-authoritative and rejects regardless. A
    committed non-npm LOCKFILE is a STRONGER signal already rejected upstream
    (`_lockfile_conflict` / `_unsupported_pm_blocker`) before this runs, so this precedence
    never masks a real committed-lockfile disagreement."""
    roots = {_norm(p) for p in files if "/" not in _norm(p)}
    if "package-lock.json" in roots:
        return True
    return _package_manager_field(pkg) == "npm"


def _unsupported_node_pm_declaration(
    files: Mapping[str, str | bytes], pkg: object
) -> _DetectBlocker | None:
    """A `toolchain_unsupported` fail-closed outcome when an AUTHORITATIVE package-manager
    declaration OTHER than a committed lockfile names a node toolchain the neutral base
    image does not provision — closing the no-lockfile bypass (§8.8). Signals, checked in
    a stable order so the finding is deterministic:

    1. the corepack `package.json` `"packageManager"` field naming bun/pnpm/yarn — the
       MOST authoritative statement of which manager builds the app; mapping it onto npm
       is exactly the §8.8 failure, so it rejects even if a (stale) `package-lock.json` is
       also present.
    2. a PASSIVE non-npm workspace/config MARKER at the ROOT (`pnpm-workspace.yaml`,
       `.yarnrc(.yml)`, `bunfig.toml`) — UNLESS an authoritative npm signal is present (a
       committed `package-lock.json` OR `packageManager:"npm@…"`), in which case the marker
       is treated as a migration leftover and npm WINS (closeout #1): the project stays a
       candidate.
    3. an ACTIVE launcher in a `start`/`build`/`prebuild`/`postbuild` script HEAD whose
       interpreter is a bun/pnpm/yarn launcher FAMILY — the bare manager OR its
       `x`-suffixed runner (`bunx`, `pnpx`) (closeout #2). This is RUNTIME-authoritative
       and rejects UNCONDITIONALLY — even beside an authoritative npm signal (closeout
       #2c): a script that literally invokes bun/pnpm/yarn genuinely needs that runtime, so
       silently lowering it to `npm start` would ship a broken bundle.

    npm never trips this (a `package-lock.json`, `packageManager:"npm@…"`, a
    `node`/`npm`/`npx` script head, or no PM signal at all), so a plain npm project —
    including one carrying a stray PASSIVE marker alongside an authoritative npm signal — is
    never mis-flagged. `None` when no unsupported declaration wins. Keyed on ROOT markers
    only (a nested `.yarnrc` in a vendored dep never triggers it)."""
    manager = _package_manager_field(pkg)
    if manager is not None and manager in _UNSUPPORTED_NODE_PM_NAMES:
        return _node_toolchain_blocker(
            manager,
            f'the package.json "packageManager" field declares {manager!r}',
            f"toolchain evidence: packageManager field names unsupported manager {manager!r} "
            "(no lockfile required)",
        )
    # closeout #1: an authoritative npm signal overrides a co-present STRAY PASSIVE config
    # marker (a migration leftover) — the base image runs npm, so a leftover non-npm config
    # file is not a reject. Scoped to the MARKER loop only: it does NOT reach the ACTIVE
    # launcher script head below (closeout #2c).
    if not _has_authoritative_npm_signal(files, pkg):
        roots = {_norm(p) for p in files if "/" not in _norm(p)}
        for marker in sorted(_NODE_WORKSPACE_MARKERS):
            if marker in roots:
                mgr = _NODE_WORKSPACE_MARKERS[marker]
                return _node_toolchain_blocker(
                    mgr,
                    f"the project declares a {marker!r} workspace/config marker",
                    f"toolchain evidence: {marker} indicates unsupported manager {mgr!r} "
                    "(no lockfile required)",
                )
    # closeout #2c: an ACTIVE launcher in a script head is RUNTIME-authoritative and rejects
    # UNCONDITIONALLY — even beside a committed `package-lock.json` / `packageManager:"npm@…"`.
    # The app literally invokes bun/pnpm/yarn, so lowering it to `npm start` would ship a
    # broken bundle; an npm lockfile does not override a live launcher.
    for script_name in _TOOLCHAIN_SCRIPT_KEYS:
        script = _script(pkg, script_name)
        if script is None:
            continue
        match = _launcher_family_in_script(script)
        if match is not None:
            head, family = match
            return _node_toolchain_blocker(
                family,
                f"the {script_name!r} script invokes the {family!r} package manager "
                f"(via the {head!r} launcher)",
                f"toolchain evidence: {script_name} script head {head!r} is an unsupported "
                f"manager {family!r} (no lockfile required)",
            )
    return None


def _node_detect(
    files: Mapping[str, str | bytes],
) -> ReleaseService | _DetectBlocker | None:
    """Detect a node ingress from a root `package.json` server `start` script.

    Returns `None` when there is no node server signature; a `ReleaseService` for a
    resolved candidate (exact `$PORT` env use, established `GET /` health when the
    server serves root); or a `_DetectBlocker` when the signature is present but the
    contract is predictably broken — a DYNAMIC/undeclared env read
    (`required_env_unresolved`) or a server that never binds `$PORT`
    (`port_contract_unresolved`)."""
    pkg = _root_package_json(files)
    if _script(pkg, "start") is None:
        return None  # no node server signature — not this runtime

    source = _node_source(files)
    port_env = "PORT"
    declared = _declared_env_names(files) | {port_env}

    if _js_has_dynamic_env(source):
        return _DetectBlocker(
            code="required_env_unresolved",
            message=(
                "the node server reads an environment variable through a dynamic "
                "`process.env[<expr>]` index whose name cannot be resolved statically; "
                "declare the required env names in a typed release intent."
            ),
            field="required_env",
            evidence=("node env read: dynamic process.env[<expr>] index",),
        )
    undeclared = sorted(_js_env_reads(source) - declared)
    if undeclared:
        return _DetectBlocker(
            code="required_env_unresolved",
            message=(
                "the node server reads environment variable(s) "
                f"{', '.join(undeclared)} that are neither the $PORT contract nor "
                "declared in a dotenv template or typed intent; requiredness cannot "
                "be established statically — declare it."
            ),
            field="required_env",
            evidence=(f"node env read: undeclared {', '.join(undeclared)}",),
        )
    if not _js_references_port(source, port_env):
        return _DetectBlocker(
            code="port_contract_unresolved",
            message=(
                "the node server does not bind the $PORT contract (no "
                "`process.env.PORT` read); it cannot honor the host-assigned port. "
                "Bind `process.env.PORT` or declare the port via a typed intent."
            ),
            field="port_env",
            evidence=("node port evidence: no process.env.PORT read (literal port only)",),
        )
    # A package-manager disagreement, an unsupported toolchain, or a build-time secret
    # is unreleasable — fail closed BEFORE emitting a candidate whose install step is
    # ambiguous, cannot run on the base image, or cannot authenticate (§8.9 / §8.8 /
    # §8.4).
    conflict = _lockfile_conflict(files)
    if conflict is not None:
        return conflict
    toolchain = _unsupported_pm_blocker(files, _UNSUPPORTED_NODE_PM)
    if toolchain is not None:
        return toolchain
    # An authoritative non-lockfile PM declaration (a `packageManager` field, a
    # workspace/config marker, or a bun/pnpm/yarn script head) rejects the release too —
    # closing the no-lockfile bypass. Ordered AFTER the two committed-lockfile checks so a
    # genuine two-lockfile disagreement still fails closed as `package_manager_conflict`.
    declared_toolchain = _unsupported_node_pm_declaration(files, pkg)
    if declared_toolchain is not None:
        return declared_toolchain
    build_secret = _secret_build_blocker(files)
    if build_secret is not None:
        return build_secret

    build_cmd = ("npm", "run", "build") if _script(pkg, "build") is not None else ()
    # A build step that reads a SOURCE-DISCOVERED secret build var fails closed like an
    # `.npmrc` build secret (§8.4) — a secret build var must never fold into build.args.
    if build_cmd:
        source_secret = _secret_build_env_blocker(files)
        if source_secret is not None:
            return source_secret
    lockfile, manager, install = _node_install(files)
    return ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.node,
        package_manager=manager,
        lockfile=lockfile,
        install_cmd=install,
        build_cmd=build_cmd,
        start_cmd=("npm", "start"),
        port_env=port_env,
        health_path="/" if _js_serves_root(source) else None,
    )


def _node_has_dependencies(pkg: object) -> bool:
    """Whether a parsed root `package.json` declares ANY dependencies (runtime, dev,
    optional, or peer). A no-build interpreted node app WITH declared dependencies must
    install them for its image to run (R2 / G04); a package.json with none has nothing
    to install, so no install layer is derived."""
    if not isinstance(pkg, dict):
        return False
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        value = pkg.get(key)
        if isinstance(value, dict) and value:
            return True
    return False
