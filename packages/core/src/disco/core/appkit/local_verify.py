"""AppKit EPIC G — the PURE, local Worker/D1 STRUCTURAL-model shim.

The strict app verifier (`verify_appkit_app`, tools layer) checks the lead flow +
the admin-auth contract WITHOUT a real Cloudflare deploy (the Epic I "locally
verifiable" decision). This module is the deploy-free MODEL: it runs the generated
`schema.sql` in an in-memory `sqlite3` database and exercises a MODEL of the
Worker's request-handling contract — driven entirely by the STRUCTURAL flags the
tool layer extracts from `worker/index.ts`, NOT by executing the real Worker.

HONEST SCOPE: `local_api_roundtrip` is a structural consistency model, not a
runtime proof. It shows the inspected structure (POST has a reachable parameterized
insert; reads are guard-first; missing ADMIN_TOKEN fails closed) is internally
coherent against a real schema — it does NOT execute the generated TypeScript, so
actual runtime behaviour/reachability is proved separately by
`packages/core/tests/test_workerd_persistence.py`, which runs the generated Worker
under local workerd/wrangler with real local D1 and a cold restart. The check name
`local_api_roundtrip` denotes a MODELLED round-trip, never a live API call. Hosted
Cloudflare deploy remains an owner-gated action outside this local model.

PURITY / LAYERING: stdlib (`sqlite3`) + the sibling `.spec` models only. No IO,
no network, no clock/random. `disco.core` is the leaf package (.importlinter), so
the browser/sandbox-dependent verifier checks (route/section coverage) stay in the
tools layer; this — pure sqlite + spec logic — lives in core.

The Worker's auth behaviour is modelled by :class:`WorkerAuthModel`, whose flags
the tool layer derives by STATICALLY inspecting the generated `worker/index.ts`.
So a generated worker that drops the admin guard (or stops failing closed) flips a
flag here and the modelled round-trip legs that depend on it FAIL — the model is
driven by the worker's inspected structure, not by an idealized contract.
"""

from __future__ import annotations

import fnmatch
import json
import math
import re
import sqlite3
import tomllib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from .spec import Entity

# A fixed, representative admin token used for the authenticated-read leg. The
# value is irrelevant (never persisted); the contract is "header must equal the
# configured token", so any stable non-empty string exercises it.
_ADMIN_TOKEN = "appkit-verify-admin-token"


@dataclass(frozen=True)
class WorkerAuthModel:
    """The Worker's read-auth contract, as inspected from `worker/index.ts`.

    * ``reads_require_auth`` — GET /api/leads AND /admin route through an
      authorization check before returning lead data.
    * ``fail_closed_without_token`` — that check DENIES when ADMIN_TOKEN is unset
      (the worker's ``if (!expected) return false`` fail-closed line), rather than
      defaulting open.
    * ``insert_parameterized`` — the POST insert uses the generated Drizzle table
      (`db.insert(leads).values(...)`), never string-concatenated SQL.
    * ``post_region_has_insert`` — the POST /api/leads handler region (the handler
      block + any helper it calls) STRUCTURALLY CONTAINS a parameterized
      ``db.insert(leads).values(...).run()`` lead insert in a non-dead position (not after
      an unconditional early return, not inside an `if (false)`/`if (0)` branch).
      This is structural PRESENCE + ordering, NOT a runtime proof that a submission
      persists — actual local reachability is covered by
      `packages/core/tests/test_workerd_persistence.py`. A POST route whose body
      short-circuits to ``return json({ ok: true })`` while an insert merely exists
      in an unreached function does NOT have the insert in-region, so the modelled
      persist leg fails instead of structurally passing on an absent / dead insert.
    """

    reads_require_auth: bool
    fail_closed_without_token: bool
    insert_parameterized: bool
    post_region_has_insert: bool = True


@dataclass(frozen=True)
class CheckResult:
    """One verifier leg: pass/fail + a short human evidence string."""

    name: str
    passed: bool
    evidence: str


