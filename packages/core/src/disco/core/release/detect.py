"""Deterministic detection of an app's release shape (Track-1 subset).

`detect_release` decides *what kind of release* a workspace is — a node service,
a python service, a static bundle, an AppKit-shaped app, an owner-supplied
container, an as-yet-undeclared import, or a non-web workspace — reading ONLY two
inputs:

* the IMMUTABLE project contents (`files`: a path -> content view), and
* the caller's TYPED declaration (`intent`, `provenance`).

It NEVER branches on the requesting build lane, the calling context, or any
free-text instruction — a hosting decision must be reproducible from what the
project *is*, not from how the request happened to be phrased. Two runs over the
same file map return EQUAL results (every collection is built in a stable order),
so a release verdict is a pure function of committed contents + typed intent.

Precedence ladder (highest wins), the Track-1 subset:

1. A typed `ReleaseIntent`, OR the four AppKit contract files
   (`.disco/appspec.json` + `wrangler.toml` + `worker/index.ts` + `schema.sql`)
   -> `candidate`. AppKit-shaped apps get a single `dev_server` ingress plus a
   sqlite-class resource. A typed intent that arrives ALONGSIDE an owner-supplied
   container manifest is two competing declarations of the release shape: that
   conflict fails closed to `needs_review` citing BOTH pieces of evidence.
2. An existing `Dockerfile` / `compose.yaml` (no intent) -> `needs_review` with a
   repairable diagnostic naming the exact fields that cannot be verified
   statically (owner-review UI is Track 2; here we only fail closed with data).
3. Deterministic detectors: `package.json` with a server `start` script -> node;
   `pyproject.toml` / `requirements.txt` with a web-framework import -> python;
   a root `index.html` with no server evidence -> static.
4. `provenance.imported=True` without a typed intent -> `needs_review`, the same
   repairable diagnostic naming every missing release-contract field.
5. No HTTP evidence at all -> `not_web`, with an honest human reason.

Databases: recognized SQLite evidence (a `DATABASE_URL=file:` reference, a
libSQL/drizzle sqlite config, or a `*.db` file) yields a sqlite resource with the
local profile `file:/data/app.db`. Any UNKNOWN engine (a `mysql://` /
`postgres://` / … url) fails closed to `needs_review` and NEVER a guessed
resource — an unmanaged database is a deploy hazard, not a default.

Deliberately ABSENT here (they belong to the Track-2 detection ladder and are NOT
implemented in this module):

* clean-room probe execution (running the build in an ephemeral jail to observe
  its real ports / output tree rather than reasoning about the files);
* attestation reuse (trusting a prior signed release verdict for an unchanged
  tree instead of re-deriving it);
* preview-evidence consumption (folding a live preview's observed HTTP behaviour
  into the assessment).

Layering: `disco.core` is the leaf — this module imports ONLY pydantic, the
stdlib, and the sibling `disco.core.release.spec` contract. It does NOT import
`disco.tools.*` or any higher layer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from disco.core.release.spec import (
    EnvScope,
    EnvVarDecl,
    LocalResourceProfile,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseService,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
    RuntimeStrategy,
    SecretClass,
    ServiceRole,
)

# Frozen + reject-unknowns, matching the release contract's discipline: a
# DetectionResult is a finding, not a mutable accumulator, so `==` over two runs
# is a trustworthy determinism check.
_STRICT = ConfigDict(extra="forbid", frozen=True)

# The single ingress service id every derived topology uses, and the sqlite
# resource id / local binding the ladder emits for a recognized sqlite database.
_INGRESS_ID = "web"
_DB_ID = "db"
_SQLITE_URL = "file:/data/app.db"
_SQLITE_PATH = "/data/app.db"
_SQLITE_VOLUME = "app-data"

# The AppKit dev_server (workerd `wrangler dev`) interim local-run profile. Its
# persistent D1/SQLite state lives under `/data/state` (the dir `wrangler dev
# --persist-to` writes), reached through a named volume mounted at `/data`. This
# is DISTINCT from the generic `/data/app.db` sqlite profile above: an AppKit app
# is served by the workerd dev runtime, not a bespoke process reading a file DB.
_APPKIT_STATE_PATH = "/data/state"
_APPKIT_STATE_URL = "file:/data/state"
_APPKIT_STATE_VOLUME = "app-state"
# AppKit's admin read-back secret NAME (fail-closed auth gate in worker/index.ts);
# used when the app's `.dev.vars.example` template is absent from the tree.
_APPKIT_DEFAULT_SECRET = "ADMIN_TOKEN"
# A safe fallback D1 database name if wrangler.toml declares none.
_APPKIT_DEFAULT_DB = "appkit"

# The wrangler.toml D1 `database_name = "..."` line, and a `NAME=` declaration in
# an AppKit `.dev.vars.example` secret template.
_WRANGLER_DB_NAME_RE = re.compile(r'(?m)^\s*database_name\s*=\s*"([^"]+)"')
_DEV_VARS_NAME_RE = re.compile(r"(?m)^\s*([A-Z_][A-Z0-9_]*)\s*=")
_DEV_VARS_EXAMPLE = ".dev.vars.example"

# The release-contract fields a typed `ReleaseIntent` supplies. A `needs_review`
# diagnostic names whichever of these it could not establish from the contents —
# the "repairable" list an owner (or a later declaration) fills in.
_CONTRACT_FIELDS = ("build_cmd", "start_cmd", "port_env", "health_path", "required_env")

# Human detail for each un-establishable contract field (used by the repairable
# diagnostics for imported repos and opaque container manifests).
_UNVERIFIABLE: dict[str, str] = {
    "build_cmd": "the build command is not declared",
    "start_cmd": "the start command is not a statically verifiable argv",
    "port_env": "the bound port ($PORT) is not statically verifiable",
    "health_path": "no health path is declared",
    "required_env": "the required env-var names are not declared",
}

# Container manifests whose mere presence means an owner already described the
# runtime opaquely (fail closed to review rather than re-deriving it).
_CONTAINER_NAMES = frozenset(
    {
        "Dockerfile",
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    }
)

# The four AppKit contract files whose joint presence is the AppKit shape.
_APPKIT_FILES = (".disco/appspec.json", "wrangler.toml", "worker/index.ts", "schema.sql")

# argv[0] interpreter tokens that reveal a declared process's runtime.
_NODE_TOKENS = frozenset({"node", "npm", "npx", "pnpm", "yarn", "bun"})
_PY_TOKENS = frozenset(
    {"python", "python3", "uvicorn", "gunicorn", "hypercorn", "flask", "fastapi", "poetry", "uv"}
)

# Non-sqlite database url schemes — recognized ONLY to fail closed on them.
_UNKNOWN_DB_SCHEMES = (
    "mysql://",
    "postgres://",
    "postgresql://",
    "mongodb://",
    "mongodb+srv://",
    "redis://",
    "mssql://",
)

_DB_FILE_URL_RE = re.compile(r"database_url\s*[=:]\s*[\"']?file:", re.IGNORECASE)
_LIBSQL_RE = re.compile(r"@libsql/client|libsql|drizzle", re.IGNORECASE)
_SQLITE_WORD_RE = re.compile(r"sqlite", re.IGNORECASE)


# ---- result models ------------------------------------------------------------


class Provenance(BaseModel):
    """How the workspace arrived, as TYPED data the detector may branch on.

    `imported` is the only decision bit (an imported repo we could not detect
    deterministically needs owner review); `evidence` carries human strings that
    are reported in the result but never change the verdict."""

    model_config = _STRICT

    imported: bool = False
    evidence: tuple[str, ...] = Field(default_factory=tuple)


class MissingField(BaseModel):
    """One release-contract field a `needs_review` result could not establish —
    the machine-readable half of the repairable diagnostic (`field` is a contract
    field name; `detail` explains why it is missing/unverifiable)."""

    model_config = _STRICT

    field: str
    detail: str


class DetectionResult(BaseModel):
    """The detector's verdict as immutable DATA.

    Carries the `assessment` plus the raw findings a caller assembles into a full
    `ReleaseSpec` (the derived `services` / `resources` / `env`), and the human
    `evidence` / `reasons` / `missing` diagnostic. It stops short of a full
    `ReleaseSpec` on purpose: the detector has the contents but NOT the source
    binding (`version_seq` / `tree_digest`) a spec must pin to, so a later work
    order stitches these findings to the committed snapshot."""

    model_config = _STRICT

    assessment: ReleaseAssessment
    services: tuple[ReleaseService, ...] = Field(default_factory=tuple)
    resources: tuple[ResourceDecl, ...] = Field(default_factory=tuple)
    env: tuple[EnvVarDecl, ...] = Field(default_factory=tuple)
    evidence: tuple[str, ...] = Field(default_factory=tuple)
    reasons: tuple[str, ...] = Field(default_factory=tuple)
    missing: tuple[MissingField, ...] = Field(default_factory=tuple)

    @property
    def ingress(self) -> ReleaseService | None:
        """The single ingress-role service, if one was derived (else `None`)."""
        for service in self.services:
            if service.role is ServiceRole.ingress:
                return service
        return None


# ---- database finding ---------------------------------------------------------


class _DbKind(Enum):
    none = "none"
    sqlite = "sqlite"
    unknown = "unknown"


class _DbFinding(NamedTuple):
    kind: _DbKind
    engine: str
    evidence: tuple[str, ...]
    has_url: bool


# ---- content helpers ----------------------------------------------------------


def _as_text(value: str | bytes) -> str:
    """A best-effort text view of a file for signal scanning (binary bytes decode
    lossily; we only ever look for ascii markers)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return value


