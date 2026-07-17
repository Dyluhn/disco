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

from disco.core.release.command_grammar import (
    check_declaration_argv,
    check_no_inline_secret_cli,
    check_no_positional_credential,
)
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
    local_mount_target,
)
from pydantic import BaseModel, ConfigDict, Field

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

# Genuinely-SUPPORTED start-command heads — an ALLOWLIST, not a blocklist. The
# neutral base images run exactly these; a declared start whose head is NOT here fails
# closed to `toolchain_unsupported` (§7 crit 4 / §15: NO node-fallback for an arbitrary
# executable merely because its name can be guessed — ruby/go/php/caddy/`./server`/bun/
# deno/uv/poetry are all rejected). Node base: the npm-family launchers the base image
# ACTUALLY provisions — `node`/`npm`/`npx` ONLY. `pnpm`/`yarn` are DELIBERATELY EXCLUDED
# (R2 / G05): the `node:22-bookworm-slim` image ships no pnpm/yarn, so a typed
# `pnpm start` / `yarn start` intent must fail closed with `toolchain_unsupported`
# exactly as the SOURCE-driven package-manager reject already does for a committed
# pnpm/yarn lockfile — never accepted and lowered into an image that cannot run it
# (§8.8: mapping them to npm is FAILURE). Python base: a bare interpreter (matched by
# the `python` prefix, e.g. `python3.12`) plus the ASGI/WSGI servers the image installs.
_SUPPORTED_NODE_HEADS = frozenset({"node", "npm", "npx"})
_SUPPORTED_PY_SERVER_HEADS = frozenset({"uvicorn", "gunicorn", "hypercorn"})

# Package-manager / runtime launcher heads the neutral base images do NOT provision. A
# TYPED intent whose start/build/install command heads with one of these — or that
# declares one as its `package_manager` — fails closed with `toolchain_unsupported`
# (R2 / G05), the intent-path analogue of the source-driven `_unsupported_pm_blocker` /
# `_unsupported_node_pm_declaration` rejects. Includes each manager's `x`-suffixed
# one-off runner (`bunx`/`pnpx`) so it cannot slip through as a build/install head.
_UNPROVISIONED_MANAGERS = frozenset({"pnpm", "pnpx", "yarn", "bun", "bunx", "deno", "poetry", "uv"})