def _connect(schema_sql: str) -> sqlite3.Connection:
    """An in-memory sqlite db with the generated schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema_sql)
    return conn


def _table_name(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _representative_value(field_name: str, field_type: str) -> str:
    """A schema-valid representative value for a lead field (email-shaped where the
    worker's email validation would apply)."""
    low_name = field_name.lower()
    low_type = field_type.strip().lower()
    if low_name == "email" or low_type == "email":
        return "lead@example.com"
    return f"sample {field_name}"[:64]


def check_schema_sql(schema_sql: str, lead: Entity) -> CheckResult:
    """Execute `schema.sql` in in-memory sqlite, insert a representative lead row
    (parameterized) from the resolved lead entity, read it back, and assert that
    every REQUIRED column rejects NULL. Pure — no deploy, no IO."""
    try:
        conn = _connect(schema_sql)
    except sqlite3.Error as exc:
        return CheckResult("schema_sql_valid", False, f"schema.sql is not valid SQL: {exc}")
    try:
        table = _table_name(conn)
        if table is None:
            return CheckResult("schema_sql_valid", False, "schema.sql created no table.")
        cols = [f.name for f in lead.fields]
        if not cols:
            return CheckResult(
                "schema_sql_valid", False, "the lead entity declares no fields to persist."
            )
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(f'"{c}"' for c in cols)
        values = [_representative_value(f.name, f.type) for f in lead.fields]
        try:
            conn.execute(
                f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})', values
            )
        except sqlite3.Error as exc:
            return CheckResult(
                "schema_sql_valid", False, f"a representative lead row failed to insert: {exc}"
            )
        back = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        if not back or back[0] != 1:
            return CheckResult(
                "schema_sql_valid", False, "the inserted lead row could not be read back."
            )
        # Required columns must reject NULL (NOT NULL enforced by the schema).
        required = [f.name for f in lead.fields if f.required]
        for req in required:
            row_vals = [
                None if f.name == req else _representative_value(f.name, f.type)
                for f in lead.fields
            ]
            try:
                conn.execute(
                    f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})', row_vals
                )
            except sqlite3.IntegrityError:
                continue  # good — NULL rejected
            return CheckResult(
                "schema_sql_valid",
                False,
                f'required column "{req}" accepted NULL (missing NOT NULL constraint).',
            )
        return CheckResult(
            "schema_sql_valid",
            True,
            f'table "{table}": inserted + read back a lead row; '
            f"{len(required)} required column(s) reject NULL.",
        )
    finally:
        conn.close()


def _tree_text(tree: Mapping[str, str | bytes | None], path: str) -> str | None:
    value = tree.get(path)
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _parse_schema_sql_columns(schema_sql: str) -> list[tuple[str, bool]] | None:
    m = re.search(
        r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"[^"]+"\s*\(\s*(.*?)\s*\)\s*;',
        schema_sql,
        re.IGNORECASE | re.DOTALL,
    )
    if m is None:
        return None
    cols: list[tuple[str, bool]] = []
    for raw in m.group(1).splitlines():
        line = raw.strip().rstrip(",")
        if not line:
            continue
        cm = re.match(r'"([^"]+)"\s+(?:TEXT|INTEGER|REAL)\b(.*)$', line, re.IGNORECASE)
        if cm is None:
            return None
        cols.append((cm.group(1), "NOT NULL" in cm.group(2).upper()))
    return cols


def _parse_drizzle_schema_columns(schema_ts: str) -> list[tuple[str, bool]] | None:
    if re.search(r"\bexport\s+const\s+leads\s*=\s*sqliteTable\s*\(", schema_ts) is None:
        return None
    cols: list[tuple[str, bool]] = []
    for m in re.finditer(
        r'^\s*(?:"[^"]+"|[A-Za-z_$][\w$]*)\s*:\s*'
        r'(?:text|integer|real)\(\s*"([^"]+)"\s*\)([^\n]*)',
        schema_ts,
        re.MULTILINE,
    ):
        cols.append((m.group(1), ".notNull()" in m.group(2)))
    return cols


def check_drizzle_schema(tree: Mapping[str, str | bytes | None]) -> CheckResult:
    """Validate the generated Drizzle schema against the generated D1 migration.

    Pure string-structural check: no Node/npm execution. Because both files are
    owned by the generator, parsing the emitted shapes is enough to prove the Drizzle
    column set (names + notNull flags) still matches `schema.sql`, and that the app
    declares the Drizzle runtime/tooling dependencies it imports.
    """
    name = "drizzle_schema_valid"
    schema_sql = _tree_text(tree, "schema.sql")
    schema_ts = _tree_text(tree, "src/db/schema.ts")
    package_json = _tree_text(tree, "package.json")
    if schema_sql is None or not schema_sql.strip():
        return CheckResult(name, False, "missing schema.sql.")
    if schema_ts is None or not schema_ts.strip():
        return CheckResult(name, False, "missing src/db/schema.ts.")
    if package_json is None or not package_json.strip():
        return CheckResult(name, False, "missing package.json.")

    sql_cols = _parse_schema_sql_columns(schema_sql)
    if sql_cols is None or not sql_cols:
        return CheckResult(name, False, "schema.sql columns could not be parsed.")
    drizzle_cols = _parse_drizzle_schema_columns(schema_ts)
    if drizzle_cols is None or not drizzle_cols:
        return CheckResult(
            name,
            False,
            "src/db/schema.ts does not declare export const leads = sqliteTable(...).",
        )
    if drizzle_cols != sql_cols:
        return CheckResult(
            name,
            False,
            "src/db/schema.ts columns do not match schema.sql "
            f"(drizzle={drizzle_cols!r}, sql={sql_cols!r}).",
        )

    try:
        pkg = json.loads(package_json)
    except json.JSONDecodeError as exc:
        return CheckResult(name, False, f"package.json is not valid JSON: {exc}")
    if not isinstance(pkg, dict):
        return CheckResult(name, False, "package.json is not a JSON object.")
    deps = pkg.get("dependencies")
    dev_deps = pkg.get("devDependencies")
    if not isinstance(deps, dict) or "drizzle-orm" not in deps:
        return CheckResult(
            name,
            False,
            "package.json dependencies must declare drizzle-orm.",
        )
    if not isinstance(dev_deps, dict) or "drizzle-kit" not in dev_deps:
        return CheckResult(
            name,
            False,
            "package.json devDependencies must declare drizzle-kit.",
        )
    return CheckResult(
        name,
        True,
        "src/db/schema.ts sqliteTable columns match schema.sql (names + notNull flags); "
        "package.json declares drizzle-orm and drizzle-kit.",
    )


