"""The LIGHT validation plane — is a `ReleaseSpec` releasable from spec + tree?

This is the FINISH-SAFE half of the release pipeline: a set of PURE functions that
decide "can this be released" using only the immutable `ReleaseSpec` and a flat
view of the workspace file tree (path -> size/content). It never spawns a process,
never talks to a container engine, never touches the network or disk. That
constraint is load-bearing: the light plane runs on the finish path where a hang
or a heavy dependency would be fatal, so it can NEVER grow one — a CI grep over
this file proves the container-build words are absent.

Fail-closed by construction: every problem is reported as DATA — a `Blocker` in a
`ValidationResult` — never raised. A caller reads `result.ok` (and, when it is
`False`, the typed `blockers`) and decides; validation itself does not throw for a
merely-unreleasable spec. The checks are defense-in-depth: several re-verify
invariants the `ReleaseSpec` validators already enforce (e.g. exactly one ingress)
so a spec built through a non-validating path (`model_construct` / `model_copy`)
is still judged honestly here.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from enum import Enum

from disco.core.release.spec import EnvScope, ReleaseSpec, SecretClass, ServiceRole
from disco.core.secret_paths import is_runtime_secret_path
from pydantic import BaseModel, ConfigDict, Field, computed_field

# A shell-style variable reference — braced (`${NAME}`) or bare (`$NAME`). Used to
# find the env vars a command/resource depends on, and (in the leak check) to
# strip legitimate references out before hunting for a bare secret NAME literal.
_VAR_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


class BlockerCode(str, Enum):
    """The closed vocabulary of reasons a release can be blocked. A stable code
    (not a free-form string) so callers can branch on the KIND of problem."""

    missing_path = "missing_path"
    ingress_count = "ingress_count"
    undeclared_env_var = "undeclared_env_var"
    secret_file_present = "secret_file_present"
    secret_name_leaked = "secret_name_leaked"
    secret_build_env_unsupported = "secret_build_env_unsupported"


class Blocker(BaseModel):
    """One reason a spec is not releasable, as DATA. Immutable: a blocker is a
    finding, not a mutable accumulator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: BlockerCode
    message: str
    # The tree/spec path the finding is about, when there is a single one (a
    # missing root, a secret file); `None` for whole-spec findings.
    path: str | None = None


class ValidationResult(BaseModel):
    """The verdict of `validate_release`: the (possibly empty) list of blockers,
    plus a derived `ok`. `ok` is a computed field over `blockers`, so the two can
    never disagree — a result is releasable IFF it carries no blockers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    blockers: list[Blocker] = Field(default_factory=list)

    @computed_field
    @property
    def ok(self) -> bool:
        return not self.blockers


# ---- path helpers -------------------------------------------------------------


def _norm(path: str) -> str:
    """Normalize a workspace path for tree comparison: trim, drop a leading `./`,
    drop a trailing `/`."""
    p = path.strip()
    if p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def _tree_paths(files: Mapping[str, int | bytes]) -> frozenset[str]:
    return frozenset(_norm(p) for p in files)


def _dir_present(tree: frozenset[str], path: str) -> bool:
    """Whether `path` names something in the tree: the workspace root, an exact
    entry, or a directory prefix of at least one entry."""
    n = _norm(path)
    if n in ("", "."):
        return True
    if n in tree:
        return True
    prefix = n + "/"
    return any(entry.startswith(prefix) for entry in tree)


def _file_present(tree: frozenset[str], root: str, path: str) -> bool:
    """Whether a referenced file exists — checked both workspace-relative and
    joined under the service `root` (a lockfile may be recorded either way)."""
    candidates = {_norm(path)}
    root_n = _norm(root)
    if root_n not in ("", "."):
        candidates.add(_norm(f"{root_n}/{_norm(path)}"))
    return any(candidate in tree for candidate in candidates)


# ---- string scanning ----------------------------------------------------------


def _var_refs(text: str) -> Iterator[str]:
    """The variable NAMES referenced (`${NAME}` / `$NAME`) inside a string."""
    for match in _VAR_REF_RE.finditer(text):
        yield match.group(1) or match.group(2)


def _bare_name_present(text: str, name: str) -> bool:
    """Whether `name` occurs as a standalone token in `text` (not as a fragment of
    a longer identifier). Callers strip `${...}` references first, so a hit here
    means the bare NAME was used as a literal value."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def _command_and_resource_strings(spec: ReleaseSpec) -> Iterator[tuple[str, str]]:
    """`(label, text)` for every string a `${VAR}` can legitimately appear in: the
    service command argv tokens, and the resource migrate argv / url / path."""
    for service in spec.services:
        for field_name in ("install_cmd", "build_cmd", "migrate_cmd", "start_cmd"):
            for token in getattr(service, field_name):
                yield (f"service {service.id!r} {field_name}", token)
    for resource in spec.resources:
        for token in resource.migrate_cmd:
            yield (f"resource {resource.id!r} migrate_cmd", token)
        yield (f"resource {resource.id!r} url", resource.profiles.local.url)
        yield (f"resource {resource.id!r} persistent_path", resource.persistent_path)