def _is_supported_toolchain_head(head: str) -> bool:
    """Whether a start-command interpreter head is a runtime the base images run."""
    return (
        head in _SUPPORTED_NODE_HEADS
        or head in _SUPPORTED_PY_SERVER_HEADS
        or head.startswith("python")
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

# A MODULE-SCOPE FastAPI (ASGI) app assignment in a root module — the only python
# shape the detector can auto-start with `uvicorn <module>:app`. Anchored at column 0
# (no leading indentation): an INDENTED `app = FastAPI()` is a function-local (an
# app-factory like `def create_app(): app = FastAPI()`), which does NOT exist at module
# scope — emitting `main:app` for it would fabricate a non-existent entrypoint. A Flask
# (WSGI) app, an app-factory, or a nested/absent entrypoint is NOT auto-startable and
# fails closed (`entrypoint_unresolved`), never a fabricated module.
_FASTAPI_APP_RE = re.compile(r"(?m)^app\s*=\s*FastAPI\b")

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

# ---- WO-C4 build-env / package-manager / build-secret signals -----------------
#
# A Vite CLIENT build var — `import.meta.env.VITE_<NAME>`. Vite exposes ONLY the
# `VITE_`-prefixed vars to the client build (the built-ins — `MODE`, `DEV`, `PROD`,
# `BASE_URL`, `SSR` — are Vite-provided, never host-supplied), so this captures
# exactly the host-supplied BUILD-scope vars. They are compile-time constants
# embedded in the PUBLIC bundle, so a `VITE_`-prefixed name is public BY CONVENTION;
# a secret-shaped one is a leak and fails closed like any secret build var.
_VITE_BUILD_ENV_RE = re.compile(r"import\.meta\.env\.(VITE_[A-Za-z0-9_]+)")

# The node lockfiles, each naming exactly one package manager. TWO or more present
# is a package-manager DISAGREEMENT (`package_manager_conflict`, §8.9): the tree
# declares two managers and picking one by precedence would silently install the
# wrong dependency graph. Fail closed, never resolve by precedence.
_NODE_LOCKFILES = ("pnpm-lock.yaml", "yarn.lock", "package-lock.json")

# An `${NAME}` / `$NAME` env reference inside an `.npmrc` (an install-time auth
# token read at `npm ci`). A secret-shaped referenced NAME is a BUILD-time secret
# the install step needs — unsupported in a secret-free bundle, so it fails closed
# with `secret_build_env_unsupported` (§8.4).
_NPMRC_ENV_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_NPMRC_NAME = ".npmrc"

# ROOT lockfiles/manifests each naming a package manager the neutral base images do
# NOT provision. The node base runs npm (`package-lock.json` -> `npm ci`, or a bare
# `npm install`); the python base runs pip (`requirements.txt` / a plain `pyproject`).
# A lockfile naming bun / pnpm / yarn (node) or poetry / uv (python) is NOT silently
# mapped onto npm/pip (§8.8: "Merely mapping them to Node/Python is FAILURE") — its
# mere presence fails detection closed with `toolchain_unsupported`. The LIVE
# install/pin proof is a separate lane; here the STATIC contract is reject-not-map.
_UNSUPPORTED_NODE_PM: dict[str, str] = {
    "bun.lock": "bun",
    "bun.lockb": "bun",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
}
_UNSUPPORTED_PY_PM: dict[str, str] = {
    "poetry.lock": "poetry",
    "uv.lock": "uv",
}

# The node package managers the neutral base image does NOT provision. An
# AUTHORITATIVE non-lockfile declaration naming one of these — a corepack
# `packageManager` field, an unambiguous workspace/config marker, or a start/build
# script that invokes it — fails detection closed with `toolchain_unsupported` EVEN
# WITH NO COMMITTED LOCKFILE (§8.8: "Merely mapping them to Node/Python is FAILURE").
# npm (a `package-lock.json`, `packageManager:"npm@…"`, a `node`/`npm`/`npx` script
# head, or no PM signal) is the one the base image runs, so it never trips this.
_UNSUPPORTED_NODE_PM_NAMES = frozenset({"bun", "pnpm", "yarn"})

# ROOT config/workspace markers that UNAMBIGUOUSLY name a non-npm package manager by
# their mere presence (each file is that tool's own config), even when no lockfile is
# committed: a `pnpm-workspace.yaml` is pnpm-only, a `.yarnrc(.yml)` is a Yarn config,
# a `bunfig.toml` is Bun's config.
_NODE_WORKSPACE_MARKERS: dict[str, str] = {
    ".yarnrc": "yarn",
    ".yarnrc.yml": "yarn",
    "bunfig.toml": "bun",
    "pnpm-workspace.yaml": "pnpm",
    "pnpm-workspace.yml": "pnpm",
}

# The bun/pnpm/yarn launcher HEADS a `start`/`build`/`prebuild`/`postbuild` script may
# invoke, mapped to the package-manager FAMILY each names. A manager and its `x`-suffixed
# one-off runner belong to the SAME family — `bunx` is bun, `pnpx` is pnpm — so an
# `x`-launcher head is caught exactly like the bare manager (closeout #2: an exact-match
# {bun,pnpm,yarn} check let `bunx`/`pnpx` heads slip through to a false npm candidate).
# Every family names a toolchain the neutral base image does not provision.
_SCRIPT_HEAD_FAMILY: dict[str, str] = {
    "bun": "bun",
    "bunx": "bun",
    "pnpm": "pnpm",
    "pnpx": "pnpm",
    "yarn": "yarn",
}

# The `package.json` script keys the EMITTED bundle provably runs, so a non-npm launcher
# hidden in any of them crashes at bundle time (closeout #2 / #2d / #2e). Ordered by
# npm-lifecycle phase:
#   * `npm ci` (the emitted install step) runs, for the ROOT package, `preinstall` ->
#     `install` -> `postinstall` -> `preprepare` -> `prepare` -> `postprepare` — the last
#     three CONFIRMED on npm 10.9.7 (the emitted `node:22-bookworm-slim` image's npm major:
#     `npm ci` with no `--ignore-scripts` runs the prepare lifecycle), closeout #2e;
#   * `npm run build` (emitted only when a `build` script exists) runs `prebuild` -> `build`
#     -> `postbuild`;
#   * `npm start` (the emitted Dockerfile CMD) runs `prestart` -> `start` -> `poststart`.
# `prepublish*` / `prepack` / `postpack` are EXCLUDED — they run only on `npm publish` /
# `npm pack`, never on the emitted `npm ci` / `npm start`, so a launcher there is not a
# bundle crash and inspecting them would risk a false-block.
_TOOLCHAIN_SCRIPT_KEYS = (
    "preinstall",
    "install",
    "postinstall",
    "preprepare",
    "prepare",
    "postprepare",
    "prebuild",
    "build",
    "postbuild",
    "prestart",
    "start",
    "poststart",
)

# Sub-command separators in a shell script string: a launcher hidden in ANY sub-command of a
# chain (`cd app && bunx vite`, `true ; pnpm dev`, `a | pnpx b`, `a & bunx b`) runs at bundle
# time, so each sub-command's head is inspected independently (closeout #2d). The two-char
# `&&` / `||` are matched BEFORE their single-char `&` / `|` so an operator is never split
# mid-token.
_SHELL_CHAIN_RE = re.compile(r"&&|\|\||;|\||&")
# A leading `NAME=VALUE` shell env-assignment (`NODE_ENV=production bunx vite`): any number
# precede the real command head and are stripped before it is read (closeout #2d).
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# TRANSPARENT-PREFIX wrappers that exec the REST of their command line as a new command, so
# the effective launcher head sits AFTER them (`cross-env FOO=1 bunx vite` -> `bunx`,
# `dotenv -- bunx x` -> `bunx`). Each is stripped (with a single following `--` argv
# separator) when it heads a sub-command; unwrapping REPEATS so stacked wrappers
# (`env cross-env bunx x`) still resolve (closeout #2e). BOUNDED to these bare-prefix
# wrappers — a flag-argument form (`dotenv -e .env`, `nice -n 10`) is the documented ceiling.
_TRANSPARENT_WRAPPERS = frozenset({"corepack", "cross-env", "env", "exec", "dotenv"})


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
    `health_path_unresolved`, `runtime_conflict`, `persistent_path_unbackable`); `field`
    optionally names the
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


