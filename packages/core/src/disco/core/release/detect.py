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

# Substrings that mark an env-var NAME as secret-shaped (matched case-insensitively
# against the UPPERCASE name). Classification fails CLOSED toward `secret`: a
# false positive merely guards a public var, whereas a false negative would embed a
# real secret's name as `public`. Provider-neutral — no vendor names.
_SECRET_NAME_MARKERS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE",
    "APIKEY",
    "KEY",
    "AUTH",
)

# argv[0] interpreter tokens that reveal a declared process's runtime.
_NODE_TOKENS = frozenset({"node", "npm", "npx", "pnpm", "yarn", "bun"})
_PY_TOKENS = frozenset(
    {"python", "python3", "uvicorn", "gunicorn", "hypercorn", "flask", "fastapi", "poetry", "uv"}
)

# Start-command heads NOT present on the neutral base images: an alternate JS
# runtime (`bun`/`deno`) or a package-manager RUNNER the base lacks (`uv run`,
# `poetry run`). A declared start through one of these fails closed to
# `toolchain_unsupported` — the base cannot run it without the owner vendoring it.
_UNSUPPORTED_TOOLCHAIN_HEADS = frozenset({"bun", "deno", "poetry", "uv"})

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

# ---- bounded detection (§7.11) ------------------------------------------------
#
# Every WHOLE-TREE content scan (web-framework, database, env, route signals) reads
# at most `_MAX_SCAN_BYTES` per file and SKIPS binary files (a NUL byte in the
# scanned head marks a file binary). A named-config parse (`package.json`,
# `wrangler.toml`, `vite.config.*`) reads a single small file and is separately
# bounded by the same cap. The detector therefore NEVER performs an unbounded
# whole-tree text decode, so an out-of-bounds binary blob or an oversized text file
# can never inject a spurious marker (a `scheme://` url, a framework token) into the
# verdict — the marker sits past the cap, or inside a skipped binary, and is never
# read. The cap is generous (well above any real source file) so a legitimate
# config is always fully seen.
_MAX_SCAN_BYTES = 256 * 1024

# Direct environment-variable reads in JS/TS: `process.env.NAME` and the quoted
# bracket form `process.env['NAME']`. A DYNAMIC bracket read (`process.env[name]`
# — a non-quoted index) cannot be resolved to a NAME statically and fails closed.
_JS_ENV_DOT_RE = re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)")
_JS_ENV_STR_RE = re.compile(r"""process\.env\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]""")
_JS_ENV_DYNAMIC_RE = re.compile(r"""process\.env\[\s*(?!['"])""")