def _norm(path: str) -> str:
    """Normalize a workspace path: trim, drop a leading `./` and trailing `/`."""
    p = path.strip()
    if p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]


def _paths(files: Mapping[str, str | bytes]) -> frozenset[str]:
    return frozenset(_norm(p) for p in files)


# ---- signal detectors ---------------------------------------------------------


def _container_manifest(files: Mapping[str, str | bytes]) -> str | None:
    """The first (sorted) container manifest path, or `None`."""
    for path in sorted(files):
        if _basename(path) in _CONTAINER_NAMES:
            return _norm(path)
    return None


def _is_appkit(files: Mapping[str, str | bytes]) -> bool:
    present = _paths(files)
    return all(required in present for required in _APPKIT_FILES)


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


def _node_service(files: Mapping[str, str | bytes]) -> ReleaseService | None:
    """A node ingress service IFF a root package.json declares a server `start`
    script (a Vite/static bundle has `build`/`dev` but no server `start`)."""
    pkg = _root_package_json(files)
    if _script(pkg, "start") is None:
        return None
    build_cmd = ("npm", "run", "build") if _script(pkg, "build") is not None else ()
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
        port_env="PORT",
    )


def _has_web_framework(files: Mapping[str, str | bytes]) -> bool:
    for path in sorted(files):
        text = _as_text(files[path]).lower()
        if "fastapi" in text or "from flask" in text or "import flask" in text:
            return True
    return False