def _strip_comments(text: str) -> str:
    """Best-effort removal of JS block/line comments (`/* */`, `//`) and Python line
    comments (`#`) for the fail-open-prone LITERAL checks (the `$PORT` bind, the Vite
    `outDir`, the health route). A decoy planted in a comment (`// outDir: 'dist'`,
    `# process.env.PORT`, `// '/healthz'`) then no longer satisfies the contract.

    Used ONLY for those literal scans — never for the database / framework / env-name
    scans. Comment lexing is approximate (it does not track string context), but that
    is safe HERE: a mistaken strip only removes a would-be literal, making the check
    MORE conservative (fail-closed), never fail-open."""
    without_block = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    lines: list[str] = []
    for line in without_block.splitlines():
        cut = len(line)
        for marker in ("//", "#"):
            idx = line.find(marker)
            if idx != -1:
                cut = min(cut, idx)
        lines.append(line[:cut])
    return "\n".join(lines)


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
    # Strip comments first: a decoy `// … process.env.PORT` must not satisfy the bind.
    return pattern.search(_strip_comments(source)) is not None


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


def _build_env_decls(files: Mapping[str, str | bytes]) -> tuple[EnvVarDecl, ...]:
    """The BUILD-scope env vars a client build reads, discovered from source (§8.1 /
    §8.2). Currently the Vite `import.meta.env.VITE_<NAME>` form — a compile-time
    constant baked into the built bundle. Each is a `build`-scope `EnvVarDecl`
    (host-derived secret classification, fail-closed), so a public build var lowers to
    a Compose build arg + Dockerfile `ARG` (never a runtime `ENV`), and every
    statically discovered NAME is conserved in the spec rather than silently dropped.

    Bounded + binary-safe (§7.11): reads only `_scan_text` views, in a stable order."""
    names: set[str] = set()
    for path in sorted(files):
        names.update(_VITE_BUILD_ENV_RE.findall(_scan_text(files[path])))
    return tuple(
        EnvVarDecl(
            name=name,
            scope=EnvScope.build,
            required=True,
            secret=_classify_secret(name),
        )
        for name in sorted(names)
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
    # A poetry/uv lockfile names a toolchain the neutral python base image does NOT
    # provision; fail closed rather than silently mapping it onto `pip install` (§8.8).
    toolchain = _unsupported_pm_blocker(files, _UNSUPPORTED_PY_PM)
    if toolchain is not None:
        return toolchain
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
            # Strip comments first: a decoy `// outDir: 'dist'` must not be read as a
            # resolved output directory when the real config is dynamic/absent.
            text = _strip_comments(_scan_text(files[path]))
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
        # A build-requiring static bundle installs with a package manager too, so the
        # same disagreement / unsupported-toolchain / build-secret guards apply (§8.9 /
        # §8.8 / §8.4).
        conflict = _lockfile_conflict(files)
        if conflict is not None:
            return conflict
        toolchain = _unsupported_pm_blocker(files, _UNSUPPORTED_NODE_PM)
        if toolchain is not None:
            return toolchain
        # An authoritative non-lockfile PM declaration also rejects a build-requiring
        # static bundle (same no-lockfile bypass, same §8.8 reject-not-map contract).
        declared_toolchain = _unsupported_node_pm_declaration(files, pkg)
        if declared_toolchain is not None:
            return declared_toolchain
        build_secret = _secret_build_blocker(files)
        if build_secret is not None:
            return build_secret
        # A SOURCE-DISCOVERED secret build var (a secret-shaped `import.meta.env.VITE_*`)
        # fails closed like an `.npmrc` build secret — never folded into build.args (§8.4).
        source_secret = _secret_build_env_blocker(files)
        if source_secret is not None:
            return source_secret
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
        "python runtime evidence: a python manifest with a web-framework (fastapi/flask) entrypoint"
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
        # Strip comments first: a decoy `// '/healthz'` must not count as a real route.
        text = _strip_comments(_scan_text(files[path]))
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
        return _DbFinding(
            _DbKind.unknown, unknown_engine, tuple(dict.fromkeys(unknown_evidence)), False
        )
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
        if local_mount_target(resource) == "/":
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


def _intent_install(
    intent: ReleaseIntent,
    files: Mapping[str, str | bytes],
    runtime: RuntimeStrategy,
    *,
    force_node: bool = False,
) -> tuple[tuple[str, ...], str | None, str | None] | _DetectBlocker:
    """The `(install_cmd, package_manager, lockfile)` for a typed-intent candidate, or a
    `_DetectBlocker` when the package manager is ambiguous/unprovisioned (R2 / G04).

    An EXPLICIT `install_cmd` wins (carrying the declared package_manager / lockfile).
    Otherwise a SAFE default is derived ONLY where the manager + manifest are unambiguous:
    a node `package.json` (with declared dependencies, OR `force_node` for a build that
    always installs) → `npm ci` when a lockfile is present else `npm install`; a python
    `requirements.txt` → `pip install -r requirements.txt` (a bare `pyproject.toml` →
    `pip install .`). Derivation first re-applies the SOURCE-path toolchain guards — a
    lockfile disagreement, an unsupported bun/pnpm/yarn manager, an unsupported
    poetry/uv, a build-time `.npmrc` secret — so an unprovisioned/ambiguous tree fails
    closed instead of deriving a broken install. A stack with no manifest (or a
    no-dependency node app) installs nothing."""
    if intent.install_cmd:
        return (intent.install_cmd, intent.package_manager, intent.lockfile)
    if runtime is RuntimeStrategy.node or runtime is RuntimeStrategy.static:
        pkg = _root_package_json(files)
        if pkg is None:
            return ((), intent.package_manager, intent.lockfile)
        if not force_node and not _node_has_dependencies(pkg):
            return ((), intent.package_manager, intent.lockfile)
        conflict = _lockfile_conflict(files)
        if conflict is not None:
            return conflict
        toolchain = _unsupported_pm_blocker(files, _UNSUPPORTED_NODE_PM)
        if toolchain is not None:
            return toolchain
        declared = _unsupported_node_pm_declaration(files, pkg)
        if declared is not None:
            return declared
        build_secret = _secret_build_blocker(files)
        if build_secret is not None:
            return build_secret
        lockfile, manager, install = _node_install(files)
        return (install, manager, lockfile)
    if runtime is RuntimeStrategy.python:
        toolchain = _unsupported_pm_blocker(files, _UNSUPPORTED_PY_PM)
        if toolchain is not None:
            return toolchain
        tree = _paths(files)
        if "requirements.txt" in tree:
            return (("pip", "install", "-r", "requirements.txt"), "pip", None)
        if "pyproject.toml" in tree:
            return (("pip", "install", "."), "pip", None)
        return ((), intent.package_manager, intent.lockfile)
    return ((), intent.package_manager, intent.lockfile)


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


def _static_from_intent(intent: ReleaseIntent, files: Mapping[str, str | bytes]) -> DetectionResult:
    """A typed intent describing a STATIC site — a prebuilt tree served as-is, or a
    build-then-serve bundle whose built assets land in `output_dir` (R2 / G03 / G06). A
    static site is SERVED, not started, so it legitimately has no `start_cmd`. Fails
    closed on an unprovisioned toolchain or a build-time secret; otherwise a single
    static ingress served from `output_dir` (defaulting to the workspace root)."""
    manager_blocker = _declared_manager_blocker(intent)
    if manager_blocker is not None:
        return _fail_closed(manager_blocker)
    # A raw sidecar reaches emission without the tool's grammar gate, so re-apply it to
    # the build_cmd (start is empty here) + the migrate/positional credential rails.
    grammar = _command_grammar_blocker(intent)
    if grammar is not None:
        return _fail_closed(grammar)
    # R3 (G07): a static intent may still declare a stateful resource; a filesystem-ROOT
    # persistent path is unbackable (its mount dir is `/`), so fail closed here too.
    topology = _resource_topology_blocker(intent.resources)
    if topology is not None:
        return _fail_closed(topology)
    # A build ALWAYS installs first, so derive an install even for a no-dependency
    # package.json (force_node); a prebuilt static serve (no build_cmd) installs nothing.
    install_outcome = _intent_install(
        intent, files, RuntimeStrategy.static, force_node=bool(intent.build_cmd)
    )
    if isinstance(install_outcome, _DetectBlocker):
        return _fail_closed(install_outcome)
    install_cmd, package_manager, lockfile = install_outcome
    env = _intent_env_result(intent)
    if isinstance(env, _DetectBlocker):
        return _fail_closed(env)
    env = _intent_build_env(env, intent, files)
    if isinstance(env, _DetectBlocker):
        return _fail_closed(env)
    service = ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.static,
        package_manager=package_manager,
        lockfile=lockfile,
        install_cmd=install_cmd,
        build_cmd=intent.build_cmd,
        output_dir=intent.output_dir if intent.output_dir is not None else ".",
        port_env=intent.port_env,
        health_path=intent.health_path,
    )
    return DetectionResult(
        assessment=ReleaseAssessment.candidate,
        services=(service,),
        resources=intent.resources,
        env=env,
        evidence=(_intent_evidence(intent),),
        reasons=(
            "static release shape declared by a typed release intent "
            "(build command, output directory, and env names supplied).",
        ),
    )