def _is_authorized(header: str | None, env_token: str | None, auth: WorkerAuthModel) -> bool:
    """A faithful Python model of the generated worker's ``isAuthorized`` + read
    gating. Mirrors: a missing ADMIN_TOKEN fails closed; otherwise the request must
    carry ``Authorization: Bearer <token>`` matching the configured token."""
    if not auth.reads_require_auth:
        return True  # no gate → an unauthenticated caller is "authorized" (a leak)
    if not env_token:
        return not auth.fail_closed_without_token  # unset token → deny iff fail-closed
    if not header or not header.startswith("Bearer "):
        return False
    return header[len("Bearer "):] == env_token


def local_api_roundtrip(
    schema_sql: str, lead: Entity, auth: WorkerAuthModel
) -> CheckResult:
    """Run a MODEL of the Worker's request handling against in-memory sqlite from
    `schema.sql` + the resolved lead entity. This does NOT execute the generated
    Worker — it exercises the behaviour implied by the inspected structural flags
    (`auth`) and checks that structure is internally CONSISTENT, WITHOUT a real CF
    deploy. The real local runtime proof lives in
    `packages/core/tests/test_workerd_persistence.py` (workerd/wrangler + local D1 +
    cold restart); hosted Cloudflare deploy remains owner-gated. The model confirms:

      1. the modelled POST /api/leads with valid JSON INSERTS (the row appears);
      2. a modelled UNauthenticated GET /api/leads is REJECTED (401);
      3. a modelled UNauthenticated GET /admin is REJECTED (401);
      4. a modelled AUTHENTICATED read returns the inserted row;
      5. with ADMIN_TOKEN UNSET the modelled worker FAILS CLOSED (reads denied).

    Returns a single pass/fail with the first inconsistent leg named in the evidence.
    """
    try:
        conn = _connect(schema_sql)
    except sqlite3.Error as exc:
        return CheckResult("local_api_roundtrip", False, f"schema.sql is not valid SQL: {exc}")
    try:
        table = _table_name(conn)
        if table is None:
            return CheckResult("local_api_roundtrip", False, "schema.sql created no table.")
        cols = [f.name for f in lead.fields]
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(f'"{c}"' for c in cols)

        def post(record: dict[str, str]) -> int:
            # Mirror the worker: known keys only, required present, parameterized insert.
            known = set(cols)
            if any(k not in known for k in record):
                return 400
            if any(not record.get(f.name) for f in lead.fields if f.required):
                return 400
            conn.execute(
                f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
                [record.get(c) for c in cols],
            )
            return 201

        def read(header: str | None, env_token: str | None) -> tuple[int, int]:
            if not _is_authorized(header, env_token, auth):
                return 401, 0
            n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            return 200, n

        # 1. POST valid → insert. The inspected POST region must CONTAIN the insert in
        # a reachable (non-dead) position; if the structural inspection shows no insert
        # in-region (it only exists in an unreached function, or sits after an early
        # return / inside a dead branch), the modelled submission cannot persist — fail
        # here rather than letting the in-memory model insert on the worker's behalf and
        # structurally pass on an absent/dead insert. (Structural presence, not a
        # runtime persistence proof — that is Epic I.)
        if not auth.post_region_has_insert:
            return CheckResult(
                "local_api_roundtrip",
                False,
                "the inspected POST /api/leads region does not CONTAIN a reachable lead "
                "insert (no db.insert(leads).values(...).run() in the handler or a helper it "
                "calls, or it sits after an early return / inside a dead branch), so the "
                "modelled submission cannot persist. Place the Drizzle insert in "
                "the POST handler's reachable path.",
            )
        record = {f.name: _representative_value(f.name, f.type) for f in lead.fields}
        if post(record) != 201:
            return CheckResult(
                "local_api_roundtrip", False, "POST /api/leads with valid JSON did not persist."
            )
        if conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != 1:
            return CheckResult(
                "local_api_roundtrip", False, "the posted lead did not appear in the table."
            )

        # 2 + 3. Unauthenticated reads (token configured) must be 401.
        status_leads, _ = read(None, _ADMIN_TOKEN)
        if status_leads != 401:
            return CheckResult(
                "local_api_roundtrip",
                False,
                "an UNauthenticated GET /api/leads was NOT rejected (leads are exposed). "
                "Gate GET /api/leads behind an Authorization Bearer token.",
            )
        status_admin, _ = read(None, _ADMIN_TOKEN)  # /admin shares the same gate model
        if status_admin != 401:
            return CheckResult(
                "local_api_roundtrip",
                False,
                "an UNauthenticated GET /admin was NOT rejected. "
                "Gate /admin behind an Authorization Bearer token.",
            )

        # 4. Authenticated read returns the row.
        status_auth, n_auth = read(f"Bearer {_ADMIN_TOKEN}", _ADMIN_TOKEN)
        if status_auth != 200 or n_auth != 1:
            return CheckResult(
                "local_api_roundtrip",
                False,
                "an AUTHENTICATED read did not return the persisted lead row.",
            )

        # 5. ADMIN_TOKEN unset → fail closed (reads denied even with a bearer header).
        status_closed, _ = read(f"Bearer {_ADMIN_TOKEN}", None)
        if status_closed != 401:
            return CheckResult(
                "local_api_roundtrip",
                False,
                "with ADMIN_TOKEN UNSET the worker did NOT fail closed (reads still served). "
                "Deny reads when ADMIN_TOKEN is unset.",
            )

        return CheckResult(
            "local_api_roundtrip",
            True,
            "modelled round-trip consistent: POST inserts; unauth GET/admin → 401; "
            "authed read returns the row; ADMIN_TOKEN unset fails closed "
            "(structural model; local runtime proof is "
            "packages/core/tests/test_workerd_persistence.py).",
        )
    finally:
        conn.close()