def _python_module(files: Mapping[str, str | bytes]) -> str:
    roots = _paths(files)
    if "main.py" in roots:
        return "main"
    if "app.py" in roots:
        return "app"
    return "main"


def _python_service(files: Mapping[str, str | bytes]) -> ReleaseService | None:
    """A python ingress service IFF a python manifest is present AND a web
    framework import (fastapi/flask) is found in the contents."""
    manifests = {"pyproject.toml", "requirements.txt"}
    tree = _paths(files)
    if not (manifests & tree) or not _has_web_framework(files):
        return None
    if "requirements.txt" in tree:
        install: tuple[str, ...] = ("pip", "install", "-r", "requirements.txt")
    else:
        install = ("pip", "install", ".")
    module = _python_module(files)
    return ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.python,
        install_cmd=install,
        start_cmd=("uvicorn", f"{module}:app", "--host", "0.0.0.0", "--port", "${PORT}"),
        port_env="PORT",
    )


def _static_service(files: Mapping[str, str | bytes]) -> ReleaseService | None:
    """A static ingress service IFF a root `index.html` is present (with no server
    `start` script — those are handled as node above)."""
    if "index.html" not in _paths(files):
        return None
    pkg = _root_package_json(files)
    if _script(pkg, "build") is not None:
        return ReleaseService(
            id=_INGRESS_ID,
            role=ServiceRole.ingress,
            runtime=RuntimeStrategy.static,
            build_cmd=("npm", "run", "build"),
            output_dir="dist",
            port_env="PORT",
        )
    return ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.static,
        output_dir=".",
        port_env="PORT",
    )


