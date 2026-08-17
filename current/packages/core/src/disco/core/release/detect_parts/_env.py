"""Env-var signal detection: declared dotenv names, discovered Vite build-time
reads, and the secret/representability blockers + typed-intent env merging
that share this ground.
"""

from __future__ import annotations

from collections.abc import Mapping

from disco.core.release.spec import EnvScope, EnvVarDecl, ReleaseIntent, SecretClass
from pydantic import ValidationError

from ._constants import _DOTENV_NAME_RE, _NPMRC_ENV_REF_RE, _NPMRC_NAME, _VITE_SOURCE_SUFFIXES
from ._database import _classify_secret
from ._js_view import _js_executable_view
from ._markup import _vite_env_reads_from_view, _vite_scannable_source
from ._models import _DetectBlocker
from ._text import _basename, _norm, _scan_text


def _declared_env_names(files: Mapping[str, str | bytes]) -> frozenset[str]:
    """The env NAMES an owner has DECLARED for the workspace, read from any dotenv /
    dev-vars file present (`.env`, `.env.example`, `.dev.vars`, …). NAMES only — the
    values are ignored. A read of one of these names is therefore an INTENDED input,
    not an unresolved one, so it does not fail detection closed."""
    names: set[str] = set()
    for path in files:
        base = _basename(path)
        if base.startswith(".env") or base.startswith(".dev.vars"):
            names.update(_DOTENV_NAME_RE.findall(_scan_text(files[path])))
    return frozenset(names)


def _discovered_build_env_names(files: Mapping[str, str | bytes]) -> set[str]:
    """Executable ``import.meta.env.VITE_<NAME>`` reads discovered in source.

    The scanner regex permits mixed case because environment names are
    case-sensitive, while the lexical view prevents comments, docs, and inert string
    contents from inventing required build inputs.
    """
    names: set[str] = set()
    for path in sorted(files):
        if not _norm(path).lower().endswith(_VITE_SOURCE_SUFFIXES):
            continue
        source = _vite_scannable_source(path, _scan_text(files[path]))
        names.update(_vite_env_reads_from_view(_js_executable_view(source)))
    return names


def _representable_env_name(name: str) -> bool:
    """True iff ``name`` can form a valid ``EnvVarDecl`` (the spec's UPPERCASE env-var
    grammar). A discovered build var that is NOT representable cannot be safely lowered
    to a build arg and must fail CLOSED — never crash the route (C9-05 P3-1)."""
    try:
        EnvVarDecl(name=name, scope=EnvScope.build, required=True, secret=SecretClass.public)
    except ValidationError:
        return False
    return True


def _build_env_decls(files: Mapping[str, str | bytes]) -> tuple[EnvVarDecl, ...]:
    """The BUILD-scope env vars a client build reads, discovered from source (§8.1 /
    §8.2). Currently the Vite `import.meta.env.VITE_<NAME>` form — a compile-time
    constant baked into the built bundle. Each is a `build`-scope `EnvVarDecl`
    (host-derived secret classification, fail-closed), so a public build var lowers to
    a Compose build arg + Dockerfile `ARG` (never a runtime `ENV`), and every
    statically discovered representable NAME is conserved in the spec rather than
    silently dropped. UNREPRESENTABLE names (mixed-case) are excluded here and are
    caught separately by `_unrepresentable_build_env_blocker` so model validation can
    never escape as a 500 (C9-05 P3-1).

    Bounded + binary-safe (§7.11): reads only `_scan_text` views, in a stable order."""
    return tuple(
        EnvVarDecl(
            name=name,
            scope=EnvScope.build,
            required=True,
            secret=_classify_secret(name),
        )
        for name in sorted(_discovered_build_env_names(files))
        if _representable_env_name(name)
    )