# ---- Epic I — Cloudflare export readiness -------------------------------------

# The deliverables a Cloudflare-ready export MUST contain (Epic E emits the first
# three; Epic I adds the owner guide, the secret template, and the .gitignore that
# keeps the real secret out of version control). The verifier reads each from the
# workspace and passes their text to `cloudflare_export_ready`.
CF_EXPORT_FILES: tuple[str, ...] = (
    "wrangler.toml",
    "schema.sql",
    "worker/index.ts",
    "OWNER_GUIDE.md",
    ".dev.vars.example",
    ".gitignore",
)

# Paths the Worker (not the static-asset/SPA layer) must own, so a navigation to the
# admin read-back or an /api/* call always reaches worker/index.ts.
_CF_WORKER_FIRST_ROUTES: tuple[str, ...] = ("/api/*", "/admin")

# The Worker entry `main` MUST point at — a navigation/`/api/*` call reaches the
# generated worker only if `main` names it. The generator emits exactly
# ``main = "worker/index.ts"`` (see generator._emit/`generate`), so this is the EXACT
# canonical target. Only a leading ``./`` (`./worker/index.ts`, the same FILE) is
# tolerated — see :func:`main_points_at_worker_entry`.
_CF_WORKER_ENTRY: str = "worker/index.ts"
#: The canonical worker-entry segments — what ``main`` must resolve to EXACTLY.
_CF_WORKER_ENTRY_PARTS: tuple[str, ...] = PurePosixPath(_CF_WORKER_ENTRY).parts


def main_points_at_worker_entry(main: object) -> bool:
    """Does wrangler.toml ``main`` point EXACTLY at the canonical worker entry
    (``worker/index.ts``)? The single source of truth for the ``main`` entrypoint
    check, shared by export-readiness (here) AND the deploy gate (deploy.py), so the
    file the canonical-worker match validates is GUARANTEED to be the file
    ``wrangler deploy`` runs as ``main``.

    NO ``lstrip`` — ``main.lstrip("./")`` strips ALL leading ``.``/``/`` *characters*
    (the SEC-1 ``[assets].directory`` bug class), so ``"....worker/index.ts"`` would
    collapse to ``"worker/index.ts"`` and FALSELY compare equal — while wrangler
    resolves it as a file inside a DIFFERENT directory literally named ``....worker``
    (an UNCHECKED, non-canonical worker; also dodges the SEC-10 ownership preflight).

    Instead we resolve ``main`` the way wrangler does and require the segments to be
    EXACTLY ``("worker", "index.ts")``:

      * accept ``worker/index.ts`` and ``./worker/index.ts`` (a leading ``./`` /
        interior ``.`` segments resolve to the SAME file);
      * REJECT an ABSOLUTE path (``/etc/x``), any ``..`` segment (``../evil.ts``),
        a different directory/file (``....worker/index.ts``, ``other/index.ts``), a
        non-string / empty value — anything that normalizes-equal-but-resolves-
        different.
    """
    if not isinstance(main, str) or not main.strip():
        return False
    p = PurePosixPath(main.strip())
    if p.is_absolute():
        return False
    parts = tuple(seg for seg in p.parts if seg != ".")
    if any(seg == ".." for seg in parts):
        return False
    return parts == _CF_WORKER_ENTRY_PARTS

# The static-asset/SPA fallback the export MUST declare so client-side routes resolve
# to index.html (without it the deep links 404 instead of hydrating the SPA).
_CF_SPA_NOT_FOUND: str = "single-page-application"

# The exact .gitignore rule that keeps the REAL local secret file out of git. Matched
# verbatim (a bare `.dev.vars` line) so it can never be satisfied by `.dev.vars.example`
# (the committed template) or by a `!.dev.vars` negation.
_CF_GITIGNORE_SECRET_RULE: str = ".dev.vars"

