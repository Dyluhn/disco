"""Module-level regex / frozenset / tuple constants for release detection.

Pure data: every name here is a compiled pattern, a frozenset of recognized
tokens, or an immutable tuple/dict of literal values consulted by the
detectors in the sibling ``detect_parts`` modules. Nothing here executes
detection logic — see ``detect.py`` for the public facade these constants
back.
"""

from __future__ import annotations

import re

from pydantic import ConfigDict

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
_VITE_ENV_NAME_RE = re.compile(r"VITE_[A-Za-z0-9_]+\Z")


_VITE_SOURCE_SUFFIXES = (
    ".astro",
    ".cjs",
    ".cts",
    ".html",
    ".js",
    ".jsx",
    ".mjs",
    ".mts",
    ".svelte",
    ".ts",
    ".tsx",
    ".vue",
)


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