def _unrepresentable_build_env_blocker(
    files: Mapping[str, str | bytes],
) -> _DetectBlocker | None:
    """Fail closed when a discovered build var name cannot be represented as an env-var
    declaration (a mixed-case ``import.meta.env.VITE_*``): it is a real case-sensitive
    build-time value we cannot lower to a build arg, so accepting the bundle would ship
    a built asset silently missing it. A typed blocker — NOT an uncaught 500 (C9-05
    P3-1)."""
    bad = sorted(n for n in _discovered_build_env_names(files) if not _representable_env_name(n))
    if not bad:
        return None
    return _DetectBlocker(
        code="build_env_unsupported_name",
        message=(
            f"the source reads build-time client env var(s) {bad} whose name(s) are not "
            "representable as an env-var declaration (the platform requires UPPERCASE "
            "env-var names). Such a build value cannot be lowered to a build arg, so the "
            "built asset would silently lack it. Rename the variable(s) to an UPPERCASE "
            "name."
        ),
        field="env",
        evidence=(f"build-env name evidence: unrepresentable discovered build var(s) {bad}",),
    )


def _secret_build_blocker(files: Mapping[str, str | bytes]) -> _DetectBlocker | None:
    """A `secret_build_env_unsupported` fail-closed outcome when the install step needs
    a BUILD-time SECRET — an `.npmrc` referencing a secret-shaped `${NAME}` auth token
    read at `npm ci` time (§8.4). A secret-free bundle cannot carry that value, and a
    plain build `ARG` would leak it into image history, so detection fails closed
    rather than shipping a bundle whose build cannot authenticate. `None` when no
    secret-shaped build reference is found."""
    for path in sorted(files):
        if _basename(path) != _NPMRC_NAME:
            continue
        for name in _NPMRC_ENV_REF_RE.findall(_scan_text(files[path])):
            if _classify_secret(name) is SecretClass.secret:
                return _DetectBlocker(
                    code="secret_build_env_unsupported",
                    message=(
                        "the build reads a secret-shaped credential from an .npmrc at "
                        "install time; a secret-free self-host bundle cannot supply a "
                        "build-time secret (a plain build ARG would leak it into image "
                        "history), so this build is not supported. Remove the "
                        "build-time credential or vendor its dependencies."
                    ),
                    field="build_env",
                    evidence=(
                        f"build-secret evidence: secret-shaped .npmrc reference in {_norm(path)}",
                    ),
                )
    return None


def _secret_build_env_blocker(files: Mapping[str, str | bytes]) -> _DetectBlocker | None:
    """A `secret_build_env_unsupported` fail-closed outcome when a SOURCE-DISCOVERED
    build-scope env var is secret-classed — e.g. a Vite client bundle reading
    `import.meta.env.VITE_API_TOKEN` (a secret-shaped name). Such a value would be
    baked into the PUBLIC build output, and a secret-free self-host bundle cannot
    supply a build-time secret, so it fails closed EXACTLY like a secret `.npmrc`
    reference (§8.4) — never folded into a plain build arg while shipping a
    self-hostable candidate. This closes the discovery-source gap: the contract is the
    same whether the secret build var comes from an `.npmrc` OR from application
    source. `None` when every discovered build var is public. Deterministic: the first
    secret build var in sorted order (via `_build_env_decls`) names the blocker."""
    for decl in _build_env_decls(files):
        if decl.secret is SecretClass.secret:
            return _DetectBlocker(
                code="secret_build_env_unsupported",
                message=(
                    f"the client build reads a secret-shaped build variable {decl.name!r} "
                    "from application source; it would be baked into the public build "
                    "output, and a secret-free self-host bundle cannot supply a build-time "
                    "secret (a plain build arg would leak it), so this build is not "
                    "supported. Rename it to a non-secret public build var, or remove the "
                    "build-time secret dependency."
                ),
                field="build_env",
                evidence=(
                    f"build-secret evidence: source-discovered secret build var {decl.name!r}",
                ),
            )
    return None