# Placeholder markers that make a `.dev.vars.example` value SELF-EVIDENTLY a template
# (never a real secret), regardless of length/entropy.
_CF_PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "replace",
    "change-me",
    "changeme",
    "your-",
    "your_",
    "example",
    "placeholder",
    "placehold",
    "todo",
    "dummy",
    "sample",
    "xxx",
    "here",
    "goes-here",
    "<",
    ">",
)


def _gitignore_pattern_matches(pattern: str, path: str) -> bool:
    """Does a single .gitignore `pattern` (the `!` already stripped) match `path`?

    Scoped to the flat, root-level secret path (`.dev.vars`) — NOT a full gitignore
    engine. Handles leading-slash root anchoring and glob wildcards (`*`, `?`, `[..]`)
    via `fnmatch`; a directory-only pattern (trailing `/`) never matches the secret
    FILE. So `.dev.vars`, `/.dev.vars`, `*.vars`, and `.dev.*` all match `.dev.vars`,
    while `.dev.vars.example` does not.
    """
    if pattern.endswith("/"):
        # Directory-only rule — the secret is a regular file, so it cannot match.
        return False
    if pattern.startswith("/"):
        pattern = pattern[1:]
    return fnmatch.fnmatchcase(path, pattern)


def _gitignore_ignores(text: str, rule: str) -> bool:
    """True iff the path `rule` (e.g. `.dev.vars`) is NET ignored after applying every
    `.gitignore` line in order with gitignore's LAST-MATCH-WINS precedence.

    Each non-blank, non-comment line is a pattern: a matching positive pattern marks
    the path ignored, a matching `!`-negation un-ignores it, and the LAST line that
    matches the path decides. So `.dev.vars` followed by `!.dev.vars` leaves the file
    NOT ignored (it would be TRACKED → secret leak → secret-safety FAILS), while
    `!.dev.vars` followed by `.dev.vars` leaves it ignored. A bare `.dev.vars.example`
    template line never matches `.dev.vars`, so it cannot satisfy the rule.
    """
    ignored = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = line[1:] if negate else line
        if not pattern:
            continue
        if _gitignore_pattern_matches(pattern, rule):
            ignored = not negate
    return ignored


def _dev_vars_assignments(text: str) -> list[tuple[str, str]]:
    """Every `KEY=value` assignment in a dotenv-style `.dev.vars(.example)` body, in
    order, as (name, value) pairs (comments/blank lines skipped, quotes stripped).
    Used to scan ALL values for hidden secrets, not just the first match for a key."""
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        out.append((name.strip(), val.strip().strip('"').strip("'")))
    return out


def _dev_vars_value(text: str, key: str) -> str | None:
    """Extract the assigned value of `key` from a dotenv-style `.dev.vars(.example)`
    body (first non-comment `KEY=value` line). None if the key is absent."""
    for name, val in _dev_vars_assignments(text):
        if name == key:
            return val
    return None


def _is_high_entropy_token(token: str) -> bool:
    """Does a single contiguous `token` look like a real secret — long (>=16 chars)
    AND high-entropy (>=3.0 bits/char)? Shannon entropy per character: random secrets
    pack many distinct symbols; a human-readable word repeats letters and scores well
    below ~3 bits."""
    if len(token) < 16:
        return False
    counts = Counter(token)
    n = len(token)
    entropy = -sum((c / n) * math.log2(c / n) for c in counts.values())
    return entropy >= 3.0


def _looks_like_real_secret(value: str) -> bool:
    """Heuristic: does `value` look like a REAL high-entropy secret (so it must NEVER
    ship in `.dev.vars.example`), rather than a placeholder?

    A placeholder MARKER (replace-me / your-… / etc.) does NOT, by itself, whitelist a
    value. A marker only makes a value safe when, AFTER the marker text is removed,
    what REMAINS is not itself a real-secret-looking token. So a value that splices a
    marker onto a real secret in the SAME assignment — e.g.
    `replace-me-<32-char-high-entropy-secret>` — still FAILS: the embedded secret
    cannot hide behind the marker. Legit SHORT placeholders (`replace-me`, `changeme`,
    `your-api-key-here`, `replace_me_with_token`) collapse, once their markers and the
    surrounding connective words are split apart, to short low-entropy fragments and
    stay safe.
    """
    v = value.strip()
    if not v:
        return False
    # Strip every placeholder marker (case-insensitively) from the value, then judge
    # the REMAINDER — never short-circuit "safe" on marker presence alone. Markers are
    # removed by splicing the surrounding text together (no separator inserted), so a
    # secret cannot be broken up merely because it happens to contain a marker word.
    residue = v
    for marker in _CF_PLACEHOLDER_MARKERS:
        idx = residue.lower().find(marker)
        while idx != -1:
            residue = residue[:idx] + residue[idx + len(marker):]
            idx = residue.lower().find(marker)
    # A real secret is a single contiguous high-entropy token. Split the residue on
    # non-alphanumeric separators so the connective words of a legit placeholder
    # (`me`, `with`, `token`) don't accrete into one long pseudo-secret; if ANY single
    # leftover token is long + high-entropy, the value carries a real secret.
    return any(_is_high_entropy_token(tok) for tok in re.split(r"[^A-Za-z0-9]+", residue))

