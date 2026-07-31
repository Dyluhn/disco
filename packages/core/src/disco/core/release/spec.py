"""Release contract v1 — the neutral, immutable, secret-free release spec.

`ReleaseSpec` is the KEYSTONE contract of the export/release pipeline: a single,
provider-neutral description of how a built app is turned into something runnable,
captured at a specific committed workspace version. Every later work order (and,
by design, every FUTURE cloud target) consumes this same shape without a rework.

Three properties make it a trustworthy keystone, and every design choice below
serves them:

* NEUTRAL — it describes *what* a release needs (an ingress service, an argv-list
  start command, a `$PORT` contract, a sqlite resource, the env-var NAMES it
  reads) in terms that no single hosting provider owns. There are NO vendor names
  anywhere in this module: the runtime strategies, resource kinds, and deploy
  `targets` are generic, so a later target is an added string / enum value, never
  a schema fork. (A CI grep enforces the "no vendor names" rule.)
* IMMUTABLE — like the AppKit specs it mirrors, every model is `frozen=True` with
  `extra="forbid"`, and every collection is a `tuple[...]` (not a `list[...]`), so
  a validated spec cannot be mutated in place into an invalid one that then gets
  serialized. A stable `spec_digest` gives a release its content identity.
* SECRET-FREE — `EnvVarDecl` records env-var NAMES and a secrecy classification,
  and NEVER a value. The release contract says which secrets a service reads; the
  secret material is supplied out-of-band by the host at deploy time.

This is the LOCAL profile: `targets` defaults to `("local_compose",)` and every
`ResourceDecl` carries a populated `local` profile plus a `cloud` placeholder that
is always `None` in Track 1 (typed now so a later track fills it without a
migration).

Layering: `disco.core` is the leaf package (`.importlinter`) — this module imports
ONLY pydantic + the stdlib. In particular it does NOT import `disco.tools.*`
(where `VersionRecord` lives); the `version_seq` / `tree_digest` source-binding
fields MIRROR `disco.tools.projects.store.VersionRecord` (`seq: int`,
`tree_digest: str`) by shape so a release can be pinned to an exact snapshot,
without an upward import.

This module is the state-free public compatibility/export facade. Cohesive
private implementation lives under :mod:`disco.core.release.spec_parts` and is
re-imported here so every public symbol keeps its import path.
"""

from __future__ import annotations

from enum import Enum

from disco.core.release.command_grammar import (
    check_health_path,
    check_persistent_path,
    check_sqlite_local_url,
    check_workspace_rel_path,
)
from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
)

from .spec_parts._constants import (
    _HEX64_RE,
    _MAX_ARGV,
    _MAX_ENV,
    _MAX_EVIDENCE,
    _MAX_GENERATED_FILES,
    _MAX_LINKS,
    _MAX_RESOURCES,
    _MAX_SERVICES,
    _MAX_TARGETS,
    _STRICT,
    _VOLUME_RE,
    _ArgvItemStr,
    _DigestStr,
    _EnvNameStr,
    _IdStr,
    _NameStr,
    _PathStr,
    _ReasonStr,
    _ShortStr,
    _UrlStr,
)
from .spec_parts._serialization import (
    IntentUpgradeError,
    load_release_spec,
    parse_release_intent,
    serialize_release_spec,
    spec_digest,
)
from .spec_parts._validators import (
    _reject_traversal,
    _require_absolute_normalized_posix,
    _require_unique,
    _resolve_env_references,
    _resolve_resource_consumers,
    _resolve_service_depends_on,
    _strip_file_scheme,
    _validate_argv,
    _validate_env_var_name,
    _validate_id,
)

# ---- enums (small closed vocabularies) ----------------------------------------


class ReleaseAssessment(str, Enum):
    """The detector's verdict on whether/how a workspace can be released.

    All SIX values exist NOW for forward-compatibility even though Track 1 only
    ever emits the first three (`not_web`, `candidate`, `needs_review`). The
    verification lifecycle values (`verifying`, `verified`, `failed`) are defined
    up front so the enum — and every persisted assessment referencing it — is
    stable when a later track wires the verification loop; no enum bump, no
    migration."""

    not_web = "not_web"
    candidate = "candidate"
    needs_review = "needs_review"
    verifying = "verifying"
    verified = "verified"
    failed = "failed"


