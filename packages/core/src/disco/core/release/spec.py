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
import re
from collections.abc import Iterable
from enum import Enum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
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

# An argv element shaped like an inline env assignment (`NAME=...`): an UPPERCASE
# env-var name immediately followed by `=`. Such a token is the classic
# `VAR=value command` shell smuggle — a way to slip a secret VALUE through a
# command list — so it is rejected from every argv (install/build/migrate/start).
# A legitimate flag like `--max-old-space-size=512` does NOT match (it does not
# start with an uppercase env-name), nor does `${PORT}` (starts with `$`).
_ENV_ASSIGN_RE = re.compile(r"^[A-Z_][A-Z0-9_]*=")

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


def _validate_argv(value: tuple[str, ...], *, field: str) -> tuple[str, ...]:
    # An argv-list is a real exec vector (`["node", "server.js"]`), NOT a shell
    # string — no element may be empty/blank (which would exec an empty arg), and no
    # element may be shaped like an inline env assignment (`NAME=value`), which would
    # smuggle a secret VALUE through a command token. The env-assignment message
    # NEVER echoes the offending token (it could carry the secret value).
    for index, arg in enumerate(value):
        if not arg.strip():
            raise ValueError(f"{field} argv element {index} must be a non-empty token, got {arg!r}")
        if _ENV_ASSIGN_RE.match(arg):
            raise ValueError(
                f"{field} argv element {index} looks like an inline env assignment "
                "(NAME=...), which could smuggle a secret VALUE through a command token; "
                "declare env vars by NAME only, never as an argv element"
            )
    return value


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
        return _reject_traversal(value, field="ReleaseService root")

    @field_validator("lockfile", "output_dir")
    @classmethod
    def _optional_path_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _reject_traversal(value, field="ReleaseService path")

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
        if not value.startswith("/"):
            raise ValueError(f"ReleaseService health_path must start with '/', got {value!r}")
        return _reject_traversal(value, field="ReleaseService health_path")

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
        if not value.strip():
            raise ValueError("ResourceDecl persistent_path must be non-empty")
        return _reject_traversal(value, field="ResourceDecl persistent_path")

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
# grows the intent's typed fields (an install argv-list, the package manager /
# lockfile, the static output directory) BEYOND the v1 shape, so the version is
# bumped to 2. A persisted v1 sidecar is therefore never silently reinterpreted
# (§8.11): `parse_release_intent` migrates a v1/legacy shape deterministically and
# rejects a NEWER one — the persisted shape change is version-gated end to end.
RELEASE_INTENT_SCHEMA_VERSION = 2


class ReleaseIntent(BaseModel):
    """The typed `release_declare` payload: how the built app builds and runs, as
    DECLARED by the agent.

    A smaller, NAMES-ONLY shape than the full `ReleaseSpec` (no topology, no
    provenance): the build/start argv-lists, the `$PORT` contract, the health
    path, the required env-var NAMES, and the resources it needs. Defined here so
    the tool layer and later work orders share ONE schema for the declaration.

    `schema_version` versions the PERSISTED shape (§8.11): the WO-C4 lowering adds
    version-gated persistence so a stored sidecar always self-identifies its shape
    and an older one is migrated (never silently reinterpreted) by
    `parse_release_intent`; it is bumped to `RELEASE_INTENT_SCHEMA_VERSION`."""

    model_config = _STRICT

    schema_version: int = Field(default=RELEASE_INTENT_SCHEMA_VERSION, ge=1)
    build_cmd: tuple[_ArgvItemStr, ...] = Field(default_factory=tuple, max_length=_MAX_ARGV)
    start_cmd: tuple[_ArgvItemStr, ...] = Field(default_factory=tuple, max_length=_MAX_ARGV)
    port_env: _EnvNameStr = "PORT"
    health_path: _PathStr | None = Field(default=None, exclude_if=lambda value: value is None)
    required_env: tuple[_EnvNameStr, ...] = Field(default_factory=tuple, max_length=_MAX_ENV)
    resources: tuple[ResourceDecl, ...] = Field(default_factory=tuple, max_length=_MAX_RESOURCES)

    @field_validator("build_cmd", "start_cmd")
    @classmethod
    def _commands_are_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_argv(value, field="ReleaseIntent command")

    @field_validator("port_env")
    @classmethod
    def _port_env_is_valid(cls, value: str) -> str:
        return _validate_env_var_name(value, field="ReleaseIntent port_env")

    @field_validator("health_path")
    @classmethod
    def _health_path_is_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/"):
            raise ValueError(f"ReleaseIntent health_path must start with '/', got {value!r}")
        return _reject_traversal(value, field="ReleaseIntent health_path")

    @field_validator("required_env")
    @classmethod
    def _required_env_is_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            _ = _validate_env_var_name(name, field="ReleaseIntent required_env")
        _require_unique(value, what="ReleaseIntent required_env name")
        return value