# Deploy steps the OWNER_GUIDE must document (Disco never runs them — it documents
# them). Substring-checked so the guide stays the single source of the deploy flow.
_CF_GUIDE_REQUIRED_STEPS: tuple[str, ...] = (
    "wrangler d1 create",
    "wrangler secret put ADMIN_TOKEN",
    "wrangler deploy",
)


def cloudflare_export_ready(files: Mapping[str, str | None]) -> CheckResult:
    """STRUCTURAL Cloudflare-export readiness (Epic I) — driven entirely by the
    generated config + docs, no deploy and no network.

    `files` maps each export path to its text (None/absent = missing). Recognised
    keys: the `CF_EXPORT_FILES` deliverables, plus an OPTIONAL `.dev.vars` (a real
    local-secret file that must NEVER be in the export — present ⇒ fail).

    The check confirms the export is the COMPLETE deployable contract — every missing
    piece FAILS with a specific reason (a false PASS on a half-built export is the
    worst outcome, so each requirement is enforced, not assumed):

      1. every required deliverable is present and non-empty (incl. `.gitignore`);
      2. `wrangler.toml` parses and its Worker entry `main` points at the generated
         worker (`worker/index.ts`) — else `/api/*` + `/admin` never reach the Worker;
      3. the `[[d1_databases]]` entry binds `DB` AND names a non-empty `database_name`
         (the Worker can't reach a database it can't name);
      4. `[assets]` binds `ASSETS` AND declares single-page-application
         `not_found_handling` (so client routes resolve to index.html);
      5. `assets.run_worker_first` is present AND covers BOTH `/api/*` and `/admin`,
         so the SPA asset layer cannot shadow the lead API + admin read-back — this is
         the Epic I routing fix and is REQUIRED unconditionally;
      6. the secret contract holds: `.gitignore` ignores the real `.dev.vars` file,
         `.dev.vars.example` templates `ADMIN_TOKEN` as a PLACEHOLDER only (no real
         high-entropy secret), and NO real `.dev.vars` is present — so a real admin
         secret can never be shipped or committed;
      7. `OWNER_GUIDE.md` documents the deploy steps the owner runs.

    This is config + doc completeness, NOT a runtime deploy proof: the real edge run
    is the owner's `npm run cf:dev` / `wrangler deploy` (documented in the guide), not
    something Disco executes.
    """
    name = "cloudflare_export_ready"

    missing = [p for p in CF_EXPORT_FILES if not (files.get(p) or "").strip()]
    if missing:
        return CheckResult(
            name,
            False,
            "missing Cloudflare export deliverable(s): "
            f"{', '.join(missing)}. Re-run app_create to regenerate the export tree.",
        )

    try:
        cfg = tomllib.loads(files["wrangler.toml"] or "")
    except tomllib.TOMLDecodeError as exc:
        return CheckResult(name, False, f"wrangler.toml is not valid TOML: {exc}")

    main = cfg.get("main")
    if not main_points_at_worker_entry(main):
        return CheckResult(
            name,
            False,
            'wrangler.toml Worker entry main does not EXACTLY name the worker '
            f'(main = "{_CF_WORKER_ENTRY}") — a non-canonical main (an absolute path, a '
            "'..' escape, or a look-alike dir like '....worker/index.ts') would deploy a "
            "DIFFERENT, unchecked Worker while /api/* + /admin never reach the generated "
            "one.",
        )

    d1 = cfg.get("d1_databases")
    db_entry = (
        next(
            (b for b in d1 if isinstance(b, dict) and b.get("binding") == "DB"),
            None,
        )
        if isinstance(d1, list)
        else None
    )
    if db_entry is None:
        return CheckResult(
            name,
            False,
            'wrangler.toml has no [[d1_databases]] entry binding "DB" — the Worker '
            "cannot reach the leads database.",
        )
    db_database_name = db_entry.get("database_name")
    if not (isinstance(db_database_name, str) and db_database_name.strip()):
        return CheckResult(
            name,
            False,
            'wrangler.toml [[d1_databases]] binds "DB" but has no non-empty '
            "database_name — the Worker cannot target a database it can't name. "
            "Set database_name to the D1 database created with `wrangler d1 create`.",
        )

    assets = cfg.get("assets")
    if not (isinstance(assets, dict) and assets.get("binding") == "ASSETS"):
        return CheckResult(
            name,
            False,
            'wrangler.toml [assets] does not bind "ASSETS" for the built SPA.',
        )

    if assets.get("not_found_handling") != _CF_SPA_NOT_FOUND:
        return CheckResult(
            name,
            False,
            "wrangler.toml [assets] does not set "
            f'not_found_handling = "{_CF_SPA_NOT_FOUND}" — client-side routes would '
            "404 instead of resolving to index.html.",
        )

    rwf = assets.get("run_worker_first")
    routes = set(rwf) if isinstance(rwf, list) else set()
    if rwf is not True and not set(_CF_WORKER_FIRST_ROUTES) <= routes:
        return CheckResult(
            name,
            False,
            "wrangler.toml [assets] does not route "
            f"{', '.join(_CF_WORKER_FIRST_ROUTES)} worker-first "
            "(assets.run_worker_first) — the single-page-application asset layer would "
            "shadow the lead API + admin read-back with index.html.",
        )

    # --- secret-safety contract -------------------------------------------------
    real_secret = files.get(".dev.vars")
    if real_secret is not None and real_secret.strip():
        return CheckResult(
            name,
            False,
            "a real .dev.vars file is present in the export — secrets must never be "
            "generated or committed (only the .dev.vars.example template). Remove .dev.vars.",
        )

    gitignore = files.get(".gitignore") or ""
    if not _gitignore_ignores(gitignore, _CF_GITIGNORE_SECRET_RULE):
        return CheckResult(
            name,
            False,
            ".gitignore does not ignore the real `.dev.vars` secret file — a developer's "
            "local `.dev.vars` (with a real ADMIN_TOKEN) could be committed. Add a "
            "`.dev.vars` line to .gitignore.",
        )

    example = files[".dev.vars.example"] or ""
    assignments = _dev_vars_assignments(example)
    if not any(name == "ADMIN_TOKEN" for name, _ in assignments):
        return CheckResult(
            name,
            False,
            ".dev.vars.example does not declare ADMIN_TOKEN — the admin read-back "
            "secret has no documented local template.",
        )
    # Scan EVERY assignment's value (every occurrence of every key), not just the first
    # ADMIN_TOKEN: a real secret hidden after a placeholder line — or on any other KEY —
    # must never escape, regardless of position, order, or duplication.
    leaked_key = next(
        (key for key, val in assignments if _looks_like_real_secret(val)), None
    )
    if leaked_key is not None:
        return CheckResult(
            name,
            False,
            f".dev.vars.example carries what looks like a REAL secret on {leaked_key}, not a "
            "placeholder — the template must ship only placeholders (e.g. "
            "ADMIN_TOKEN=replace-me). Replace it so no real secret is committed.",
        )

    guide = files["OWNER_GUIDE.md"] or ""
    missing_steps = [s for s in _CF_GUIDE_REQUIRED_STEPS if s not in guide]
    if missing_steps:
        return CheckResult(
            name,
            False,
            "OWNER_GUIDE.md does not document the required deploy step(s): "
            f"{', '.join(missing_steps)}.",
        )

    return CheckResult(
        name,
        True,
        "Cloudflare export complete: wrangler.toml main → worker/index.ts, binds DB "
        f"(database_name={db_database_name!r}) + ASSETS with single-page-application "
        "fallback and worker-first /api/* + /admin routing; .gitignore ignores the real "
        ".dev.vars; .dev.vars.example templates ADMIN_TOKEN as a placeholder (no real "
        "secret in the tree); OWNER_GUIDE.md documents d1 create, schema load, secret "
        "put, and deploy. STRUCTURAL export-readiness — the owner runs the deploy "
        "(Disco never deploys).",
    )


