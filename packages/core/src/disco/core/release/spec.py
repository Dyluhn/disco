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
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Iterable
from enum import Enum
from typing import Annotated

from disco.core.release.command_grammar import (
    check_health_path,
    check_persistent_path,
    check_sqlite_local_url,
    check_token_hygiene,
    check_workspace_rel_path,
    parse_effective_start_argv,
    public_bind_contract_reason,
    public_bind_env_ref,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

# ---- shared config ------------------------------------------------------------
#
# Reject unknown fields everywhere (a stray key is a typo / version skew we want
# to fail loudly), and freeze every model. `frozen=True` blocks attribute
# REASSIGNMENT; every collection field is a `tuple[...]`, not a `list[...]`, so an
# element cannot be appended / replaced in place to smuggle a duplicate id or a
# broken command past the cross-field validators and on to serialization. A
# validated ReleaseSpec is therefore immutable end to end — see the AppKit spec
# (`disco.core.appkit.spec`) this deliberately mirrors.
#
# `hide_input_in_errors=True` keeps pydantic from appending `input_value=...` to a
# validation error message. These models are NAMES-ONLY value guards: a rejected
# argv token (`API_TOKEN=hunter2`) or env NAME could itself carry a secret VALUE,
# so the raw error must never echo the offending input — a `str(ValidationError)`
# names the FIELD, never the value.
_STRICT = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

# An environment variable NAME: UPPERCASE env-var style. A leading letter or
# underscore, then letters / digits / underscores. This rejects the exact classes
# the acceptance criteria call out — an '=', any whitespace, lower-case, a leading
# digit, or any other punctuation — so a declared name is always a safe shell /
# compose identifier.
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# A service / resource id (and the ids that reference them): snake/kebab-case,
# starting with a lowercase letter — a safe compose service name and dependency
# key. Letters, digits, underscore, hyphen.
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# A local volume name (compose/docker style): starts alphanumeric, then a small
# safe charset.
_VOLUME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")

# `tree_digest` mirrors `VersionRecord.tree_digest`, which the project store emits
# as a BARE lowercase sha256 hexdigest (64 hex chars, no algorithm prefix). Pin
# the exact shape so a spec can only bind to a real store digest.
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# ---- bounded string / collection caps -----------------------------------------
#
# Every free-form string is length-capped and every list is count-capped so a
# schema-valid spec is bounded in size (validation cost + serialized bytes) and a
# hostile/corrupt spec can't become a memory bomb — the same discipline the AppKit
# specs use.
_ID_MAX = 64
_NAME_MAX = 200
_SHORT_MAX = 120
_PATH_MAX = 512
_URL_MAX = 2048
_ENV_NAME_MAX = 128
_ARGV_ITEM_MAX = 2048
_REASON_MAX = 2000
_DIGEST_MAX = 64

_MAX_SERVICES = 50
_MAX_ENV = 300
_MAX_RESOURCES = 50
_MAX_ARGV = 200
_MAX_LINKS = 50  # depends_on / consumers fan-out
_MAX_TARGETS = 20
_MAX_GENERATED_FILES = 500
_MAX_EVIDENCE = 200

_IdStr = Annotated[str, StringConstraints(max_length=_ID_MAX)]
_NameStr = Annotated[str, StringConstraints(max_length=_NAME_MAX)]
_ShortStr = Annotated[str, StringConstraints(max_length=_SHORT_MAX)]
_PathStr = Annotated[str, StringConstraints(max_length=_PATH_MAX)]
_UrlStr = Annotated[str, StringConstraints(max_length=_URL_MAX)]
_EnvNameStr = Annotated[str, StringConstraints(max_length=_ENV_NAME_MAX)]
_ArgvItemStr = Annotated[str, StringConstraints(max_length=_ARGV_ITEM_MAX)]
_ReasonStr = Annotated[str, StringConstraints(max_length=_REASON_MAX)]
_DigestStr = Annotated[str, StringConstraints(max_length=_DIGEST_MAX)]


# ---- shared validation helpers ------------------------------------------------


def _validate_env_var_name(value: str, *, field: str) -> str:
    if not _ENV_NAME_RE.match(value):
        raise ValueError(
            f"{field} must be an UPPERCASE env-var name matching {_ENV_NAME_RE.pattern!r} "
            "(a leading letter/underscore, then letters/digits/underscores; no '=', "
            f"whitespace, lower-case or other punctuation), got {value!r}"
        )
    return value


def _validate_id(value: str, *, field: str) -> str:
    if not _ID_RE.match(value):
        raise ValueError(
            f"{field} must match {_ID_RE.pattern!r} (a lowercase letter, then "
            f"letters/digits/underscore/hyphen), got {value!r}"
        )
    return value


def _validate_argv(
    value: tuple[str, ...], *, field: str, allow_public_bind: bool = False
) -> tuple[str, ...]:
    # An argv-list is a real exec vector (`["node", "server.js"]`), NOT a shell string.
    # Each element must be a non-empty token (an empty token would exec an empty arg)
    # AND must pass token hygiene (WO-C5): the ONLY expandable form is an ENTIRE typed
    # env reference (`${NAME}`); a `$` outside a whole reference, command substitution,
    # backticks, separators, redirections, quotes, backslashes, control characters, NUL,
    # Unicode separators, an inline `NAME=value` assignment, or URL userinfo are all
    # rejected — so a token can never smuggle a separator, substitution, partial
    # interpolation, or inline credential. Every message is value-free (it could carry
    # a secret), so a `str(ValidationError)` names the FIELD/rule, never the input.
    for index, arg in enumerate(value):
        if not arg.strip():
            raise ValueError(f"{field} argv element {index} must be a non-empty token")
        check_token_hygiene(
            arg,
            field=f"{field} argv element {index}",
            allow_public_bind=allow_public_bind,
        )
    return value


def _validate_public_bind_start(
    argv: tuple[str, ...], *, declared_names: frozenset[str], field: str
) -> None:
    """Re-parse a start carrying the sole sanctioned partial interpolation shape."""
    if not any(public_bind_env_ref(token) is not None for token in argv):
        return
    parse_effective_start_argv(argv, declared_names=declared_names, field=field)


def _reject_traversal(value: str, *, field: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{field} must not contain a NUL byte")
    if ".." in value.split("/"):
        raise ValueError(f"{field} must not contain a '..' path segment, got {value!r}")
    return value


def _require_unique(values: Iterable[str], *, what: str) -> None:
    seen: set[str] = set()
    for item in values:
        if item in seen:
            raise ValueError(f"duplicate {what}: {item!r}")
        seen.add(item)


def _require_absolute_normalized_posix(value: str, *, field: str) -> None:
    """A resource `persistent_path` (WO-C7 model invariant) must be an ABSOLUTE,
    NORMALIZED POSIX path: it starts at the root (`/`), carries no `//` run, no
    `/./` single-dot segment, no `..` traversal, and no trailing slash — i.e. it is
    already in canonical form. A relative path, a double slash (including a leading
    `//`, which POSIX/`posixpath.normpath` treats specially and would otherwise
    survive normalization), or a `.`/`..` segment is rejected. Value-free: the
    message names the RULE, never the path (a path could carry sensitive data)."""
    if not value.startswith("/"):
        raise ValueError(f"{field} must be an absolute POSIX path (a leading '/')")
    if value.startswith("//"):
        raise ValueError(f"{field} must not begin with a '//' run")
    if value != posixpath.normpath(value):
        raise ValueError(
            f"{field} must be a NORMALIZED absolute POSIX path "
            "(no '//' run, no '/./' segment, no '..' segment, no trailing slash)"
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

    @field_validator("install_cmd", "build_cmd", "migrate_cmd")
    @classmethod
    def _commands_are_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(value, field="ReleaseService command")

    @field_validator("start_cmd")
    @classmethod
    def _start_is_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(
            value,
            field="ReleaseService start_cmd",
            allow_public_bind=True,
        )

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

    @model_validator(mode="after")
    def _public_bind_is_context_bound(self) -> ReleaseService:
        _validate_public_bind_start(
            self.start_cmd,
            declared_names=frozenset({self.port_env}),
            field="ReleaseService start_cmd",
        )
        return self

    @model_validator(mode="after")
    def _start_binds_the_adapter_port(self) -> ReleaseService:
        """A RECOGNIZED dedicated-server start whose bind the schema layer can analyze must
        bind THIS service's adapter-owned ``port_env`` on a PUBLIC interface — never a foreign
        env var, a hard-coded number, a loopback interface, or a non-TCP socket.

        The host publishes ingress on the port it injects through ``port_env`` (compose sets
        e.g. ``PORT=8080``) and maps it to the container's PUBLIC interface. So a uvicorn /
        gunicorn / hypercorn start (incl. ``python -m <server>``) that instead binds
        ``${OTHER_PORT}``, a literal ``--port 9999`` / ``--bind 0.0.0.0:9999``, a loopback
        ``--host 127.0.0.1`` / ``--bind 127.0.0.1:...``, or a ``--bind unix:...`` socket
        listens where the published ingress never reaches — an unreachable deployment.

        This belt FAILS CLOSED by POSITIVE proof (``public_bind_contract_reason``): it SCANS
        the bind tokens robustly rather than trusting a whole-argv parse, so a wrong port that
        rides a duplicate ``--port`` or a trailing unknown flag (which abort the strict parse)
        is still caught — closing the ``bound_public_port_token`` fail-open. It DEFERS to the
        detector (the primary guard) on binds it genuinely cannot analyze at the schema layer:
        a start with no explicit port token (the adapter injects ``port_env``), a non-server /
        source-bound start (``node server.js``), an unrecognized launcher, or a ``--config``-
        only bind. Value-free: the message names the RULE, never the token."""
        reason = public_bind_contract_reason(self.start_cmd, port_env=self.port_env)
        if reason is not None:
            raise ValueError(reason)
        return self


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


def _strip_file_scheme(url: str) -> str:
    return url[len("file:") :] if url.startswith("file:") else url


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

    @field_validator("build_cmd", "install_cmd")
    @classmethod
    def _commands_are_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(value, field="ReleaseIntent command")

    @field_validator("start_cmd")
    @classmethod
    def _start_is_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(
            value,
            field="ReleaseIntent start_cmd",
            allow_public_bind=True,
        )

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

    @model_validator(mode="after")
    def _public_bind_is_context_bound(self) -> ReleaseIntent:
        declared = frozenset(self.required_env) | {item.name for item in self.env} | {self.port_env}
        _validate_public_bind_start(
            self.start_cmd,
            declared_names=declared,
            field="ReleaseIntent start_cmd",
        )
        return self


class IntentUpgradeError(ValueError):
    """A persisted intent sidecar declares a schema version this build cannot read
    (a NEWER version, or a v1 shape that cannot be migrated without guessing). A
    typed subclass of `ValueError` so callers can distinguish a version-gate refusal
    (`intent_upgrade_required`, §8.11) from a plain malformed sidecar."""


def parse_release_intent(raw: object) -> ReleaseIntent:
    """Parse a PERSISTED release-intent sidecar payload into a `ReleaseIntent`,
    version-gated per §8.11 so an older shape is never silently reinterpreted:

    * ``schema_version == RELEASE_INTENT_SCHEMA_VERSION`` (or a legacy shape carrying
      no version, treated as v1) is validated / migrated deterministically. An OLDER
      shape (a v1/v2 payload, whose fields are a strict SUBSET of v3) migrates by
      dropping the version tag and letting the new v3 fields default — a total,
      deterministic adapter, so the same older bytes always yield the same v3 intent.
    * a NEWER ``schema_version`` (one this build does not know) is REJECTED with
      `IntentUpgradeError` rather than mis-read as the current schema.

    Raises `IntentUpgradeError` for a version-gate refusal (a NEWER schema, or a v1
    shape carrying a field the current schema forbids — an upgrade this build cannot
    perform without guessing) and `ValueError` / pydantic `ValidationError` for a
    genuinely malformed payload."""
    if not isinstance(raw, dict):
        raise ValueError(f"release intent must be a JSON object, got {type(raw).__name__}")
    version_obj: object = raw.get("schema_version", 1)
    if not isinstance(version_obj, int) or isinstance(version_obj, bool):
        raise ValueError(f"release intent schema_version must be an int, got {version_obj!r}")
    if version_obj > RELEASE_INTENT_SCHEMA_VERSION:
        raise IntentUpgradeError(
            f"release intent declares schema_version {version_obj}, newer than this "
            f"build supports ({RELEASE_INTENT_SCHEMA_VERSION}); it cannot be read "
            "without an upgrade and must not be silently reinterpreted."
        )
    if version_obj == RELEASE_INTENT_SCHEMA_VERSION:
        return ReleaseIntent.model_validate(raw)
    # v1 / v2 / legacy: migrate deterministically — drop the version tag; the older
    # fields are a strict subset of v3, so the new fields default.
    migrated = {key: value for key, value in raw.items() if key != "schema_version"}
    try:
        return ReleaseIntent.model_validate(migrated)
    except ValidationError as exc:
        # A well-formed legacy shape whose ONLY defect is a field the current schema
        # FORBIDS (`extra_forbidden`) cannot be upgraded without guessing what that
        # removed field meant — that is a version-gate refusal (`intent_upgrade_required`,
        # §8.11), NOT a generic malformed sidecar, and NOT a field to silently drop. A
        # payload carrying any OTHER validation error (a bad env name, a wrong type) is
        # genuinely malformed and is re-raised as the `ValidationError` it is.
        errors = exc.errors()
        if errors and all(err.get("type") == "extra_forbidden" for err in errors):
            raise IntentUpgradeError(
                "release intent declares a legacy (v1) shape carrying field(s) the "
                f"current schema (v{RELEASE_INTENT_SCHEMA_VERSION}) does not define; it "
                "cannot be upgraded without guessing and must not be silently "
                "reinterpreted."
            ) from exc
        raise


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
    def _adapter_port_env_is_not_application_env(self) -> ReleaseSpec:
        """Keep every service port name exclusively owned by the target adapter.

        The adapter supplies this value to match the port it publishes. Allowing the same
        name in application env metadata (runtime *or* build scope, required or optional,
        bound or unbound) creates two owners and can silently redirect the process away
        from the published port. This belongs at the neutral spec boundary so every target
        adapter and every construction path gets the same invariant.
        """
        application_names = {var.name for var in self.env}
        for service in self.services:
            if service.port_env in application_names:
                raise ValueError(
                    f"service {service.id!r} port_env is also declared as application env; "
                    "a service port variable is adapter-owned and must not appear in "
                    "ReleaseSpec.env"
                )
        return self

    @model_validator(mode="after")
    def _references_resolve(self) -> ReleaseSpec:
        service_ids = {service.id for service in self.services}
        resource_ids = {resource.id for resource in self.resources}
        for service in self.services:
            for dep in service.depends_on:
                if dep not in service_ids:
                    raise ValueError(f"service {service.id!r} depends_on unknown service {dep!r}")
        for resource in self.resources:
            # WO-C7: every resource must have at least one (valid) consumer — an
            # orphan resource that no service uses is an ill-formed topology.
            if not resource.consumers:
                raise ValueError(
                    f"resource {resource.id!r} declares no consumers; every resource must "
                    "have at least one consumer service"
                )
            for consumer in resource.consumers:
                if consumer not in service_ids:
                    raise ValueError(
                        f"resource {resource.id!r} names unknown consumer service {consumer!r}"
                    )
        for var in self.env:
            if var.binding is not None and var.binding not in resource_ids:
                raise ValueError(f"env var {var.name!r} binds unknown resource {var.binding!r}")
            # WO-C7: a DECLARED per-env consumer scope must name at least one real
            # service. `None` (no scope) is handled elsewhere (sole-ingress default /
            # implicit-fan-out rejection); an EXPLICIT empty tuple `[]` is a distinct,
            # fail-closed error — it is NOT "reaches every service", it would reach
            # NONE, silently dropping a required var from every container (a
            # broken-bundle false affordance). Reject it, and reject any named
            # consumer that is not a real service.
            if var.consumers is not None:
                if not var.consumers:
                    raise ValueError(
                        f"env var {var.name!r} declares an empty consumer scope; a "
                        "declared env consumer scope must name at least one service"
                    )
                for consumer in var.consumers:
                    if consumer not in service_ids:
                        raise ValueError(
                            f"env var {var.name!r} names unknown consumer service {consumer!r}"
                        )
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


# ---- digest + canonical serialization -----------------------------------------
#
# All digests are namespaced with their algorithm (`sha256:`) so a future switch
# is self-describing on disk — the same convention AppKit's snapshot digests use.
_ALGO = "sha256"


def spec_digest(spec: ReleaseSpec) -> str:
    """A stable content digest over a ReleaseSpec.

    Canonical JSON — sorted keys, no insignificant whitespace — so two specs that
    are EQUAL as data hash identically regardless of the order fields were
    supplied in or of formatting, and ANY field change changes the digest. This is
    a release's content identity."""
    payload = json.dumps(
        spec.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{_ALGO}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def serialize_release_spec(spec: ReleaseSpec) -> str:
    """Canonical, human-readable JSON text for a ReleaseSpec.

    REVALIDATES first (a non-construction path such as `model_copy(update=...)` /
    `model_construct(...)` can build an instance that skipped the cross-field
    validators), then dumps canonically: sorted keys + a stable indent + a
    trailing newline. Deterministic, so `serialize → load → serialize` is
    byte-identical."""
    validated = ReleaseSpec.model_validate(spec.model_dump(mode="json"))
    return (
        json.dumps(
            validated.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )


def load_release_spec(data: bytes | str) -> ReleaseSpec:
    """Parse + schema-validate a ReleaseSpec from JSON bytes/str already in hand.

    Version-gated (WO-C7 v1 read policy): a payload declaring a `schema_version`
    NEWER than `RELEASE_SPEC_SCHEMA_VERSION` is REJECTED rather than mis-read under
    the current schema. A v1 (or version-less legacy) payload is read as-is: it
    carries no per-env `consumers`, so each var parses as `None` and the cross-field
    invariants apply the documented v1 rule (sole-ingress default for a
    single-service spec; a multi-service unbound-consumer-less var is refused, never
    fanned out).

    Raises `ValueError` on malformed JSON, a non-object payload, or a newer schema
    version, and a pydantic `ValidationError` if the JSON violates the schema."""
    raw = data.decode("utf-8") if isinstance(data, bytes) else data
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ReleaseSpec is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"ReleaseSpec must be a JSON object, got {type(parsed).__name__}")
    version = parsed.get("schema_version", 1)
    if isinstance(version, int) and not isinstance(version, bool):
        if version > RELEASE_SPEC_SCHEMA_VERSION:
            raise ValueError(
                f"ReleaseSpec declares schema_version {version}, newer than this build "
                f"supports ({RELEASE_SPEC_SCHEMA_VERSION}); it must not be silently "
                "reinterpreted under an older schema."
            )
    return ReleaseSpec.model_validate(parsed)


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