def _detect_service(files: Mapping[str, str | bytes]) -> ReleaseService | None:
    return _node_service(files) or _python_service(files) or _static_service(files)


def _detect_database(files: Mapping[str, str | bytes]) -> _DbFinding:
    """Classify database evidence: `unknown` (any non-sqlite url scheme — fail
    closed) beats `sqlite` (a file: url / libSQL-drizzle config / `*.db` file)."""
    unknown_engine = ""
    unknown_evidence: list[str] = []
    sqlite_evidence: list[str] = []
    has_url = False
    for path in sorted(files):
        norm = _norm(path)
        text = _as_text(files[path])
        low = text.lower()
        for scheme in _UNKNOWN_DB_SCHEMES:
            if scheme in low:
                if not unknown_engine:
                    unknown_engine = scheme.split("://", 1)[0]
                unknown_evidence.append(f"unrecognized database url '{scheme}' in {norm}")
        if _DB_FILE_URL_RE.search(text):
            has_url = True
            sqlite_evidence.append(f"DATABASE_URL file: reference in {norm}")
        if _LIBSQL_RE.search(text) and (_SQLITE_WORD_RE.search(text) or "file:" in low):
            sqlite_evidence.append(f"libSQL/drizzle sqlite config in {norm}")
        if norm.endswith(".db"):
            sqlite_evidence.append(f"sqlite database file {norm}")
    if unknown_engine:
        return _DbFinding(_DbKind.unknown, unknown_engine, tuple(dict.fromkeys(unknown_evidence)),
                          False)
    if sqlite_evidence:
        return _DbFinding(_DbKind.sqlite, "sqlite", tuple(dict.fromkeys(sqlite_evidence)), has_url)
    return _DbFinding(_DbKind.none, "", (), False)


# ---- evidence / resource builders ---------------------------------------------


def _intent_evidence(intent: ReleaseIntent) -> str:
    if intent.start_cmd:
        return "typed release intent (start_cmd: " + " ".join(intent.start_cmd) + ")"
    return "typed release intent declared"


def _container_evidence(manifest: str) -> str:
    return f"existing container manifest: {manifest}"


def _sqlite_resource(consumer_id: str) -> ResourceDecl:
    return ResourceDecl(
        id=_DB_ID,
        kind=ResourceKind.sqlite,
        persistent_path=_SQLITE_PATH,
        profiles=ResourceProfiles(
            local=LocalResourceProfile(url=_SQLITE_URL, volume=_SQLITE_VOLUME)
        ),
        consumers=(consumer_id,),
    )


def _appkit_db_name(files: Mapping[str, str | bytes]) -> str:
    """The D1 `database_name` declared in `wrangler.toml` (the name `wrangler d1
    execute` targets), or a safe fallback if none is declared. Read from the
    IMMUTABLE contents so the derived migrate command targets the app's real DB."""
    for path in files:
        if _norm(path) == "wrangler.toml":
            match = _WRANGLER_DB_NAME_RE.search(_as_text(files[path]))
            if match is not None and match.group(1).strip():
                return match.group(1).strip()
    return _APPKIT_DEFAULT_DB


def _appkit_secret_names(files: Mapping[str, str | bytes]) -> tuple[str, ...]:
    """The secret env-var NAMES an AppKit app reads locally, taken from its
    `.dev.vars.example` template (NAMES only — the template's placeholder VALUES
    never enter the spec). Falls back to AppKit's `ADMIN_TOKEN` admin-gate secret
    when the template is absent (e.g. a minimal contract-only tree)."""
    for path in files:
        if _norm(path) == _DEV_VARS_EXAMPLE:
            names = tuple(dict.fromkeys(_DEV_VARS_NAME_RE.findall(_as_text(files[path]))))
            if names:
                return names
    return (_APPKIT_DEFAULT_SECRET,)