def _intent_env_result(intent: ReleaseIntent) -> tuple[EnvVarDecl, ...] | _DetectBlocker:
    """Merge the intent's `required_env` shorthand and its typed `env` declarations into
    the candidate's env set (R2 / G03), or a `_DetectBlocker` on a build-scope secret.

    `required_env` names become runtime/required decls (secret-SHAPE classified). A typed
    `env` decl carries its own scope / requiredness / secret class and, for a name in
    both, WINS — except a secret-SHAPED name is never DOWNGRADED to public (fail-closed).
    A BUILD-scope secret decl fails closed with `secret_build_env_unsupported`, exactly as
    a source-discovered secret build var does (a secret-free bundle cannot supply a
    build-time secret). Deterministic: `required_env` order, then first-seen `env` order."""
    by_name: dict[str, EnvVarDecl] = {}
    order: list[str] = []
    for name in intent.required_env:
        by_name[name] = EnvVarDecl(
            name=name, scope=EnvScope.runtime, required=True, secret=_classify_secret(name)
        )
        order.append(name)
    for decl in intent.env:
        secret = decl.secret
        if _classify_secret(decl.name) is SecretClass.secret:
            secret = SecretClass.secret  # fail-closed: never downgrade a secret-shaped name
        if decl.scope is EnvScope.build and secret is SecretClass.secret:
            return _DetectBlocker(
                code="secret_build_env_unsupported",
                message=(
                    f"the declared build-scope env var {decl.name!r} is secret; a "
                    "secret-free self-host bundle cannot supply a build-time secret (a "
                    "build arg would leak it into image build history), so this build is "
                    "not supported. Declare it as a runtime var, rename it to a public "
                    "build var, or remove the build-time secret dependency."
                ),
                field="build_env",
                evidence=(f"build-secret evidence: declared secret build var {decl.name!r}",),
            )
        if decl.name not in by_name:
            order.append(decl.name)
        by_name[decl.name] = EnvVarDecl(
            name=decl.name,
            scope=decl.scope,
            required=decl.required,
            secret=secret,
            binding=decl.binding,
            consumers=decl.consumers,
        )
    return tuple(by_name[name] for name in order)


def _intent_build_env(
    env: tuple[EnvVarDecl, ...],
    intent: ReleaseIntent,
    files: Mapping[str, str | bytes],
) -> tuple[EnvVarDecl, ...] | _DetectBlocker:
    """Source build-env parity for the typed-intent paths (R7 live-pubvite defect;
    C9-05 scope-conflict correction).

    The source-detection path conserves SOURCE-DISCOVERED public client build vars
    (`import.meta.env.VITE_*`) into the spec for a build-requiring service — they must
    lower to a Compose build arg + Dockerfile ARG or the built asset ships without
    them — and fails closed on a secret-SHAPED discovered build var
    (`secret_build_env_unsupported`, §8.4).

    When source PROVES a public var is read at BUILD time, a same-name declaration
    must not silently suppress that build lowering:

    * name NOT declared -> conserve the discovered BUILD-scope decl (it lowers to an
      ARG);
    * name declared with BUILD scope -> the explicit build-scope decl already lowers;
      keep it, don't duplicate;
    * name declared with RUNTIME scope (an explicit runtime decl, or the
      `required_env` shorthand which is runtime-scoped) -> SCOPE CONFLICT: the current
      schema has no dual-scope decl, and Compose lowers only build-scope decls to build
      args, so trusting the runtime declaration would ship a candidate whose built
      asset silently lacks the value. Fail closed with `build_env_scope_conflict`."""
    if not intent.build_cmd:
        return env
    unrepresentable = _unrepresentable_build_env_blocker(files)
    if unrepresentable is not None:
        return unrepresentable
    secret = _secret_build_env_blocker(files)
    if secret is not None:
        return secret
    by_name = {var.name: var for var in env}
    additions: list[EnvVarDecl] = []
    for decl in _build_env_decls(files):
        existing = by_name.get(decl.name)
        if existing is None:
            additions.append(decl)
        elif existing.scope is EnvScope.build:
            continue  # explicit build-scope declaration already lowers; no duplicate
        else:
            return _DetectBlocker(
                code="build_env_scope_conflict",
                message=(
                    f"the build variable {decl.name!r} is read at BUILD time in the "
                    "source (a Vite `import.meta.env` client build var), but it is "
                    "declared as a RUNTIME env var. A runtime declaration lowers to a "
                    "container ENV, never a build arg, so the built asset would silently "
                    "lack the value. Declare it with build scope (or, if it is genuinely "
                    "needed at both build and runtime, split it into a build-scoped and "
                    "a runtime-scoped declaration)."
                ),
                field="env",
                evidence=(
                    f"build-env scope evidence: {decl.name!r} read at build time but "
                    f"declared scope={existing.scope.value}",
                ),
            )
    return env + tuple(additions)