# ---- Epic N — STATIC (directory) Cloudflare export readiness -------------------

# The deliverables a STATIC-primitive (directory) export MUST contain. Unlike the
# lead-gen export there is NO `.dev.vars.example` (the static site has no secret) —
# `schema.sql` is still emitted (table-free) so a same-path overwrite of a prior
# lead-gen app leaves no stale schema, but the D1 binding + admin-secret contract
# does not apply.
STATIC_CF_EXPORT_FILES: tuple[str, ...] = (
    "wrangler.toml",
    "schema.sql",
    "worker/index.ts",
    "OWNER_GUIDE.md",
    ".gitignore",
)

# Deploy steps a STATIC-site OWNER_GUIDE must document (no D1/secret steps — just
# build + publish).
_CF_STATIC_GUIDE_REQUIRED_STEPS: tuple[str, ...] = (
    "npm run build",
    "wrangler deploy",
)

# A STATIC primitive carries NO server data plane, so its schema.sql must be
# table-free. This matches a `CREATE TABLE` DDL statement (any whitespace, optional
# `IF NOT EXISTS`) case-insensitively — a comment-only placeholder schema.sql does NOT
# match, but a lead-gen leftover `CREATE TABLE leads (...)` does.
_CF_CREATE_TABLE_RE = re.compile(r"\bcreate\s+table\b", re.IGNORECASE)