def _appkit_sqlite_resource(db_name: str) -> ResourceDecl:
    """The AppKit persistent-state resource: a sqlite-class resource whose data
    lives under `/data/state` (where `wrangler dev --persist-to` writes its local
    D1). Its `migrate_cmd` is the `wrangler d1 execute` argv that applies
    `schema.sql` to that local DB; the local-run adapter appends the persist dir."""
    return ResourceDecl(
        id=_DB_ID,
        kind=ResourceKind.sqlite,
        persistent_path=_APPKIT_STATE_PATH,
        profiles=ResourceProfiles(
            local=LocalResourceProfile(url=_APPKIT_STATE_URL, volume=_APPKIT_STATE_VOLUME)
        ),
        consumers=(_INGRESS_ID,),
        migrate_cmd=("npx", "wrangler", "d1", "execute", db_name, "--local", "--file=./schema.sql"),
    )


def _runtime_from_argv(argv: tuple[str, ...]) -> RuntimeStrategy:
    """Infer a declared process's runtime from its start argv's interpreter token;
    an unrecognized/absent interpreter falls back to `node`, the conservative base
    for a declared long-running process on this platform."""
    if argv:
        head = argv[0].rsplit("/", 1)[-1].lower()
        if head in _NODE_TOKENS:
            return RuntimeStrategy.node
        # `startswith("python")` maps versioned interpreters (`python3.12`,
        # `python3.13`) to python too, not just the bare `python`/`python3` tokens.
        if head.startswith("python") or head in _PY_TOKENS:
            return RuntimeStrategy.python
    return RuntimeStrategy.node


def _missing_fields(fields: tuple[str, ...]) -> tuple[MissingField, ...]:
    return tuple(MissingField(field=name, detail=_UNVERIFIABLE[name]) for name in fields)


# ---- ladder rungs -------------------------------------------------------------


def _conflict_result(intent: ReleaseIntent, manifest: str) -> DetectionResult:
    return DetectionResult(
        assessment=ReleaseAssessment.needs_review,
        evidence=(_intent_evidence(intent), _container_evidence(manifest)),
        reasons=(
            "a typed release intent and an existing container manifest both declare "
            "how to run this app; the two sources conflict and an owner must "
            "reconcile them before release.",
        ),
        missing=(
            MissingField(
                field="release_source",
                detail=f"typed intent vs. existing {manifest} — reconcile the two",
            ),
        ),
    )


def _from_intent(intent: ReleaseIntent) -> DetectionResult:
    service = ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=_runtime_from_argv(intent.start_cmd),
        build_cmd=intent.build_cmd,
        start_cmd=intent.start_cmd,
        port_env=intent.port_env,
        health_path=intent.health_path,
    )
    env = tuple(
        EnvVarDecl(name=name, scope=EnvScope.runtime, required=True)
        for name in intent.required_env
    )
    return DetectionResult(
        assessment=ReleaseAssessment.candidate,
        services=(service,),
        resources=intent.resources,
        env=env,
        evidence=(_intent_evidence(intent),),
        reasons=(
            "release shape declared by a typed release intent "
            "(build/start argv, port, health path and env names supplied).",
        ),
    )


def _appkit_result(files: Mapping[str, str | bytes]) -> DetectionResult:
    service = ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.dev_server,
        start_cmd=("npm", "run", "cf:dev"),
        port_env="PORT",
        health_path="/",
    )
    env = tuple(
        EnvVarDecl(
            name=name,
            scope=EnvScope.runtime,
            required=True,
            secret=SecretClass.secret,
        )
        for name in _appkit_secret_names(files)
    )
    resource = _appkit_sqlite_resource(_appkit_db_name(files))
    return DetectionResult(
        assessment=ReleaseAssessment.candidate,
        services=(service,),
        resources=(resource,),
        env=env,
        evidence=tuple(f"AppKit contract file: {name}" for name in _APPKIT_FILES),
        reasons=(
            "AppKit contract files present; a single dev_server ingress on the "
            "workerd dev runtime with a sqlite-class resource persisted at "
            f"{_APPKIT_STATE_PATH}.",
        ),
    )


def _container_review(manifest: str) -> DetectionResult:
    return DetectionResult(
        assessment=ReleaseAssessment.needs_review,
        evidence=(_container_evidence(manifest),),
        reasons=(
            f"an existing {manifest} defines the runtime opaquely; the release "
            "contract fields below cannot be verified statically and need owner "
            "review.",
        ),
        missing=_missing_fields(("start_cmd", "port_env", "health_path", "required_env")),
    )