# A `NAME=` declaration line in a dotenv / dev-vars file (`.env`, `.env.example`,
# `.dev.vars`, …) — the env NAMES an owner has DECLARED for the workspace (values
# are ignored; only the names matter). An optional `export ` prefix is tolerated.
_DOTENV_NAME_RE = re.compile(r"(?m)^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

# A root HTTP route registration serving `/` (an Express/http root handler or a
# FastAPI/Flask root GET decorator) — used to POSITIVELY establish the `GET /`
# health contract rather than guessing it.
_JS_ROOT_ROUTE_RE = re.compile(r"""\.(?:get|use|all|route)\(\s*['"]/['"]""")
_JS_CATCHALL_RE = re.compile(r"createServer\s*\(")
_PY_ROOT_GET_RE = re.compile(r"""@\w+\.get\(\s*['"]/['"]""")

# A module-level FastAPI (ASGI) app assignment in a root module — the only python
# shape the detector can auto-start with `uvicorn <module>:app`. A Flask (WSGI) app
# or a nested/absent entrypoint is NOT auto-startable and fails closed.
_FASTAPI_APP_RE = re.compile(r"(?m)^\s*app\s*=\s*FastAPI\b")

# CONVENTIONAL health-probe endpoints the detector "infers" — the well-known paths a
# reader would ASSUME a service exposes. When an intent declares one of these AND the
# workspace ships a scannable service that does NOT actually register the route, the
# health contract is broken (`health_path_unresolved`): the app claims a standard
# probe it never serves. A NON-conventional, owner-specific path (e.g. a deep custom
# route) is trusted as a declared datum, not second-guessed against the source. The
# root `/` is universally served and is never verified.
_CONVENTIONAL_HEALTH_PATHS = frozenset(
    {"/healthz", "/health", "/ping", "/status", "/livez", "/readyz", "/healthcheck"}
)

# A static-build output directory declared in a Vite config: an EXPLICIT string
# literal (`outDir: 'dist'`) is honored; a bare `outDir` key with a non-literal
# value (`outDir: process.env...`) is DYNAMIC and fails closed.
_VITE_OUTDIR_LITERAL_RE = re.compile(r"""outDir\s*:\s*['"]([^'"]+)['"]""")
_VITE_OUTDIR_KEY_RE = re.compile(r"\boutDir\b")
_VITE_CONFIG_NAMES = frozenset(
    {"vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.cjs"}
)


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


class DetectionBlocker(BaseModel):
    """A fine-grained, TYPED reason a workspace fails closed to `needs_review`.

    `code` is a stable, machine-readable code the release API returns verbatim on
    the blocker's `code` field (`required_env_unresolved`, `port_contract_unresolved`,
    `entrypoint_unresolved`, `toolchain_unsupported`, `output_dir_unresolved`,
    `health_path_unresolved`, `runtime_conflict`); `field` optionally names the
    release-contract field an owner must declare; `path` optionally names a
    workspace path the finding is about. Distinct from `MissingField`: a
    `DetectionBlocker` carries the EXACT typed code (not the coarse
    `release_field_unresolved`), so a predictable defect is diagnosable."""

    model_config = _STRICT

    code: str
    message: str
    field: str | None = None
    path: str | None = None


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
    blockers: tuple[DetectionBlocker, ...] = Field(default_factory=tuple)

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


class _DetectBlocker(NamedTuple):
    """An internal fail-closed outcome from a runtime detector: the workspace has
    THIS runtime's signature but a predictable, unrepairable-without-declaration
    defect, so detection fails closed with the exact typed `code`. A detector
    returns `None` (signature absent — try the next runtime), a `ReleaseService`
    (a resolved candidate), or a `_DetectBlocker` (signature present but unreleasable)."""

    code: str
    message: str
    field: str
    evidence: tuple[str, ...]


# ---- content helpers ----------------------------------------------------------


def _as_text(value: str | bytes) -> str:
    """A best-effort, BOUNDED text view of a named-config file for parsing (binary
    bytes decode lossily; we only ever look for ascii markers). Capped at
    `_MAX_SCAN_BYTES` so even a pathological single config file can never force an
    unbounded decode (§7.11)."""
    if isinstance(value, bytes):
        return value[:_MAX_SCAN_BYTES].decode("utf-8", errors="ignore")
    return value[:_MAX_SCAN_BYTES]


def _scan_text(value: str | bytes) -> str:
    """A BOUNDED, binary-safe text view for WHOLE-TREE signal scanning (§7.11).

    Reads at most `_MAX_SCAN_BYTES` and returns an EMPTY view for a binary file (a
    NUL byte in the scanned head marks it binary). A marker that sits past the cap
    (an oversized text file) or inside a binary blob is therefore never read, so it
    can never reach the verdict via an unbounded whole-tree decode."""
    if isinstance(value, bytes):
        head = value[:_MAX_SCAN_BYTES]
        if b"\x00" in head:
            return ""
        return head.decode("utf-8", errors="ignore")
    return value[:_MAX_SCAN_BYTES]


def _file_text(files: Mapping[str, str | bytes], target: str) -> str | None:
    """The bounded text of the file whose normalized path is `target`, or `None`."""
    for path in files:
        if _norm(path) == target:
            return _scan_text(files[path])
    return None


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


# ---- env / route / health signal helpers (bounded, whole-tree) ----------------


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
    return pattern.search(source) is not None


def _js_serves_root(source: str) -> bool:
    """Whether a node server POSITIVELY serves `GET /` — a raw `http.createServer`
    catch-all handler, or an explicit root route registration. Used to establish
    the `/` health contract rather than assuming it."""
    return (
        _JS_CATCHALL_RE.search(source) is not None or _JS_ROOT_ROUTE_RE.search(source) is not None
    )


def _node_source(files: Mapping[str, str | bytes]) -> str:
    """The bounded, binary-safe concatenation of the workspace's scannable content —
    where `process.env` reads and route registrations live. Each file is capped and
    binary files are skipped (§7.11), so an out-of-bounds file injects no signal."""
    return "\n".join(_scan_text(files[path]) for path in sorted(files))


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
        port_env=port_env,
        health_path="/" if _js_serves_root(source) else None,
    )


def _has_web_framework(files: Mapping[str, str | bytes]) -> bool:
    for path in sorted(files):
        text = _scan_text(files[path]).lower()
        if "fastapi" in text or "from flask" in text or "import flask" in text:
            return True
    return False