class ServiceRole(str, Enum):
    """A service's role in the release topology.

    `ingress` is the single public entrypoint (the one that binds `$PORT`);
    `ReleaseSpec` enforces EXACTLY ONE ingress service. Everything else is an
    internal collaborator."""

    ingress = "ingress"
    backend = "backend"
    worker = "worker"


class RuntimeStrategy(str, Enum):
    """How a service is built/run — a GENERIC, provider-neutral vocabulary.

    `node` / `python` — a runtime process; `static` — pre-built assets served as
    files; `dev_server` — a framework/tooling dev server run strategy; `container`
    — a prebuilt image. Deliberately neutral so a hosting target maps ONTO these
    strategies rather than the spec naming a target."""

    node = "node"
    python = "python"
    static = "static"
    dev_server = "dev_server"
    container = "container"


class EnvScope(str, Enum):
    """When an env var is consumed: at `build` time or at `runtime`."""

    build = "build"
    runtime = "runtime"


class SecretClass(str, Enum):
    """Secrecy classification for a declared env var. `public` values are safe to
    embed/log; `secret` names carry material the host injects out-of-band and that
    must never be persisted with the spec (which records the NAME only)."""

    public = "public"
    secret = "secret"


class ResourceKind(str, Enum):
    """The kind of stateful resource a release needs. `sqlite` is the only kind in
    v1; the enum leaves room for more without a schema fork."""

    sqlite = "sqlite"


# ---- env vars (NAMES ONLY, never values) --------------------------------------