def _detected_result(service: ReleaseService, files: Mapping[str, str | bytes]) -> DetectionResult:
    database = _detect_database(files)
    base_evidence = (f"deterministic detector: {service.runtime.value} ingress service",)
    if database.kind is _DbKind.unknown:
        return DetectionResult(
            assessment=ReleaseAssessment.needs_review,
            services=(service,),
            evidence=base_evidence + database.evidence,
            reasons=(
                f"detected a {service.runtime.value} ingress service, but the project "
                f"references an unrecognized database engine '{database.engine}' that "
                "cannot be auto-provisioned; an owner must declare it.",
            ),
            missing=(
                MissingField(
                    field="resources",
                    detail=f"unrecognized database engine '{database.engine}'",
                ),
            ),
        )
    resources: tuple[ResourceDecl, ...] = ()
    env: tuple[EnvVarDecl, ...] = ()
    evidence = base_evidence
    if database.kind is _DbKind.sqlite:
        resources = (_sqlite_resource(service.id),)
        evidence = base_evidence + database.evidence
        if database.has_url:
            env = (
                EnvVarDecl(
                    name="DATABASE_URL",
                    scope=EnvScope.runtime,
                    required=True,
                    binding=_DB_ID,
                ),
            )
    return DetectionResult(
        assessment=ReleaseAssessment.candidate,
        services=(service,),
        resources=resources,
        env=env,
        evidence=evidence,
        reasons=(
            f"detected a {service.runtime.value} ingress service from the immutable "
            "project contents.",
        ),
    )


def _imported_review(provenance: Provenance) -> DetectionResult:
    evidence = ("imported project without a typed release intent",) + tuple(provenance.evidence)
    return DetectionResult(
        assessment=ReleaseAssessment.needs_review,
        evidence=evidence,
        reasons=(
            "this project was imported, no release-shaping stack was detected, and "
            "no typed intent was supplied; every release-contract field below must "
            "be declared before release.",
        ),
        missing=_missing_fields(_CONTRACT_FIELDS),
    )


def _not_web_result() -> DetectionResult:
    return DetectionResult(
        assessment=ReleaseAssessment.not_web,
        reasons=(
            "no HTTP entrypoint was found: no package.json server script, no python "
            "web framework, no root index.html, and no container manifest — this "
            "workspace does not describe a web app to release.",
        ),
    )


# ---- the public entrypoint ----------------------------------------------------


def detect_release(
    files: Mapping[str, str | bytes],
    *,
    intent: ReleaseIntent | None,
    provenance: Provenance,
) -> DetectionResult:
    """Detect a workspace's release shape from its immutable contents + typed
    intent, following the precedence ladder documented at module top.

    PURE and deterministic: it reads only `files`, `intent`, and `provenance`,
    builds every collection in a stable order, and returns the same
    `DetectionResult` for the same inputs (so `detect_release(m) ==
    detect_release(m)`). It never spawns a process, touches the network, or
    branches on anything but the two typed inputs.
    """
    manifest = _container_manifest(files)

    # Rung 1 — a typed intent that arrives alongside an owner-supplied container
    # manifest is a conflict between two release declarations: fail closed.
    if intent is not None and manifest is not None:
        return _conflict_result(intent, manifest)

    # Rung 1 — a typed intent wins outright.
    if intent is not None:
        return _from_intent(intent)

    # Rung 1 — the AppKit contract shape.
    if _is_appkit(files):
        return _appkit_result(files)

    # Rung 2 — an existing container manifest, no intent: needs owner review.
    if manifest is not None:
        return _container_review(manifest)

    # Rung 3 — deterministic detectors (with database handling).
    service = _detect_service(files)
    if service is not None:
        return _detected_result(service, files)

    # Rung 4 — imported without a typed intent: repairable review.
    if provenance.imported:
        return _imported_review(provenance)

    # Rung 5 — no HTTP evidence at all.
    return _not_web_result()


__all__ = [
    "DetectionResult",
    "MissingField",
    "Provenance",
    "detect_release",
]