def _intent_build_env(
    env: tuple[EnvVarDecl, ...],
    intent: ReleaseIntent,
    files: Mapping[str, str | bytes],
) -> tuple[EnvVarDecl, ...] | _DetectBlocker:
    """Source build-env parity for the typed-intent paths (R7 live-pubvite defect).

    The source-detection path conserves SOURCE-DISCOVERED public client build vars
    (`import.meta.env.VITE_*`) into the spec for a build-requiring service — they must
    lower to a Compose build arg + Dockerfile ARG or the built asset ships without
    them — and fails closed on a secret-SHAPED discovered build var
    (`secret_build_env_unsupported`, §8.4). The intent paths lowered only DECLARED
    env, so an undeclared public build var silently never reached the build and an
    undeclared secret-shaped one silently never rejected. Apply the same two source
    rules when the intent declares a `build_cmd`; a declared name keeps explicit-wins
    precedence (it is never duplicated or reclassified here)."""
    if not intent.build_cmd:
        return env
    secret = _secret_build_env_blocker(files)
    if secret is not None:
        return secret
    existing = {var.name for var in env}
    return env + tuple(decl for decl in _build_env_decls(files) if decl.name not in existing)


def _port_contract_start(start_cmd: tuple[str, ...], port_env: str) -> tuple[str, ...]:
    """Bind a declared bare ``uvicorn <module>:app`` start to the platform port
    contract (R7 live-fastapi defect). uvicorn's defaults are ``127.0.0.1:8000``, so a
    bare declaration can NEVER satisfy the contract the emitted bundle establishes
    (compose ``PORT``, ``EXPOSE``, loopback healthcheck) — the container is unreachable
    and never becomes healthy. Mirror the source-detection shape by appending
    ``--host 0.0.0.0 --port ${<port_env>}`` for EXACTLY that shape: head ``uvicorn``
    with neither ``--host`` nor ``--port`` declared (spaced or ``=``-form). An
    owner-declared binding always wins verbatim; every other command is untouched."""
    if not start_cmd or start_cmd[0].rsplit("/", 1)[-1].lower() != "uvicorn":
        return start_cmd
    for token in start_cmd[1:]:
        if token in ("--host", "--port") or token.startswith(("--host=", "--port=")):
            return start_cmd
    return start_cmd + ("--host", "0.0.0.0", "--port", "${" + port_env + "}")