class EnvVarDecl(BaseModel):
    """A declared environment variable — its NAME and metadata, NEVER its value.

    This model is value-free BY CONSTRUCTION (there is no `value` field): a release
    contract records WHICH env vars a service reads, at what scope, whether they
    are required, and whether they are secret — the secret material itself is
    supplied by the host at deploy time and never travels with the spec."""

    model_config = _STRICT

    name: _EnvNameStr
    scope: EnvScope
    required: bool = True
    secret: SecretClass = SecretClass.public
    # Optional id of a `ResourceDecl` this variable is bound to (e.g. the DB URL
    # var bound to the sqlite resource). Still a NAME/id reference — never a value.
    binding: _IdStr | None = Field(default=None, exclude_if=lambda value: value is None)
    # WO-C7 (schema v2): the CONSUMER service ids this env var is injected into. A
    # per-env consumer scope so a multi-service secret reaches ONLY its consumers
    # (plan §11.8/§11.11). `None` means "no explicit scope declared": a BOUND var
    # (`binding` set) derives its consumers from the resource it binds; an UNBOUND
    # var defaults to the sole ingress in a SINGLE-service spec and is REJECTED in a
    # multi-service spec (implicit fan-out is forbidden). An empty tuple `[]` is a
    # DISTINCT, EXPLICIT empty scope that the cross-field validators REJECT
    # fail-closed (a declared scope must name >=1 service): it is never read as
    # "reaches every service" — it would reach NONE and silently drop a required
    # var, so it is refused rather than lowered into a broken bundle. The
    # field is ABSENT from a v1 sidecar (added in v2) — `exclude_if=None` keeps a
    # var that declares no scope serializing to the v1 shape, and a v1 payload
    # lacking the key parses as `None` (the documented v1 read policy).
    consumers: tuple[_IdStr, ...] | None = Field(
        default=None, max_length=_MAX_LINKS, exclude_if=lambda value: value is None
    )

    @field_validator("name")
    @classmethod
    def _name_is_env_var(cls, value: str) -> str:
        return _validate_env_var_name(value, field="EnvVarDecl name")

    @field_validator("binding")
    @classmethod
    def _binding_is_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_id(value, field="EnvVarDecl binding")

    @field_validator("consumers")
    @classmethod
    def _consumers_are_ids(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        for consumer in value:
            _ = _validate_id(consumer, field="EnvVarDecl consumer")
        _require_unique(value, what="EnvVarDecl consumer")
        return value


# ---- services -----------------------------------------------------------------


class ReleaseService(BaseModel):
    """One runnable unit of the release.

    Commands are ARGV-LISTS (`("npm", "ci")`), not shell strings, so they exec
    without a shell and can't be command-injected. `port_env` names the env var
    through which the host tells the service which port to bind (the `$PORT`
    contract). `role` places the service in the topology; `ReleaseSpec` enforces
    exactly one `ingress`."""

    model_config = _STRICT

    id: _IdStr
    role: ServiceRole
    runtime: RuntimeStrategy
    root: _PathStr = "."
    package_manager: _ShortStr | None = Field(default=None, exclude_if=lambda value: value is None)
    lockfile: _PathStr | None = Field(default=None, exclude_if=lambda value: value is None)
    install_cmd: tuple[_ArgvItemStr, ...] = Field(default_factory=tuple, max_length=_MAX_ARGV)
    build_cmd: tuple[_ArgvItemStr, ...] = Field(default_factory=tuple, max_length=_MAX_ARGV)
    migrate_cmd: tuple[_ArgvItemStr, ...] = Field(default_factory=tuple, max_length=_MAX_ARGV)
    start_cmd: tuple[_ArgvItemStr, ...] = Field(default_factory=tuple, max_length=_MAX_ARGV)
    port_env: _EnvNameStr = "PORT"
    health_path: _PathStr | None = Field(default=None, exclude_if=lambda value: value is None)
    depends_on: tuple[_IdStr, ...] = Field(default_factory=tuple, max_length=_MAX_LINKS)
    output_dir: _PathStr | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("id")
    @classmethod
    def _id_is_valid(cls, value: str) -> str:
        return _validate_id(value, field="ReleaseService id")

    @field_validator("root")
    @classmethod
    def _root_is_safe(cls, value: str) -> str:
        # WO-C5: a workspace-relative POSIX path in a closed safe charset — no absolute
        # path, traversal, control char, or metacharacter — so a `root` can never emit
        # an absolute/traversing `COPY` source or split a `COPY` line into a new
        # `RUN`/`ADD`/`COPY --from` Dockerfile instruction.
        check_workspace_rel_path(value, field="ReleaseService root")
        return value

    @field_validator("lockfile", "output_dir")
    @classmethod
    def _optional_path_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # WO-C5: same workspace-relative POSIX guard as `root` — a static `output_dir`
        # can never split the two-stage `COPY --from=build /app/<output_dir>/` line.
        check_workspace_rel_path(value, field="ReleaseService path")
        return value

    @field_validator("install_cmd", "build_cmd", "migrate_cmd", "start_cmd")
    @classmethod
    def _commands_are_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(value, field="ReleaseService command")

    @field_validator("port_env")
    @classmethod
    def _port_env_is_valid(cls, value: str) -> str:
        return _validate_env_var_name(value, field="ReleaseService port_env")

    @field_validator("health_path")
    @classmethod
    def _health_path_is_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # WO-C5: a restricted HTTP-path grammar (data, not source) — see check_health_path.
        check_health_path(value, field="ReleaseService health_path")
        return value

    @field_validator("depends_on")
    @classmethod
    def _depends_on_is_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for dep in value:
            _ = _validate_id(dep, field="ReleaseService depends_on")
        _require_unique(value, what="ReleaseService depends_on entry")
        return value


# ---- resources ----------------------------------------------------------------


class LocalResourceProfile(BaseModel):
    """The LOCAL binding for a resource: how it is reached and where it persists on
    the local host (e.g. `url="file:/data/app.db"`, `volume="app-data"`)."""

    model_config = _STRICT

    url: _UrlStr
    volume: _ShortStr

    @field_validator("url")
    @classmethod
    def _url_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("LocalResourceProfile url must be non-empty")
        return value

    @field_validator("volume")
    @classmethod
    def _volume_is_valid(cls, value: str) -> str:
        if not _VOLUME_RE.match(value):
            raise ValueError(
                f"LocalResourceProfile volume must match {_VOLUME_RE.pattern!r}, got {value!r}"
            )
        return value


class CloudResourceProfile(BaseModel):
    """Typed PLACEHOLDER for a future cloud-managed binding of a resource.

    Intentionally EMPTY in v1: Track 1 emits the LOCAL profile only, so
    `ResourceProfiles.cloud` is ALWAYS `None` here. The type exists now so a later
    cloud target can populate it without migrating the resource shape. Kept
    provider-neutral by construction — no vendor fields."""

    model_config = _STRICT


class ResourceProfiles(BaseModel):
    """The per-profile bindings of a resource. `local` is always present (Track 1
    is the LOCAL profile); `cloud` is a typed placeholder that defaults to `None`
    and stays `None` in Track 1."""

    model_config = _STRICT

    local: LocalResourceProfile
    cloud: CloudResourceProfile | None = Field(default=None, exclude_if=lambda value: value is None)


class ResourceDecl(BaseModel):
    """A stateful resource the release provisions (v1: sqlite).

    `consumers` are the service ids that use it; `migrate_cmd` is the argv-list
    that applies its schema; `persistent_path` is the path whose data must survive
    across restarts; `profiles` carries the LOCAL binding (and the always-`None`
    cloud placeholder)."""

    model_config = _STRICT

    id: _IdStr
    kind: ResourceKind
    persistent_path: _PathStr
    profiles: ResourceProfiles
    consumers: tuple[_IdStr, ...] = Field(default_factory=tuple, max_length=_MAX_LINKS)
    migrate_cmd: tuple[_ArgvItemStr, ...] = Field(default_factory=tuple, max_length=_MAX_ARGV)

    @field_validator("id")
    @classmethod
    def _id_is_valid(cls, value: str) -> str:
        return _validate_id(value, field="ResourceDecl id")

    @field_validator("persistent_path")
    @classmethod
    def _persistent_path_is_safe(cls, value: str) -> str:
        # WO-C5: a mount/volume path in a closed safe charset — no NUL, traversal,
        # control character, or shell metacharacter.
        check_persistent_path(value, field="ResourceDecl persistent_path")
        return value

    @field_validator("consumers")
    @classmethod
    def _consumers_are_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for consumer in value:
            _ = _validate_id(consumer, field="ResourceDecl consumer")
        _require_unique(value, what="ResourceDecl consumer")
        return value

    @field_validator("migrate_cmd")
    @classmethod
    def _migrate_cmd_is_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(value, field="ResourceDecl migrate_cmd")

    @model_validator(mode="after")
    def _local_url_is_safe(self) -> ResourceDecl:
        # WO-C5 §9.8: a sqlite resource's LOCAL url must be a credential-free `file:`
        # URL with an absolute POSIX path CONSISTENT with `persistent_path`. Any
        # authority/userinfo, query, fragment, non-`file` scheme, relative path, or
        # persistent-path mismatch is rejected. Cross-field (url ⊕ persistent_path), so
        # it is a model validator, and value-free so a rejected url never leaks.
        if self.kind is ResourceKind.sqlite:
            check_sqlite_local_url(
                self.profiles.local.url,
                self.persistent_path,
                field="ResourceDecl local url",
            )
        return self


def local_mount_target(resource: ResourceDecl) -> str:
    """The container directory a resource's named volume mounts at — the REAL PARENT
    directory of its local path, so the emitted mount always BACKS the persistent path
    exactly.

    The SINGLE source of truth (WO-C7 / R3 G07) shared by two consumers so they can never
    drift: the `ReleaseSpec` schema validator (no two distinct resources may claim the
    same mount target, and a root parent is rejected as unbackable) and the local-compose
    emitter (each resource mounts its volume here). Derived from the LOCAL url (a sqlite
    `file:/data/app.db` → `/data`; `file:/var/lib/app/db.sqlite` → `/var/lib/app`); the C5
    url⊕persistent_path consistency guard makes the two agree.

    A filesystem-ROOT file (its only path separator is the leading one, e.g. `/app.db`)
    resolves to `/` — its TRUE parent — NOT a blind `/data` (R3 G07). The historical
    `/data` fallback named a mount that does NOT contain a root-level file, so a
    `self_host:true` bundle promised persistence its volume did not back. Detection now
    fails such a layout closed (`persistent_path_unbackable`) BEFORE it can become a
    candidate — and the schema validator refuses it too — because a volume mounted at `/`
    would shadow the container root. Returning the honest `/` here keeps this function
    TRUTHFUL (the mount target it names is the real parent) rather than papering over the
    gap with a directory that does not cover the file."""
    path = _strip_file_scheme(resource.profiles.local.url) or resource.persistent_path
    path = path.rstrip("/")
    slash = path.rfind("/")
    if slash > 0:
        return path[:slash]
    # A filesystem-ROOT path: its true parent is the root `/`. A `/` mount would shadow
    # the container root, so this layout is rejected up front (detection's
    # `persistent_path_unbackable` blocker + the schema's mount-target validator) and a
    # candidate never emits it; naming the honest parent keeps the target truthful.
    return "/"


# ---- detector provenance ------------------------------------------------------


class DetectorProvenance(BaseModel):
    """Where a ReleaseSpec's assessment came from: which detector reached it, at
    what version, and on what evidence.

    Carrying provenance + evidence on the spec means an assessment is legible and
    auditable later (why was this a `candidate`? which detector rev decided?)
    without re-running detection. Provider-neutral: it names a detector and the
    signals it matched, never a hosting target."""

    model_config = _STRICT

    detector: _ShortStr
    detector_version: _ShortStr
    assessment: ReleaseAssessment
    evidence: tuple[_ShortStr, ...] = Field(default_factory=tuple, max_length=_MAX_EVIDENCE)
    reason: _ReasonStr | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("detector", "detector_version")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DetectorProvenance detector/detector_version must be non-empty")
        return value


# ---- generated-file references ------------------------------------------------


class GeneratedFileRef(BaseModel):
    """A reference to a file the release generation emits into the workspace (e.g.
    a compose file, an entrypoint, a Dockerfile). A workspace-relative POSIX path
    plus a short purpose tag — the reference, not the contents."""

    model_config = _STRICT

    path: _PathStr
    purpose: _ShortStr

    @field_validator("path")
    @classmethod
    def _path_is_relative_safe(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("GeneratedFileRef path must be non-empty")
        if value.startswith("/"):
            raise ValueError(
                f"GeneratedFileRef path must be workspace-relative (no leading '/'), got {value!r}"
            )
        return _reject_traversal(value, field="GeneratedFileRef path")

    @field_validator("purpose")
    @classmethod
    def _purpose_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("GeneratedFileRef purpose must be non-empty")
        return value


# ---- release intent (the `release_declare` payload) ---------------------------

# The CURRENT schema version of the persisted `ReleaseIntent` sidecar shape. WO-C4
# grew the intent to v2 (schema version tag). The R2 remediation (G03) completes the
# typed declaration contract the shape already CLAIMED but lacked — an explicit
# `runtime` strategy, an install argv-list, the package manager / lockfile, the static
# `output_dir`, and scoped `env` declarations (build/runtime scope, requiredness,
# secret class) — a persisted shape change, so the version is bumped to 3. A persisted
# v1/v2 sidecar is therefore never silently reinterpreted (§8.11):
# `parse_release_intent` migrates an older shape deterministically (its fields are a
# strict subset of v3, so the new fields default) and rejects a NEWER one — the
# persisted shape change is version-gated end to end.
RELEASE_INTENT_SCHEMA_VERSION = 3


class ReleaseIntent(BaseModel):
    """The typed `release_declare` payload: how the built app builds and runs, as
    DECLARED by the agent.

    A smaller, NAMES-ONLY shape than the full `ReleaseSpec` (no topology, no
    provenance): the build/start argv-lists, the `$PORT` contract, the health
    path, the required env-var NAMES, and the resources it needs. Defined here so
    the tool layer and later work orders share ONE schema for the declaration.

    `schema_version` versions the PERSISTED shape (§8.11): the WO-C4/R2 lowering adds
    version-gated persistence so a stored sidecar always self-identifies its shape
    and an older one is migrated (never silently reinterpreted) by
    `parse_release_intent`; it is bumped to `RELEASE_INTENT_SCHEMA_VERSION`.

    R2 (G03) completes the CONTRACT the shape claimed but lacked: an explicit `runtime`
    strategy, an `install_cmd` argv-list (so a runtime dependency install no longer
    depends on a build step), the `package_manager` / `lockfile` it installs with, the
    static build `output_dir`, and scoped `env` declarations (each an `EnvVarDecl` with
    build/runtime scope, requiredness, and secret class). These reuse the SAME
    field validators as the `ReleaseService`/`ReleaseSpec` contract (`_validate_argv`
    token hygiene for `install_cmd`, `check_workspace_rel_path` for `lockfile` /
    `output_dir`, `EnvVarDecl`'s own name/scope guards for `env`)."""

    model_config = _STRICT

    schema_version: int = Field(default=RELEASE_INTENT_SCHEMA_VERSION, ge=1)
    # R2 (G03): the runtime strategy the app is built/run as. `None` means "infer from
    # the start command" (the historical behaviour); an explicit `static` lets a
    # prebuilt / build-then-serve site declare it has no long-running process.
    runtime: RuntimeStrategy | None = Field(default=None, exclude_if=lambda value: value is None)
    build_cmd: tuple[_ArgvItemStr, ...] = Field(default_factory=tuple, max_length=_MAX_ARGV)
    # R2 (G03): the runtime DEPENDENCY-install argv (e.g. `("npm", "ci")` /
    # `("pip", "install", "-r", "requirements.txt")`). Explicit-wins over the detector's
    # safe derivation; it is token-hygiene checked here and credential-rail checked at
    # the tool boundary (a non-runtime head like `pip` is legal, so it is NOT run
    # through the runtime-start grammar).
    install_cmd: tuple[_ArgvItemStr, ...] = Field(default_factory=tuple, max_length=_MAX_ARGV)
    start_cmd: tuple[_ArgvItemStr, ...] = Field(default_factory=tuple, max_length=_MAX_ARGV)
    # R2 (G03): the package manager + lockfile the install uses. NAMES/paths only.
    package_manager: _ShortStr | None = Field(default=None, exclude_if=lambda value: value is None)
    lockfile: _PathStr | None = Field(default=None, exclude_if=lambda value: value is None)
    # R2 (G03/G06): where a static build's output lands (e.g. `dist`), lowered into the
    # two-stage `COPY --from=build /app/<output_dir>/` copy. Workspace-relative.
    output_dir: _PathStr | None = Field(default=None, exclude_if=lambda value: value is None)
    port_env: _EnvNameStr = "PORT"
    health_path: _PathStr | None = Field(default=None, exclude_if=lambda value: value is None)
    required_env: tuple[_EnvNameStr, ...] = Field(default_factory=tuple, max_length=_MAX_ENV)
    # R2 (G03): scoped env declarations — each carries scope (build/runtime),
    # requiredness, and secret class. A NAMES-ONLY superset of `required_env` (which
    # stays a runtime/required shorthand); detection MERGES the two.
    env: tuple[EnvVarDecl, ...] = Field(default_factory=tuple, max_length=_MAX_ENV)
    resources: tuple[ResourceDecl, ...] = Field(default_factory=tuple, max_length=_MAX_RESOURCES)

    @field_validator("build_cmd", "start_cmd", "install_cmd")
    @classmethod
    def _commands_are_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(value, field="ReleaseIntent command")

    @field_validator("lockfile", "output_dir")
    @classmethod
    def _optional_paths_are_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # R2 (G03/G06): the SAME workspace-relative POSIX guard `ReleaseService` uses —
        # no absolute path, traversal, control char, or COPY-flag-shaped segment — so a
        # declared `output_dir`/`lockfile` can never split a Dockerfile `COPY` line.
        check_workspace_rel_path(value, field="ReleaseIntent path")
        return value

    @field_validator("port_env")
    @classmethod
    def _port_env_is_valid(cls, value: str) -> str:
        return _validate_env_var_name(value, field="ReleaseIntent port_env")

    @field_validator("health_path")
    @classmethod
    def _health_path_is_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # WO-C5: a restricted HTTP-path grammar (data, not source) — see check_health_path.
        check_health_path(value, field="ReleaseIntent health_path")
        return value

    @field_validator("required_env")
    @classmethod
    def _required_env_is_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            _ = _validate_env_var_name(name, field="ReleaseIntent required_env")
        _require_unique(value, what="ReleaseIntent required_env name")
        return value


# ---- the release spec ---------------------------------------------------------

# The CURRENT schema version of the CONTRACT shape. WO-C7 adds the per-env
# `consumers` topology (`EnvVarDecl.consumers`), so the version is bumped to 2. The
# v1 READ POLICY is explicit and tested: a persisted v1 spec carries no per-env
# consumers, so every `EnvVarDecl.consumers` parses as `None`; the cross-field
# invariants below then read it under the SAME rules as a fresh v2 spec — a
# single-service spec defaults an unbound var to its sole ingress, while a
# MULTI-service spec with an unbound, consumer-less var is REJECTED (not silently
# treated as global — plan §11.11 forbids that). Since v1 only ever emitted
# single-service specs (the detector derives a single `web` ingress), every genuine
# v1 spec still reads; only a hand-forged multi-service v1 spec with implicit
# fan-out is refused.
RELEASE_SPEC_SCHEMA_VERSION = 2


class ReleaseSpec(BaseModel):
    """The neutral, immutable, secret-free release contract (v2, LOCAL profile).

    Binds a release to an exact committed workspace version (`version_seq` +
    `tree_digest`, mirroring `store.VersionRecord`) and captures the full
    topology: the services (with EXACTLY ONE ingress), the declared env-var names,
    the resources, the detector provenance, the deploy `targets`, and references
    to the files generation emits.

    Cross-field invariants enforced here (so every consumer can trust the spec):
    exactly one ingress-role service; unique service ids, env-var names, and
    resource ids; referential integrity of `depends_on` / resource `consumers` /
    env `consumers` (→ service ids) and env `binding` (→ resource ids); and — the
    WO-C7 topology invariants — every resource has ≥1 consumer, distinct resources
    never share a persistent_path OR a mount target, every persistent_path is an
    absolute normalized POSIX path, the service dependency graph is acyclic (no
    self-edge, no cycle), and a multi-service spec never fans an unbound env var out
    implicitly (it must declare consumers)."""

    model_config = _STRICT

    # Forward-compat version tag for the CONTRACT shape itself (distinct from the
    # workspace `version_seq` below), matching the AppKit specs' convention. WO-C7
    # bumps the default to `RELEASE_SPEC_SCHEMA_VERSION` (2) — see that constant for
    # the tested v1 read policy.
    schema_version: int = Field(default=RELEASE_SPEC_SCHEMA_VERSION, ge=1)
    kind: _ShortStr
    name: _NameStr
    # Source binding — mirrors `disco.tools.projects.store.VersionRecord`
    # (`seq: int`, `tree_digest: str`, a bare sha256 hexdigest) so a release pins
    # to an exact snapshot. Mirrored by shape (NOT imported) to keep core a leaf.
    version_seq: int = Field(ge=1)
    tree_digest: _DigestStr
    services: tuple[ReleaseService, ...] = Field(default_factory=tuple, max_length=_MAX_SERVICES)
    env: tuple[EnvVarDecl, ...] = Field(default_factory=tuple, max_length=_MAX_ENV)
    resources: tuple[ResourceDecl, ...] = Field(default_factory=tuple, max_length=_MAX_RESOURCES)
    provenance: DetectorProvenance
    # Deploy targets as OPEN strings (not an enum) so a new target is an added
    # string, never a schema fork — the neutrality guarantee. Defaults to the
    # local compose target (Track 1's only target).
    targets: tuple[_ShortStr, ...] = Field(default=("local_compose",), max_length=_MAX_TARGETS)
    generated_files: tuple[GeneratedFileRef, ...] = Field(
        default_factory=tuple, max_length=_MAX_GENERATED_FILES
    )

    @field_validator("kind", "name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ReleaseSpec kind/name must be non-empty")
        return value

    @field_validator("tree_digest")
    @classmethod
    def _tree_digest_is_hex(cls, value: str) -> str:
        if not _HEX64_RE.match(value):
            raise ValueError(
                "ReleaseSpec tree_digest must be a 64-char lowercase sha256 hexdigest "
                "(matching store.VersionRecord.tree_digest), got "
                f"{value!r}"
            )
        return value

    @field_validator("targets")
    @classmethod
    def _targets_are_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for target in value:
            if not target.strip():
                raise ValueError("ReleaseSpec target must be non-empty")
        _require_unique(value, what="ReleaseSpec target")
        return value

    @model_validator(mode="after")
    def _exactly_one_ingress(self) -> ReleaseSpec:
        ingress = [service for service in self.services if service.role is ServiceRole.ingress]
        if len(ingress) != 1:
            raise ValueError(
                f"a ReleaseSpec must declare EXACTLY ONE ingress-role service, found {len(ingress)}"
            )
        return self

    @model_validator(mode="after")
    def _ids_unique(self) -> ReleaseSpec:
        _require_unique((service.id for service in self.services), what="service id")
        _require_unique((var.name for var in self.env), what="env var name")
        _require_unique((resource.id for resource in self.resources), what="resource id")
        return self

    @model_validator(mode="after")
    def _references_resolve(self) -> ReleaseSpec:
        service_ids = {service.id for service in self.services}
        resource_ids = {resource.id for resource in self.resources}
        _resolve_service_depends_on(self.services, service_ids)
        _resolve_resource_consumers(self.resources, service_ids)
        _resolve_env_references(self.env, service_ids, resource_ids)
        return self

    @model_validator(mode="after")
    def _resource_paths_and_mount_targets_unique(self) -> ReleaseSpec:
        # WO-C7: distinct resources may not claim the SAME persistent_path or mount
        # target — either collision would collapse two resources onto one volume and
        # silently clobber their state. Also enforce the absolute-normalized-POSIX
        # persistent_path invariant here (a SPEC/release-boundary tightening; a raw
        # intent-time `ResourceDecl` may still carry a relative spelling). Messages
        # name resource IDS only, never the path value.
        seen_paths: dict[str, str] = {}
        seen_targets: dict[str, str] = {}
        for resource in self.resources:
            _require_absolute_normalized_posix(
                resource.persistent_path, field=f"resource {resource.id!r} persistent_path"
            )
            path = resource.persistent_path
            if path in seen_paths:
                raise ValueError(
                    f"resources {seen_paths[path]!r} and {resource.id!r} declare the same "
                    "persistent_path; distinct resources may not claim the same persistent path"
                )
            seen_paths[path] = resource.id
            target = local_mount_target(resource)
            # R3 (G07): a FILESYSTEM-ROOT persistent path (parent dir `/`) is UNBACKABLE — a
            # named volume that backed it would have to mount at `/` and shadow the whole
            # container root, so no portable volume can persist it. Detection already fails
            # such a layout closed (`persistent_path_unbackable`); this is the AUTHORITATIVE
            # emit-boundary belt (like the emitter's `_revalidated` gate) so a spec built
            # through a NON-detecting path (`model_construct` / a future producer) can never
            # be lowered into a `<volume>:/` mount that silently does not persist. Reject it
            # here — declare the path under a subdirectory (e.g. `/data/app.db`). Names the
            # resource id only, never the path value.
            if target == "/":
                raise ValueError(
                    f"resource {resource.id!r} persists at a filesystem-root path whose "
                    "parent directory is '/'; a named volume cannot back it without mounting "
                    "at the container root, so it cannot be represented portably — declare "
                    "the persistent path under a subdirectory (e.g. '/data/app.db')"
                )
            if target in seen_targets:
                raise ValueError(
                    f"resources {seen_targets[target]!r} and {resource.id!r} mount at the same "
                    "container target; distinct resources may not share a mount target"
                )
            seen_targets[target] = resource.id
        return self

    @model_validator(mode="after")
    def _dependency_graph_is_acyclic(self) -> ReleaseSpec:
        # WO-C7: the generic `depends_on` graph must be a DAG — a service may not
        # depend on itself, and no cycle may exist (a cycle deadlocks compose
        # start-ordering). Referential validity (`_references_resolve`) has already
        # run, so every edge points at a known service; an unknown edge is skipped
        # here defensively. Bounded by `_MAX_SERVICES`.
        adjacency = {service.id: service.depends_on for service in self.services}
        for service in self.services:
            if service.id in service.depends_on:
                raise ValueError(f"service {service.id!r} depends on itself (a self-dependency)")
        visiting: set[str] = set()
        done: set[str] = set()

        def _visit(node: str) -> None:
            visiting.add(node)
            for dep in adjacency.get(node, ()):
                if dep not in adjacency:
                    continue
                if dep in visiting:
                    raise ValueError(
                        f"the service dependency graph contains a cycle (reached {dep!r} again)"
                    )
                if dep not in done:
                    _visit(dep)
            visiting.discard(node)
            done.add(node)

        for sid in adjacency:
            if sid not in done:
                _visit(sid)
        return self

    @model_validator(mode="after")
    def _multi_service_unbound_env_declares_consumers(self) -> ReleaseSpec:
        # WO-C7 §11.11: in a MULTI-service spec, an unbound RUNTIME env var must
        # declare its consumers — implicit fan-out (and silently treating a missing
        # consumer list as global) is forbidden. A single-service spec may default
        # an unbound var to its sole ingress, so the rule is scoped to multi-service.
        # BUILD-scope env is a distinct, documented mechanism: it is PUBLIC by
        # construction (a secret build var is refused upstream) and is folded into
        # each service's compile-time `build.args`, not the runtime environment, so
        # it carries no per-consumer secret-isolation semantics and is not gated here.
        if len(self.services) <= 1:
            return self
        for var in self.env:
            if var.scope is EnvScope.runtime and var.binding is None and var.consumers is None:
                raise ValueError(
                    f"env var {var.name!r} is unbound and declares no consumers in a "
                    "multi-service spec; implicit fan-out is forbidden — declare its "
                    "consumer service(s)"
                )
        return self


__all__ = [
    "CloudResourceProfile",
    "DetectorProvenance",
    "EnvScope",
    "EnvVarDecl",
    "GeneratedFileRef",
    "RELEASE_INTENT_SCHEMA_VERSION",
    "RELEASE_SPEC_SCHEMA_VERSION",
    "IntentUpgradeError",
    "LocalResourceProfile",
    "local_mount_target",
    "ReleaseAssessment",
    "ReleaseIntent",
    "ReleaseService",
    "ReleaseSpec",
    "ResourceDecl",
    "ResourceKind",
    "ResourceProfiles",
    "RuntimeStrategy",
    "SecretClass",
    "ServiceRole",
    "load_release_spec",
    "parse_release_intent",
    "serialize_release_spec",
    "spec_digest",
]