def _python_root_module(files: Mapping[str, str | bytes]) -> str | None:
    """The root module (`main`/`app`) that defines a module-level FastAPI (ASGI)
    `app` — the only shape auto-startable with `uvicorn <module>:app`. Returns
    `None` for a nested entrypoint, a Flask (WSGI) app, or an absent root module —
    the detector must NOT invent a `main:app` that does not exist."""
    for module, filename in (("main", "main.py"), ("app", "app.py")):
        text = _file_text(files, filename)
        if text is not None and _FASTAPI_APP_RE.search(text):
            return module
    return None


def _python_detect(
    files: Mapping[str, str | bytes],
) -> ReleaseService | _DetectBlocker | None:
    """Detect a python ingress from a python manifest + a web-framework import.

    Returns `None` when there is no python-web signature; a `ReleaseService` for a
    resolvable FastAPI (ASGI) root app (`uvicorn main:app`, established `GET /`
    health when a root route exists); or a `_DetectBlocker`
    (`entrypoint_unresolved`) when the framework is present but no compatible root
    ASGI entrypoint can be resolved (a nested app, a Flask/WSGI app, or no root
    module) — never a fabricated `main:app`."""
    manifests = {"pyproject.toml", "requirements.txt"}
    tree = _paths(files)
    if not (manifests & tree) or not _has_web_framework(files):
        return None  # no python-web signature — not this runtime

    module = _python_root_module(files)
    if module is None:
        return _DetectBlocker(
            code="entrypoint_unresolved",
            message=(
                "a python web framework was detected but no compatible root ASGI "
                "entrypoint (a `main.py`/`app.py` defining `app = FastAPI(...)`) could "
                "be resolved — a nested module or a WSGI (Flask) app has no statically "
                "verifiable start command; declare the start command via a typed intent."
            ),
            field="start_cmd",
            evidence=("python entrypoint evidence: no root `app = FastAPI(...)` module",),
        )
    install: tuple[str, ...] = (
        ("pip", "install", "-r", "requirements.txt")
        if "requirements.txt" in tree
        else ("pip", "install", ".")
    )
    module_text = _file_text(files, f"{module}.py") or ""
    return ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.python,
        install_cmd=install,
        start_cmd=("uvicorn", f"{module}:app", "--host", "0.0.0.0", "--port", "${PORT}"),
        port_env="PORT",
        health_path="/" if _PY_ROOT_GET_RE.search(module_text) else None,
    )


def _static_output_dir(files: Mapping[str, str | bytes], build_script: str) -> str | None:
    """Resolve a build-requiring static bundle's output directory, or `None` when it
    cannot be established (fail closed):

    * an unknown build tool (not a recognized `vite` build) -> `None`;
    * a Vite config with an EXPLICIT literal `outDir: '<x>'` -> `<x>`;
    * a Vite config with a DYNAMIC `outDir` (a non-literal value) -> `None`;
    * a provable default-Vite build (recognized `vite`, no `outDir` override) ->
      Vite's default `dist`."""
    if not re.search(r"\bvite\b", build_script):
        return None  # unknown bundler — output dir not statically knowable
    for path in files:
        if _basename(path) in _VITE_CONFIG_NAMES:
            text = _scan_text(files[path])
            literal = _VITE_OUTDIR_LITERAL_RE.search(text)
            if literal is not None:
                return literal.group(1)
            if _VITE_OUTDIR_KEY_RE.search(text):
                return None  # dynamic/computed outDir — fail closed
    return "dist"  # default-Vite: no outDir override


def _static_detect(
    files: Mapping[str, str | bytes],
) -> ReleaseService | _DetectBlocker | None:
    """Detect a static ingress from a root `index.html` (with no node `start`).

    A no-build site serves the workspace root; a build-requiring bundle must resolve
    a statically-known output directory, else it fails closed
    (`output_dir_unresolved`)."""
    if "index.html" not in _paths(files):
        return None
    pkg = _root_package_json(files)
    build_script = _script(pkg, "build")
    if build_script is not None:
        output_dir = _static_output_dir(files, build_script)
        if output_dir is None:
            return _DetectBlocker(
                code="output_dir_unresolved",
                message=(
                    "a static build was detected but its output directory cannot be "
                    "resolved statically (an unknown build tool, or a dynamically "
                    "computed Vite `outDir`); the emitted bundle would serve the wrong "
                    "tree. Declare the output directory via a typed intent."
                ),
                field="output_dir",
                evidence=(
                    "static output evidence: build output directory not statically resolvable",
                ),
            )
        # A build-requiring static bundle MUST install its dependencies before the
        # build runs, or the emitted image builds against an empty node_modules.
        lockfile, manager, install = _node_install(files)
        return ReleaseService(
            id=_INGRESS_ID,
            role=ServiceRole.ingress,
            runtime=RuntimeStrategy.static,
            package_manager=manager,
            lockfile=lockfile,
            install_cmd=install,
            build_cmd=("npm", "run", "build"),
            output_dir=output_dir,
            port_env="PORT",
        )
    return ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.static,
        output_dir=".",
        port_env="PORT",
    )