def _from_intent(intent: ReleaseIntent, files: Mapping[str, str | bytes]) -> DetectionResult:
    # R2 (G03/G06): a static site (a prebuilt tree, or a build-then-serve bundle) is
    # SERVED, not started, so it has no `start_cmd`. It is recognized when the intent
    # declares a `static` runtime OR an `output_dir` (which only makes sense for a static
    # bundle) — routed to the static shape rather than the no-start needs_review below.
    if not intent.start_cmd:
        if intent.runtime is RuntimeStrategy.static or intent.output_dir is not None:
            return _static_from_intent(intent, files)
        # A release intent with NO start command AND no static shape cannot describe a
        # runnable app: there is no process to launch. Rather than fabricate a runnable
        # candidate (the emitter would default an empty start to `npm start`), fail
        # closed to needs_review naming the missing start command.
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
    # runner + any pip-package start executable declared as a dependency), and must not
    # name an unprovisioned package manager (pnpm/yarn/bun/…) in start/build/install.
    toolchain = _toolchain_blocker(intent, files)
    if toolchain is not None:
        return _fail_closed(toolchain)
    # WO-C5 #2 F1: the declared start/build argv must parse through the SAME runtime
    # command grammar the release_declare tool enforces, so a raw sidecar cannot bake a
    # secret CLI value (or an unsupported/opaque command) into the emitted bundle.
    grammar = _command_grammar_blocker(intent)
    if grammar is not None:
        return _fail_closed(grammar)
    # R3 (G07): a declared resource whose persistent_path is a filesystem-ROOT file cannot
    # be volume-backed (its mount dir is `/`, which would shadow the container root), so
    # fail closed rather than emit a self-host bundle whose state silently does not persist.
    topology = _resource_topology_blocker(intent.resources)
    if topology is not None:
        return _fail_closed(topology)
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
    # R2 (G03): the runtime strategy is the declared one, else inferred from the start
    # argv's interpreter token.
    runtime = intent.runtime if intent.runtime is not None else _runtime_from_argv(intent.start_cmd)
    # R2 (G04): a runtime dependency install no longer depends on a build step — an
    # interpreted candidate (declared deps, no build) installs them, or fails closed.
    install_outcome = _intent_install(intent, files, runtime)
    if isinstance(install_outcome, _DetectBlocker):
        return _fail_closed(install_outcome)
    install_cmd, package_manager, lockfile = install_outcome
    env = _intent_env_result(intent)
    if isinstance(env, _DetectBlocker):
        return _fail_closed(env)
    env = _intent_build_env(env, intent, files)
    if isinstance(env, _DetectBlocker):
        return _fail_closed(env)
    service = ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=runtime,
        package_manager=package_manager,
        lockfile=lockfile,
        install_cmd=install_cmd,
        build_cmd=intent.build_cmd,
        start_cmd=_port_contract_start(intent.start_cmd, intent.port_env),
        port_env=intent.port_env,
        health_path=intent.health_path,
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
    # Statically discovered BUILD-scope env NAMES (a Vite `import.meta.env.VITE_*`
    # client build var) are conserved in the spec so they lower to a build arg +
    # Dockerfile ARG (§8.1 / §8.2); a name already present (a runtime DB var) is not
    # duplicated. Only added for a build-requiring service — a served-as-is static
    # site or a plain runtime process has no build step to inject them into.
    if service.build_cmd:
        existing = {var.name for var in env}
        env = env + tuple(decl for decl in _build_env_decls(files) if decl.name not in existing)
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