def cloudflare_export_ready_static(files: Mapping[str, str | None]) -> CheckResult:
    """STRUCTURAL Cloudflare-export readiness for a STATIC primitive (directory).

    Like `cloudflare_export_ready` but for a site with NO server data plane: it
    requires the static deliverables, a `wrangler.toml` whose `main` points at the
    Worker and whose `[assets]` binds `ASSETS` with single-page-application fallback,
    and an `OWNER_GUIDE.md` documenting the build + deploy steps. It does NOT require
    a D1 binding, `run_worker_first` routing, or an admin-secret template — those have
    no meaning for a static site. It STILL fails closed on a leaked secret: a real
    `.dev.vars` in the export is rejected. Config + doc completeness, NOT a deploy."""
    name = "cloudflare_export_ready"

    missing = [p for p in STATIC_CF_EXPORT_FILES if not (files.get(p) or "").strip()]
    if missing:
        return CheckResult(
            name,
            False,
            "missing Cloudflare export deliverable(s): "
            f"{', '.join(missing)}. Re-run app_create to regenerate the export tree.",
        )

    try:
        cfg = tomllib.loads(files["wrangler.toml"] or "")
    except tomllib.TOMLDecodeError as exc:
        return CheckResult(name, False, f"wrangler.toml is not valid TOML: {exc}")

    main = cfg.get("main")
    if not (isinstance(main, str) and main.lstrip("./") == _CF_WORKER_ENTRY):
        return CheckResult(
            name,
            False,
            'wrangler.toml Worker entry main does not point at the worker '
            f'(main = "{_CF_WORKER_ENTRY}") — the static-asset Worker would not serve.',
        )

    # NEGATIVE assertion: a STATIC primitive has NO server data plane, so a D1 binding
    # is a lead-gen leftover that must FAIL verify (not pass). A directory app that
    # still carries `[[d1_databases]]` is a half-converted lead-gen tree.
    if cfg.get("d1_databases"):
        return CheckResult(
            name,
            False,
            "wrangler.toml declares a [[d1_databases]] binding, but a STATIC directory "
            "site has no D1 data plane — this is a lead-gen leftover. Remove the "
            "[[d1_databases]] binding (a static primitive ships no database).",
        )

    assets = cfg.get("assets")
    if not (isinstance(assets, dict) and assets.get("binding") == "ASSETS"):
        return CheckResult(
            name,
            False,
            'wrangler.toml [assets] does not bind "ASSETS" for the built SPA.',
        )
    if assets.get("not_found_handling") != _CF_SPA_NOT_FOUND:
        return CheckResult(
            name,
            False,
            "wrangler.toml [assets] does not set "
            f'not_found_handling = "{_CF_SPA_NOT_FOUND}" — client-side routes would '
            "404 instead of resolving to index.html.",
        )

    # NEGATIVE assertion: a STATIC site serves everything from the asset layer — there
    # is no Worker-first dynamic route. `assets.run_worker_first` is a lead-gen routing
    # leftover (it shadows the asset layer with the Worker) and must FAIL verify.
    if assets.get("run_worker_first") is not None:
        return CheckResult(
            name,
            False,
            "wrangler.toml [assets] sets run_worker_first, but a STATIC directory site "
            "has no Worker-first dynamic route — this is a lead-gen routing leftover. "
            "Remove assets.run_worker_first so the asset layer serves everything.",
        )

    # NEGATIVE assertion: the static schema.sql must be table-free. A `CREATE TABLE`
    # DDL is a lead-gen leftover (the directory primitive persists nothing) and must
    # FAIL verify — the placeholder schema.sql is comment-only.
    schema_sql = files.get("schema.sql") or ""
    if _CF_CREATE_TABLE_RE.search(schema_sql):
        return CheckResult(
            name,
            False,
            "schema.sql contains a CREATE TABLE statement, but a STATIC directory site "
            "persists nothing — this is a lead-gen schema leftover. The static "
            "schema.sql must be table-free (comment-only placeholder).",
        )

    # Secret-safety still holds: a static site has no secret, so a real .dev.vars in
    # the export is always a leak.
    real_secret = files.get(".dev.vars")
    if real_secret is not None and real_secret.strip():
        return CheckResult(
            name,
            False,
            "a real .dev.vars file is present in the export — secrets must never be "
            "generated or committed. Remove .dev.vars.",
        )

    guide = files["OWNER_GUIDE.md"] or ""
    missing_steps = [s for s in _CF_STATIC_GUIDE_REQUIRED_STEPS if s not in guide]
    if missing_steps:
        return CheckResult(
            name,
            False,
            "OWNER_GUIDE.md does not document the required deploy step(s): "
            f"{', '.join(missing_steps)}.",
        )

    return CheckResult(
        name,
        True,
        "Cloudflare export complete (static site): wrangler.toml main → worker/index.ts "
        "binds ASSETS with single-page-application fallback; NO D1 binding, NO "
        "run_worker_first routing, and a table-free schema.sql (no lead-gen leftovers); "
        "no secret contract needed; OWNER_GUIDE.md documents build + deploy. STRUCTURAL "
        "export-readiness — the owner runs the deploy (Disco never deploys).",
    )


__all__ = [
    "CF_EXPORT_FILES",
    "STATIC_CF_EXPORT_FILES",
    "CheckResult",
    "WorkerAuthModel",
    "check_drizzle_schema",
    "check_schema_sql",
    "cloudflare_export_ready",
    "cloudflare_export_ready_static",
    "local_api_roundtrip",
    "main_points_at_worker_entry",
]