def _runtime_conflict_result() -> DetectionResult:
    """Fail closed when TWO different runtime detectors match the same tree.

    A project that carries BOTH a node server start-script AND a python
    web-framework entrypoint declares two competing runtimes; short-circuiting to
    one silently discards the other. Return `needs_review` naming BOTH pieces of
    evidence — carried verbatim in the typed `runtime_conflict` blocker so an owner
    can see WHY the runtimes conflict and declare which one releases the app."""
    node_evidence = "node runtime evidence: package.json declares a server 'start' script"
    python_evidence = (
        "python runtime evidence: a python manifest with a web-framework "
        "(fastapi/flask) entrypoint"
    )
    return DetectionResult(
        assessment=ReleaseAssessment.needs_review,
        evidence=(node_evidence, python_evidence),
        reasons=(
            "two different runtime stacks were detected — a node server start "
            "script AND a python web-framework entrypoint; this conflicting "
            "evidence cannot be reconciled automatically, so an owner must declare "
            "which runtime releases this app.",
        ),
        blockers=(
            DetectionBlocker(
                code="runtime_conflict",
                field="runtime",
                message=(
                    "conflicting runtime evidence — " + node_evidence + "; " + python_evidence
                ),
            ),
        ),
    )


def _fail_closed(blocker: _DetectBlocker) -> DetectionResult:
    """Build a `needs_review` result carrying an EXACT typed `DetectionBlocker`
    (and its evidence) from a detector's fail-closed outcome — no ingress, no
    overlay, so the release API returns `self_host:false` with the precise code."""
    return DetectionResult(
        assessment=ReleaseAssessment.needs_review,
        evidence=blocker.evidence,
        reasons=(blocker.message,),
        blockers=(
            DetectionBlocker(code=blocker.code, message=blocker.message, field=blocker.field),
        ),
    )


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
        text = _scan_text(files[path])
        if any(needle in text for needle in needles):
            return True
    return False


def _detect_database(files: Mapping[str, str | bytes]) -> _DbFinding:
    """Classify database evidence: `unknown` (any non-sqlite url scheme — fail
    closed) beats `sqlite` (a file: url / libSQL-drizzle config / `*.db` file)."""
    unknown_engine = ""
    unknown_evidence: list[str] = []
    sqlite_evidence: list[str] = []
    has_url = False
    for path in sorted(files):
        norm = _norm(path)
        text = _scan_text(files[path])
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


def _declared_python_deps(files: Mapping[str, str | bytes]) -> frozenset[str]:
    """The python package NAMES an owner has DECLARED (a `requirements.txt` line, or
    a `pyproject.toml` token), lowercased. A declared start executable that is a pip
    package (e.g. `gunicorn`) must appear here, or the base image cannot run it."""
    names: set[str] = set()
    req = _file_text(files, "requirements.txt")
    if req is not None:
        for line in req.splitlines():
            token = re.split(r"[\s<>=!~;\[#]", line.strip(), maxsplit=1)[0].strip().lower()
            if token:
                names.add(token)
    pyproject = _file_text(files, "pyproject.toml")
    if pyproject is not None:
        names.update(token.lower() for token in re.findall(r"[A-Za-z0-9_.-]+", pyproject))
    return frozenset(names)