def _leak_scan_strings(spec: ReleaseSpec) -> Iterator[tuple[str, str]]:
    """`(label, text)` for every free-form VALUE string a secret name could leak
    into. Deliberately EXCLUDES the `EnvVarDecl` declarations themselves (a secret
    name lives there by design) and structural identifiers (service/resource ids,
    depends_on/consumers, port_env)."""
    yield ("spec.kind", spec.kind)
    yield ("spec.name", spec.name)
    for target in spec.targets:
        yield ("spec.targets", target)
    for service in spec.services:
        for field_name in ("root", "package_manager", "lockfile", "output_dir", "health_path"):
            value = getattr(service, field_name)
            if value is not None:
                yield (f"service {service.id!r} {field_name}", value)
        for field_name in ("install_cmd", "build_cmd", "migrate_cmd", "start_cmd"):
            for token in getattr(service, field_name):
                yield (f"service {service.id!r} {field_name}", token)
    for resource in spec.resources:
        yield (f"resource {resource.id!r} persistent_path", resource.persistent_path)
        yield (f"resource {resource.id!r} url", resource.profiles.local.url)
        yield (f"resource {resource.id!r} volume", resource.profiles.local.volume)
        for token in resource.migrate_cmd:
            yield (f"resource {resource.id!r} migrate_cmd", token)
    yield ("provenance.detector", spec.provenance.detector)
    yield ("provenance.detector_version", spec.provenance.detector_version)
    if spec.provenance.reason is not None:
        yield ("provenance.reason", spec.provenance.reason)
    for signal in spec.provenance.evidence:
        yield ("provenance.evidence", signal)
    for ref in spec.generated_files:
        yield (f"generated_file {ref.path!r} path", ref.path)
        yield (f"generated_file {ref.path!r} purpose", ref.purpose)


# ---- the individual checks (each appends blockers; none raise) ----------------


def _check_ingress_count(spec: ReleaseSpec, blockers: list[Blocker]) -> None:
    count = sum(1 for service in spec.services if service.role is ServiceRole.ingress)
    if count != 1:
        blockers.append(
            Blocker(
                code=BlockerCode.ingress_count,
                message=(
                    f"a releasable spec must declare EXACTLY ONE ingress service, found {count}"
                ),
            )
        )


def _check_paths_present(
    spec: ReleaseSpec, files: Mapping[str, int | bytes], blockers: list[Blocker]
) -> None:
    tree = _tree_paths(files)
    for service in spec.services:
        if not _dir_present(tree, service.root):
            blockers.append(
                Blocker(
                    code=BlockerCode.missing_path,
                    message=f"service {service.id!r} root is absent from the file tree",
                    path=service.root,
                )
            )
        if service.lockfile is not None and not _file_present(tree, service.root, service.lockfile):
            blockers.append(
                Blocker(
                    code=BlockerCode.missing_path,
                    message=f"service {service.id!r} lockfile is absent from the file tree",
                    path=service.lockfile,
                )
            )


def _check_undeclared_env_vars(spec: ReleaseSpec, blockers: list[Blocker]) -> None:
    # A var is "known" if it is declared in spec.env OR is a service `port_env` —
    # the $PORT contract the host injects out-of-band, so referencing it is legal.
    known = {var.name for var in spec.env} | {service.port_env for service in spec.services}
    seen: set[str] = set()
    for label, text in _command_and_resource_strings(spec):
        for name in _var_refs(text):
            if name not in known and name not in seen:
                seen.add(name)
                blockers.append(
                    Blocker(
                        code=BlockerCode.undeclared_env_var,
                        message=(
                            f"{label} references ${{{name}}}, which is not declared in spec.env"
                        ),
                    )
                )