class IntentUpgradeError(ValueError):
    """A persisted intent sidecar declares a schema version this build cannot read
    (a NEWER version, or a v1 shape that cannot be migrated without guessing). A
    typed subclass of `ValueError` so callers can distinguish a version-gate refusal
    (`intent_upgrade_required`, §8.11) from a plain malformed sidecar."""


def parse_release_intent(raw: object) -> ReleaseIntent:
    """Parse a PERSISTED release-intent sidecar payload into a `ReleaseIntent`,
    version-gated per §8.11 so an older shape is never silently reinterpreted:

    * ``schema_version == RELEASE_INTENT_SCHEMA_VERSION`` (or a legacy shape carrying
      no version, treated as v1) is validated / migrated deterministically. A v1
      shape (the pre-WO-C4 fields, a strict SUBSET of v2) migrates by dropping the
      version tag and letting the new v2 fields default — a total, deterministic
      adapter, so the same v1 bytes always yield the same v2 intent.
    * a NEWER ``schema_version`` (one this build does not know) is REJECTED with
      `IntentUpgradeError` rather than mis-read as v2.

    Raises `IntentUpgradeError` for an unreadable/newer version and `ValueError` /
    pydantic `ValidationError` for a malformed payload."""
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
    # v1 / legacy: migrate deterministically — drop the version tag; the pre-WO-C4
    # fields are a strict subset of v2, so the new fields default. Any field a v1
    # sidecar carries that v2 does not (a removed field) trips `extra=forbid` and
    # is rejected, never guessed at.
    migrated = {key: value for key, value in raw.items() if key != "schema_version"}
    return ReleaseIntent.model_validate(migrated)


# ---- the release spec ---------------------------------------------------------


class ReleaseSpec(BaseModel):
    """The neutral, immutable, secret-free release contract (v1, LOCAL profile).

    Binds a release to an exact committed workspace version (`version_seq` +
    `tree_digest`, mirroring `store.VersionRecord`) and captures the full
    topology: the services (with EXACTLY ONE ingress), the declared env-var names,
    the resources, the detector provenance, the deploy `targets`, and references
    to the files generation emits.

    Cross-field invariants enforced here (so every consumer can trust the spec):
    exactly one ingress-role service; unique service ids, env-var names, and
    resource ids; and referential integrity of `depends_on` / resource `consumers`
    (→ service ids) and env `binding` (→ resource ids)."""

    model_config = _STRICT

    # Forward-compat version tag for the CONTRACT shape itself (distinct from the
    # workspace `version_seq` below), matching the AppKit specs' convention.
    schema_version: int = Field(default=1, ge=1)
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
        for service in self.services:
            for dep in service.depends_on:
                if dep not in service_ids:
                    raise ValueError(f"service {service.id!r} depends_on unknown service {dep!r}")
        for resource in self.resources:
            for consumer in resource.consumers:
                if consumer not in service_ids:
                    raise ValueError(
                        f"resource {resource.id!r} names unknown consumer service {consumer!r}"
                    )
        for var in self.env:
            if var.binding is not None and var.binding not in resource_ids:
                raise ValueError(f"env var {var.name!r} binds unknown resource {var.binding!r}")
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

    Raises `ValueError` on malformed JSON or a non-object payload, and a pydantic
    `ValidationError` if the JSON violates the schema."""
    raw = data.decode("utf-8") if isinstance(data, bytes) else data
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ReleaseSpec is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"ReleaseSpec must be a JSON object, got {type(parsed).__name__}")
    return ReleaseSpec.model_validate(parsed)


__all__ = [
    "CloudResourceProfile",
    "DetectorProvenance",
    "EnvScope",
    "EnvVarDecl",
    "GeneratedFileRef",
    "RELEASE_INTENT_SCHEMA_VERSION",
    "IntentUpgradeError",
    "LocalResourceProfile",
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