def _toolchain_blocker(
    intent: ReleaseIntent, files: Mapping[str, str | bytes]
) -> _DetectBlocker | None:
    """A `toolchain_unsupported` fail-closed outcome when the declared start command
    cannot run on the neutral base image: an unsupported runner/runtime
    (`bun`/`deno`/`uv run`/`poetry run`), or a python start executable that is a pip
    package absent from the declared dependencies. `None` when the toolchain is
    supported."""
    head = intent.start_cmd[0].rsplit("/", 1)[-1].lower()
    if head in _UNSUPPORTED_TOOLCHAIN_HEADS:
        return _DetectBlocker(
            code="toolchain_unsupported",
            message=(
                f"the declared start command uses {head!r}, which is not available on "
                "the neutral base image; choose a supported toolchain (node/npm or a "
                "python interpreter) or vendor it explicitly."
            ),
            field="start_cmd",
            evidence=(f"toolchain evidence: unsupported start runner {head!r}",),
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
    return None


def _classify_secret(name: str) -> SecretClass:
    """Classify a declared env-var NAME as `secret` or `public` by its shape.

    A name carrying a secret-shaped marker (TOKEN / SECRET / KEY / PASSWORD /
    CREDENTIAL / …) is treated as `secret` so the export guards it and never labels
    it public. Fails CLOSED toward `secret`: over-classifying a public var is
    harmless (it is still just guarded), while under-classifying a real secret is
    the actual hazard."""
    upper = name.upper()
    if any(marker in upper for marker in _SECRET_NAME_MARKERS):
        return SecretClass.secret
    return SecretClass.public


def _from_intent(intent: ReleaseIntent, files: Mapping[str, str | bytes]) -> DetectionResult:
    # A release intent with NO start command cannot describe a runnable app: there is
    # no process to launch. Rather than fabricate a runnable candidate (the emitter
    # would default an empty start to `npm start`), fail closed to needs_review
    # naming the missing start command. The port defaults to the $PORT contract and a
    # health path is optional (consistent with detector-produced candidates), so a
    # non-empty start_cmd is the honest minimum contract for a candidate.
    if not intent.start_cmd:
        return DetectionResult(
            assessment=ReleaseAssessment.needs_review,
            evidence=("typed release intent declared without a start command",),
            reasons=(
                "the declared release intent has no start command, so there is no "
                "runnable process to release; declare at least a start command "
                "before this app can be a release candidate.",
            ),
            missing=_missing_fields(("start_cmd",)),
        )
    # The declared start command must run on the neutral base image (a supported
    # runner + any pip-package start executable declared as a dependency).
    toolchain = _toolchain_blocker(intent, files)
    if toolchain is not None:
        return _fail_closed(toolchain)
    # A declared CONVENTIONAL health probe (`/healthz`, `/health`, …) must correspond
    # to a real route WHEN the workspace ships a concrete node/python service we can
    # scan: claiming a standard probe the source never registers is a broken contract.
    # A non-conventional owner-specific path is trusted as a declared datum, and an
    # intent over an undetectable stack (no scannable service) is trusted as declared.
    if (
        intent.health_path is not None
        and intent.health_path in _CONVENTIONAL_HEALTH_PATHS
        and (_node_detect(files) is not None or _python_detect(files) is not None)
        and not _health_route_present(files, intent.health_path)
    ):
        return _fail_closed(
            _DetectBlocker(
                code="health_path_unresolved",
                message=(
                    f"the declared health path {intent.health_path!r} is a conventional "
                    "probe but does not correspond to any route in the detected service's "
                    "source; the health check would never pass. Declare a health path the "
                    "app actually serves."
                ),
                field="health_path",
                evidence=(f"health evidence: no route for conventional {intent.health_path!r}",),
            )
        )
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
        EnvVarDecl(
            name=name,
            scope=EnvScope.runtime,
            required=True,
            secret=_classify_secret(name),
        )
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
        return _from_intent(intent, files)

    # Rung 1 — the AppKit contract shape.
    if _is_appkit(files):
        return _appkit_result(files)

    # Rung 2 — an existing container manifest, no intent: needs owner review.
    if manifest is not None:
        return _container_review(manifest)

    # Rung 3 — deterministic detectors (with database handling). Each detector
    # returns `None` (its runtime signature is absent), a `ReleaseService` (a
    # resolved candidate), or a `_DetectBlocker` (its signature is present but the
    # contract is predictably broken — fail closed with the exact typed code). When
    # two DIFFERENT runtime signatures both match (a node server start-script AND a
    # python web-framework entrypoint), that is genuinely conflicting evidence: fail
    # closed to `runtime_conflict` naming both.
    node = _node_detect(files)
    python = _python_detect(files)
    if node is not None and python is not None:
        return _runtime_conflict_result()
    outcome = node if node is not None else python
    if outcome is None:
        outcome = _static_detect(files)
    if isinstance(outcome, _DetectBlocker):
        return _fail_closed(outcome)
    if outcome is not None:
        return _detected_result(outcome, files)

    # Rung 4 — imported without a typed intent: repairable review.
    if provenance.imported:
        return _imported_review(provenance)

    # Rung 5 — no HTTP evidence at all.
    return _not_web_result()


__all__ = [
    "DetectionBlocker",
    "DetectionResult",
    "MissingField",
    "Provenance",
    "detect_release",
]