def _check_secret_files(files: Mapping[str, int | bytes], blockers: list[Blocker]) -> None:
    for path in sorted(files):
        if is_runtime_secret_path(path):
            blockers.append(
                Blocker(
                    code=BlockerCode.secret_file_present,
                    message="tree contains a runtime-secret file that must never ship in a release",
                    path=path,
                )
            )


def _check_secret_build_env(spec: ReleaseSpec, blockers: list[Blocker]) -> None:
    """Fail closed on ANY build-scope env var classified `secret`.

    A secret build variable cannot be lowered safely: folding it into a Compose
    `build.args` / Dockerfile `ARG` bakes it into image build history, and a
    secret-free self-host bundle has no way to supply a build-time secret. The
    detector already fails such a workspace closed at discovery
    (`secret_build_env_unsupported`), but this is the AUTHORITATIVE gate at the
    emit/validate boundary: it refuses the spec regardless of how the build var was
    produced (source-derived, hand-built, or a future producer), so the "a secret
    never folds into build.args" invariant does not rest solely on the detector.
    Deterministic: reports each offending name once, in spec order."""
    for var in spec.env:
        if var.scope is EnvScope.build and var.secret is SecretClass.secret:
            blockers.append(
                Blocker(
                    code=BlockerCode.secret_build_env_unsupported,
                    message=(
                        f"build-scope env var {var.name!r} is classified secret; a "
                        "secret build variable cannot be supplied to a secret-free "
                        "self-host bundle (folding it into a build arg would leak it "
                        "into image build history), so this release is not supported. "
                        "Rename it to a non-secret public build var, or remove the "
                        "build-time secret dependency."
                    ),
                )
            )


def _check_secret_name_leaks(spec: ReleaseSpec, blockers: list[Blocker]) -> None:
    secret_names = [var.name for var in spec.env if var.secret is SecretClass.secret]
    if not secret_names:
        return
    reported: set[str] = set()
    for label, text in _leak_scan_strings(spec):
        # Strip legitimate `${NAME}` / `$NAME` references; anything left is a
        # literal value, so a bare secret NAME here is a misuse/leak.
        stripped = _VAR_REF_RE.sub(" ", text)
        for name in secret_names:
            if name not in reported and _bare_name_present(stripped, name):
                reported.add(name)
                blockers.append(
                    Blocker(
                        code=BlockerCode.secret_name_leaked,
                        message=(f"secret env name {name!r} appears as a literal value in {label}"),
                    )
                )


# ---- the public entrypoint ----------------------------------------------------


def validate_release(spec: ReleaseSpec, files: Mapping[str, int | bytes]) -> ValidationResult:
    """Decide whether `spec` is releasable given `files` (a path -> size|content
    view of the workspace tree). PURE and total: it inspects the spec and the path
    set only, and ALWAYS returns a `ValidationResult` — a non-releasable spec
    yields `ok=False` with typed `blockers`, never an exception.

    Blockers found:
    * `ingress_count`      — not exactly one ingress-role service.
    * `missing_path`       — a service root / referenced lockfile absent from the tree.
    * `undeclared_env_var` — a `${VAR}` used by a command/resource that is neither
      declared in `spec.env` nor a service `port_env`.
    * `secret_file_present`— a tree path the runtime-secret predicate flags.
    * `secret_name_leaked` — a `secret`-classified env NAME used as a bare literal
      value (outside a `${...}` reference) somewhere in the spec.
    * `secret_build_env_unsupported` — a build-scope env var classified `secret`
      (unsafe to lower: a build arg bakes it into image history).
    """
    blockers: list[Blocker] = []
    _check_ingress_count(spec, blockers)
    _check_paths_present(spec, files, blockers)
    _check_undeclared_env_vars(spec, blockers)
    _check_secret_files(files, blockers)
    _check_secret_build_env(spec, blockers)
    _check_secret_name_leaks(spec, blockers)
    return ValidationResult(blockers=blockers)


__all__ = [
    "Blocker",
    "BlockerCode",
    "ValidationResult",
    "validate_release",
]
