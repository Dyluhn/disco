"""AppKit EPIC G — `verify_appkit_app`, the STRICT app verifier.

Where `verify_web_app` (W-45) answers "does the running web app render without
errors", `verify_appkit_app` answers the stronger AppKit question: "is this a
design-clean lead-gen app whose lead+admin contract is STRUCTURALLY correct —
presence + ordering + parameterization + guard-first". This is STRUCTURAL
verification of the generated source, NOT a proof that the app works at runtime:
runtime reachability / behavioural execution of the Worker is explicitly deferred
to Epic I's local CF (workerd) emulation. It runs NINE checks against the
generated app in the workspace, each returning a PASS/FAIL with concrete evidence:

* ``design_lint_clean``   — `lint_design(workspace_tree, design_spec)` == 0 findings.
* ``schema_sql_valid``    — run the generated `schema.sql` in in-memory sqlite,
                            insert + read back a representative lead row, and prove
                            required columns reject NULL (pure, no deploy).
* ``drizzle_schema_valid`` — parse `src/db/schema.ts` and `schema.sql` and prove the
                            generated Drizzle table has the same column names +
                            notNull flags, and package.json declares drizzle-orm +
                            drizzle-kit.
* ``worker_contract``     — STRUCTURALLY inspect `worker/index.ts` (presence +
                            ordering + parameterization + guard-first; NOT CF-runtime
                            execution — that is Epic I): the public POST /api/leads
                            region CONTAINS a Drizzle insert in a
                            non-dead position; GET /api/leads AND /admin each
                            early-return 401 via the auth guard as the FIRST statement
                            BEFORE any read; isAuthorized Bearer-checks + fails closed
                            when ADMIN_TOKEN unset. A guard whose result is ignored, a
                            200-regardless block, a string-built insert, or an insert
                            after an early return / inside a dead branch FAILS (no
                            false-PASS on a broken/insecure worker).
* ``lead_form_posts``     — control-flow-inspect the form component(s): a REAL (non-
                            comment) POST fetch to `/api/leads`, JSON content-type, the
                            form state serialized into the body, and every lead field
                            actually BOUND to that form state (value + onChange).
* ``local_api_roundtrip`` — a MODEL (driven by the structural flags above, NOT a
                            runtime execution of the worker) exercised against
                            in-memory sqlite: confirms that structure is internally
                            consistent (modelled POST inserts; unauth GET/admin → 401;
                            an authed read returns the row; ADMIN_TOKEN unset fails
                            closed). Runtime behaviour is deferred to Epic I emulation.
* ``cloudflare_export_ready`` — Epic I deploy-export completeness: the export tree
                            carries the owner deliverables (OWNER_GUIDE.md + the
                            .dev.vars.example secret template), wrangler.toml binds DB +
                            ASSETS and routes /api/* + /admin worker-first (so the SPA
                            asset layer can't shadow them), no real .dev.vars secret
                            ships, and OWNER_GUIDE documents the deploy steps. Config +
                            doc completeness, NOT a runtime deploy proof.
* ``route_coverage``      — navigate every AppSpec page route (the W-45 browser path)
                            and require 2xx/3xx with no console/network errors.
* ``section_coverage``    — assert every AppSpec section's `data-appkit-section`
                            marker is present in the RENDERED DOM (same browser path).

The verdict is W-45-COMPATIBLE — top-level {ok/verdict/passed, summary,
next_action, url, failure_fingerprint, console/network} + an embedded
`verify_web_app` verdict for the structural part + a per-check breakdown — so the
finish gate's existing verdict-consumer (`_gate_verify_web_app`) drives it with no
new plumbing.

Layering: tools → core is allowed. The PURE sqlite/spec logic (the local Worker/D1
shim) lives in `disco.core.appkit.local_verify` so core stays a leaf; the
browser/sandbox-dependent route+section coverage stays here.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from disco.core import SecurityRisk
from disco.core.appkit import (
    APPSPEC_RELPATH,
    CF_EXPORT_FILES,
    DIRECTORY_PRIMITIVE_ID,
    STATIC_CF_EXPORT_FILES,
    WorkerAuthModel,
    check_drizzle_schema,
    check_schema_sql,
    cloudflare_export_ready,
    cloudflare_export_ready_static,
    load_app_spec_from_bytes,
    local_api_roundtrip,
    resolve_lead_entity,
    resolve_primitive,
)
from disco.core.appkit.spec import AppSpec, Entity
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from .browser import BrowserArgs, BrowserTool
from .design_lint import DesignLintArgs, DesignLintTool
from .verify_app import VerifyWebAppArgs, VerifyWebAppTool

_SCHEMA_RELPATH = "schema.sql"
_DRIZZLE_SCHEMA_RELPATH = "src/db/schema.ts"
_PACKAGE_RELPATH = "package.json"
_WORKER_RELPATH = "worker/index.ts"
_COMPONENTS_DIR = "src/components"
_VITE_CONFIG_RELPATH = "vite.config.ts"
_VITE_PACKAGE_SHA_RELPATH = ".disco/appkit-vite-package.sha256"
_VITE_BUILD_TIMEOUT_S = 300
_BUILT_PREVIEW_NAME = "appkit-built-vite"
_VITE_PREVIEW_COMMAND = (
    "npx vite preview --host 0.0.0.0 --port {port} --strictPort"
)


@dataclass(frozen=True)
class WorkerAuthVerdict(WorkerAuthModel):
    """The inspected worker-auth model (:class:`WorkerAuthModel` from core) PLUS the
    SEC-4 non-bypassable booleans the deploy gate requires. It IS-A ``WorkerAuthModel``
    (so ``local_api_roundtrip`` and every existing consumer keep working) and only
    ADDS:

    * ``admin_token_safe`` — ``env.ADMIN_TOKEN`` is read ONLY inside the canonical
      ``isAuthorized`` comparison and never flows (directly OR via an alias /
      destructuring) into a Response / return / serialization / log SINK anywhere
      else. Closes SEC-4-A: the deploy gate's direct-``env.ADMIN_TOKEN``-in-a-Response
      regex is trivially aliased around (``const leaked = env.ADMIN_TOKEN; return new
      Response(leaked)``).
    * ``all_lead_reads_guarded`` — EVERY route handler that returns lead data (not just
      the two canonical read routes) is dominated by the ``if (!isAuthorized(...))``
      early-return guard. Closes SEC-4-B: an extra unauthenticated ``/debug-leads``
      route returning leads previously slipped through.

    The deploy gate should require ``inspect_worker(...)[1].admin_token_safe`` AND
    ``inspect_worker(...)[1].all_lead_reads_guarded`` (both default ``True`` only on a
    fully-canonical, non-leaking worker)."""

    admin_token_safe: bool = True
    all_lead_reads_guarded: bool = True


# ---- pure static-inspection helpers (unit-testable without a sandbox) ----------
#
# These verify the generated worker/form by CONTROL FLOW, not substring presence.
# A worker that merely *mentions* `isAuthorized` but ignores its result, returns 200
# to an unauthenticated admin, or builds its INSERT by string concatenation MUST
# FAIL: a false-PASS verifier is the worst outcome — it gives false confidence in a
# broken/insecure generated app.
#
# SCOPE (honest): this is STRUCTURAL verification of the TypeScript *source*
# (presence + ordering + parameterization + guard-first) — NOT CF-runtime execution
# (running the Worker under workerd is Epic I's local-emulation job). A PASS here
# means "the handler's STRUCTURE enforces the lead/admin contract" — it does NOT
# prove the insert is reached or the guard runs at runtime. The flags it extracts
# then drive `local_api_roundtrip`, which runs a MODEL of that behaviour against a
# real in-memory sqlite DB — a structural-consistency check, NOT a runtime execution
# of the worker itself. Runtime reachability/behaviour is deferred to Epic I.


def _read_string_literal(src: str, start: int) -> tuple[str | None, int]:
    """If `src[start]` opens a `'`/`"`/`` ` `` literal, return (inner_raw, index just
    past the closing quote); else (None, start). Escapes are kept raw — callers only
    scan the inner text for SQL keywords, never evaluate it."""
    if start >= len(src) or src[start] not in ("'", '"', "`"):
        return None, start
    quote = src[start]
    i = start + 1
    out: list[str] = []
    while i < len(src):
        ch = src[i]
        if ch == "\\" and i + 1 < len(src):
            out.append(src[i : i + 2])
            i += 2
            continue
        if ch == quote:
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return None, start  # unterminated → treat as not-a-literal


def _strip_ts_comments(src: str) -> str:
    """Blank out `//` and `/* */` comments (length-preserving) WITHOUT touching
    string/template literals, so a `fetch(...)`/auth check hidden inside a comment is
    no longer read as live code. The worker's only regex literal (`EMAIL_RE`) carries
    no `//`/`/*`, so a plain literal-skip is exact here."""
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in ("'", '"', "`"):
            lit, end = _read_string_literal(src, i)
            if lit is not None:
                out.append(src[i:end])
                i = end
                continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            out.append("  ")
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _match_brace(src: str, open_idx: int) -> int | None:
    """Index just past the `}` matching the `{` at `open_idx`, skipping braces inside
    string/template literals. None if unbalanced. (The route-handler blocks and the
    `isAuthorized`/`adminLoginPage` bodies we match through contain no nested template
    literals, so a simple literal-skip is exact.)"""
    depth = 0
    i = open_idx
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in ("'", '"', "`"):
            lit, end = _read_string_literal(src, i)
            if lit is not None:
                i = end
                continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _match_paren(src: str, open_idx: int) -> int | None:
    """Index just past the `)` matching the `(` at `open_idx`, skipping parens inside
    string/template literals. None if unbalanced. (Used to find the end of a
    `.bind(...)` argument list so the chained `.run()` after it can be confirmed.)"""
    depth = 0
    i = open_idx
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in ("'", '"', "`"):
            _lit, end = _read_string_literal(src, i)
            if end > i:
                i = end
                continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _braced_block_after(src: str, header_re: str) -> str | None:
    """The balanced `{...}` block (braces included) whose opening `{` is matched at
    the END of `header_re`. None if the header is absent or the braces don't balance."""
    m = re.search(header_re, src)
    if m is None:
        return None
    brace = src.find("{", m.end() - 1)
    if brace == -1:
        return None
    end = _match_brace(src, brace)
    return src[brace:end] if end is not None else None


def _route_handler(src: str, guard_re: str) -> str | None:
    """The `{...}` body of the `if (<guard>) { ... }` route handler, or None."""
    return _braced_block_after(src, r"if\s*\(\s*" + guard_re + r"\s*\)\s*\{")


def _isauthorized_body(src: str) -> str | None:
    return _braced_block_after(src, r"function\s+isAuthorized\s*\([^)]*\)[^{]*\{")


def _admin_login_returns_401(src: str) -> bool:
    """The `adminLoginPage()` shell must build its Response with `status: 401` — so a
    `401`→`200` mutation (admin served unauthenticated) is caught."""
    body = _braced_block_after(src, r"function\s+adminLoginPage\s*\([^)]*\)[^{]*\{")
    return body is not None and bool(re.search(r"status:\s*401\b", body))


def _returns_401(guard_body: str) -> bool:
    """The auth-guard fail branch returns an HTTP 401 (`json({...}, 401)`)."""
    return bool(re.search(r",\s*401\s*\)", guard_body))


def _read_block_gated(block: str, denial_ok: Callable[[str], bool]) -> bool:
    """True iff the read handler `block` ENFORCES auth by control flow: it gates on
    the REAL auth condition `if (!isAuthorized(request, env)) { ... }`, that branch
    UNCONDITIONALLY EARLY-RETURNS a denial (`denial_ok`), the DB read (`listLeads`)
    happens only AFTER the guard, and nothing returns before it.

    The guard's condition is pinned to the actual auth expression
    `!isAuthorized(request, env)` — a constant/dead condition (`if (false)` /
    `if (true)`) or any non-auth predicate does not match and FAILS. Crucially the
    denial must be the FIRST statement of the guard body (the generated form
    `{ return <denial>; }`): a denial buried behind an inner dead condition
    (`{ if (false) return json(..., 401); }`) is NOT reachable for an unauthenticated
    caller — control falls through the guard to the read — so it FAILS. An ignored
    auth result, a 200-regardless block, or a dead denial are the false-PASS holes
    this closes."""
    guard = re.search(
        r"if\s*\(\s*!\s*isAuthorized\s*\(\s*request\s*,\s*env\s*\)\s*\)\s*\{", block
    )
    if guard is None:
        return False
    gbrace = block.find("{", guard.end() - 1)
    gend = _match_brace(block, gbrace)
    if gend is None:
        return False
    guard_body = block[gbrace:gend]
    # The denial must be the guard body's FIRST statement (unconditional early return),
    # so an unauthenticated caller is denied — not a `return` reachable only behind a
    # dead/constant inner condition while control otherwise falls through to the read.
    if re.match(r"\s*\{\s*return\b", guard_body) is None:
        return False
    if not denial_ok(guard_body):
        return False  # guard returns, but not an HTTP denial (401)
    read = re.search(r"\blistLeads\s*\(", block)
    if read is not None and read.start() < guard.start():
        return False  # leads read BEFORE the guard runs
    if re.search(r"\breturn\b", block[: guard.start()]):
        return False  # a value is served before the guard runs
    return True


def _dead_branch_spans(region: str) -> list[tuple[int, int]]:
    """`[start, end)` spans of code controlled by an obviously-dead constant guard
    (`if (false)` / `if (0)`): either the braced block or the single statement after
    the guard. An insert that lands inside such a span can never run.

    HONEST SCOPE: this catches the literal constant-false branch only — it is a
    heuristic for the demonstrated dead case, NOT a general dead-code/SAT analysis
    (static regex cannot soundly decide reachability)."""
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r"if\s*\(\s*(?:false|0)\s*\)\s*", region):
        j = m.end()
        if j < len(region) and region[j] == "{":
            end = _match_brace(region, j)
            if end is not None:
                spans.append((j, end))
        else:
            semi = region.find(";", j)
            spans.append((j, semi + 1 if semi != -1 else len(region)))
    return spans


def _enclosing_block_start(region: str, pos: int) -> int:
    """Index of the `{` opening the innermost brace block that encloses `pos` (the
    last unclosed `{` at/just before `pos`), skipping braces inside string/template
    literals. -1 if `pos` is not inside any block."""
    stack: list[int] = []
    i = 0
    n = len(region)
    while i < n and i < pos:
        ch = region[i]
        if ch in ("'", '"', "`"):
            _lit, end = _read_string_literal(region, i)
            if end > i:
                i = end
                continue
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            stack.pop()
        i += 1
    return stack[-1] if stack else -1


def _unconditional_exit_precedes(region: str, pos: int) -> bool:
    """True if, within the innermost brace block enclosing `pos`, an UNCONDITIONAL
    `return`/`throw` statement occurs at that block's own statement level BEFORE
    `pos` — so control exits the block and never reaches `pos`. A `return`/`throw`
    that is part of an `if (...) return` guard (preceded by `)`) is conditional and
    does NOT count; only a statement-start exit (preceded by `{`/`;`/`}`) does.

    HONEST SCOPE: a heuristic for the demonstrated `return ...; <insert>` dead case,
    not a complete control-flow analysis (it does not reason about `else`/loops/
    `switch` fall-through)."""
    block_open = _enclosing_block_start(region, pos)
    i = 0 if block_open < 0 else block_open + 1
    depth = 0
    prev_sig = "{"  # the block open precedes the first statement
    n = len(region)
    while i < pos and i < n:
        ch = region[i]
        if ch in ("'", '"', "`"):
            _lit, end = _read_string_literal(region, i)
            if end > i:
                i = end
                prev_sig = '"'
                continue
        if ch == "{":
            depth += 1
            prev_sig = ch
            i += 1
            continue
        if ch == "}":
            depth -= 1
            prev_sig = ch
            i += 1
            continue
        if depth == 0 and prev_sig in "{;}" and re.match(r"(?:return|throw)\b", region[i:]):
            return True  # unconditional early exit at this block's level before `pos`
        if not ch.isspace():
            prev_sig = ch
        i += 1
    return False


def _insert_in_dead_position(region: str, insert_start: int) -> bool:
    """True if the insert at `insert_start` can never run because it is
    inside a constant-false branch OR after an unconditional early return/throw in its
    enclosing block. These close the concrete dead-code evasions; they do NOT claim
    general reachability soundness (impossible for static regex — see Epic I)."""
    for start, end in _dead_branch_spans(region):
        if start <= insert_start < end:
            return True
    return _unconditional_exit_precedes(region, insert_start)


def _region_has_run_insert(region: str) -> tuple[bool, bool]:
    """Scan `region` for a Drizzle lead insert that is RUN and in a
    non-dead position — `db.insert(leads).values(...).run()` — and report
    `(region_has_run_insert, is_parameterized)`:

    * ``region_has_run_insert`` — a Drizzle insert whose `.values(...)` is chained into
      a `.run(` call is PRESENT in this region in a reachable (non-dead) position.
      A bare `.insert(...).values(...)` with no `.run()` is a DEAD reference (never
      executed) and does NOT count; nor does an insert after an unconditional early
      return or inside an `if (false)`/`if (0)` branch.
    * ``is_parameterized`` — true only for the generated Drizzle table insert. A raw
      SQL `INSERT INTO` in the POST data plane is reported as present-but-not-ok so
      worker_contract fails with a specific reason.

    HONEST SCOPE: this proves STRUCTURAL PRESENCE + non-dead position of the insert in
    the POST handler region (see `_post_region`), NOT that runtime control actually
    REACHES it. An insert that exists only in an UNcalled function is correctly NOT
    found here; full reachability is deferred to Epic I's local CF emulation."""
    for m in re.finditer(r"\.insert\s*\(\s*leads\s*\)", region):
        values = re.match(r"\s*\.values\s*\(", region[m.end():])
        if values is None:
            continue
        values_open = m.end() + values.end() - 1
        values_end = _match_paren(region, values_open)
        if values_end is None:
            continue
        if re.match(r"\s*\.run\s*\(", region[values_end:]) is None:
            continue
        if _insert_in_dead_position(region, m.start()):
            continue
        return True, True

    # Legacy/raw SQL data planes are not the ratified generated shape. If one is
    # present in-region, surface it as an insert that is not acceptable instead of
    # conflating it with a completely missing insert.
    for m in re.finditer(r"\.prepare\s*\(", region):
        j = m.end()
        while j < len(region) and region[j] in " \t\r\n":
            j += 1
        lit, end = _read_string_literal(region, j)
        if lit is None or "INSERT INTO" not in lit.upper():
            continue
        k = end
        while k < len(region) and region[k] in " \t\r\n":
            k += 1
        if k >= len(region) or region[k] != ")":
            continue  # `.prepare("..." + x)` etc. — literal not closed by `)`
        bind = re.match(r"\s*\.bind\s*\(", region[k + 1 :])
        if bind is None:
            continue  # no `.bind(...)` follows the prepared INSERT
        bind_open = k + 1 + bind.end() - 1  # index of the `(` opening `.bind(`
        bind_end = _match_paren(region, bind_open)
        if bind_end is None:
            continue
        if re.match(r"\s*\.run\s*\(", region[bind_end:]) is None:
            continue  # prepared+bound but never `.run()` — a dead reference
        if _insert_in_dead_position(region, m.start()):
            continue  # present but UNREACHABLE (dead branch / after early return)
        return True, False
    return False, False


def _post_region(src: str, post_block: str) -> str:
    """The POST handler's in-region code: `post_block` plus the body of every
    top-level `function NAME(...) {...}` that the POST block CALLS (one level deep).
    The generated POST handler delegates the insert to a helper it returns
    (`return insertLead(env, body)`), so the insert lives in that helper — but only
    because the handler actually CALLS it. An insert in a function the POST block
    never calls is NOT in this region, so it cannot satisfy the contract."""
    region = post_block
    seen: set[str] = set()
    for name in re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", post_block):
        if name in seen:
            continue
        seen.add(name)
        body = _braced_block_after(
            src, r"\bfunction\s+" + re.escape(name) + r"\s*\([^)]*\)[^{]*\{"
        )
        if body is not None:
            region += "\n" + body
    return region


# ---- SEC-4: alias-aware token-leak + all-lead-reads-guarded analysis -----------
#
# Two residual BYPASSES of the worker-auth verdict, closed here with the strongest
# feasible STATIC analysis on the generated worker source. The approach is a
# CANONICAL-SHAPE + light-taint HYBRID: because WE generate the worker, the known-safe
# shape is fixed, so ANY deviation from it (the token read anywhere but the canonical
# auth check; a lead-read route that is not guard-first) is itself the failure signal —
# that is far more robust than trying to enumerate every possible leak expression.
#
# SEC-4-A `admin_token_safe` — the admin token must be read ONLY inside the canonical
#   `isAuthorized` comparison. The deploy gate's DIRECT `env.ADMIN_TOKEN`-in-a-Response
#   regex is defeated by `const leaked = env.ADMIN_TOKEN; return new Response(leaked)`.
#   We require ALL of: (1) every `env.ADMIN_TOKEN` read sits inside the isAuthorized
#   body; (2) no `{ ADMIN_TOKEN } = env` destructuring alias anywhere; (3) neither the
#   token nor any `const x = env.ADMIN_TOKEN` alias flows into a leak sink (Response /
#   return / console / JSON.stringify / json()).
#
# SEC-4-B `all_lead_reads_guarded` — EVERY route handler that performs a lead-read sink
#   (calls `listLeads` / runs a `SELECT ... FROM ... leads`) must be dominated by the
#   `if (!isAuthorized(request, env))` early-return guard. The verdict previously only
#   inspected the two canonical read routes, so an extra unauthenticated `/debug-leads`
#   returning leads slipped through. We ENUMERATE all `url.pathname ===` handlers and
#   fail if any lead-returning one is not guard-first.
#
# HONEST SCOPE: static (no runtime). The leak-sink + reachability matchers cover the
# demonstrated exfiltration/bypass channels — they are NOT a complete information-flow
# proof (full runtime behaviour is Epic I's local CF emulation).


# Sinks into which a token VALUE must never flow ({E} = the token expr/alias fragment).
# A mere comparison (`=== expected` / `!expected`) is NOT a sink — only a value that is
# returned raw or piped into a response/serialization/log escapes.
_TOKEN_SINKS = (
    r"new\s+Response\s*\([^;]*?{E}",
    r"console\s*\.\s*\w+\s*\([^;]*?{E}",
    r"\bjson\s*\([^;]*?{E}",
    r"JSON\s*\.\s*stringify\s*\([^;]*?{E}",
    r"\breturn\s+{E}\s*[;,)]",
)


def _token_flows_to_sink(src: str, expr: str) -> bool:
    """True if the token expression/alias `expr` (a regex fragment) is RETURNED raw or
    passed into a Response / console / json() / JSON.stringify sink — i.e. the value
    escapes. The `[^;]` body keeps each match inside a single statement."""
    return any(re.search(p.replace("{E}", expr), src) for p in _TOKEN_SINKS)


def _isauthorized_span(src: str) -> tuple[int, int] | None:
    """The [start, end) span of the `isAuthorized` body `{...}` (braces included)."""
    m = re.search(r"function\s+isAuthorized\s*\([^)]*\)[^{]*\{", src)
    if m is None:
        return None
    brace = src.find("{", m.end() - 1)
    if brace == -1:
        return None
    end = _match_brace(src, brace)
    return (brace, end) if end is not None else None


def _admin_token_safe(src: str) -> bool:
    """SEC-4-A: the admin token is read ONLY inside the canonical `isAuthorized` check
    and never flows (directly, via an alias, or via destructuring) into a response /
    serialization / log sink. Any `env.ADMIN_TOKEN` read OUTSIDE isAuthorized, any
    `{ ADMIN_TOKEN } = env` destructuring alias, or any token/alias sink → False."""
    span = _isauthorized_span(src)
    if span is None:
        return False  # no canonical auth check to confine the token read to
    a0, a1 = span
    reads = list(re.finditer(r"env\s*\.\s*ADMIN_TOKEN\b", src))
    if not reads:
        return False  # the canonical worker reads the token inside isAuthorized
    for m in reads:
        if not (a0 <= m.start() < a1):
            return False  # token read OUTSIDE the canonical auth check (alias channel)
    if re.search(r"\{[^{}]*\bADMIN_TOKEN\b[^{}]*\}\s*=\s*env\b", src):
        return False  # `const { ADMIN_TOKEN } = env` destructuring alias of the token
    if _token_flows_to_sink(src, r"env\s*\.\s*ADMIN_TOKEN\b"):
        return False  # the token read piped straight into a sink
    for name in set(
        re.findall(
            r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*env\s*\.\s*ADMIN_TOKEN\b", src
        )
    ):
        if _token_flows_to_sink(src, r"\b" + re.escape(name) + r"\b"):
            return False  # an alias of the token escapes into a sink
    return True


def _pathname_handler_blocks(src: str) -> list[str]:
    """The `{...}` body of every `if (url.pathname === "...") {...}` route handler."""
    blocks: list[str] = []
    for m in re.finditer(r'if\s*\(\s*url\.pathname\s*===\s*"[^"]*"', src):
        brace = src.find("{", m.end())
        if brace == -1:
            continue
        end = _match_brace(src, brace)
        if end is not None:
            blocks.append(src[brace:end])
    return blocks


def _block_reads_leads(src: str, block: str) -> bool:
    """True if the route `block` performs a lead-read sink: a `listLeads(...)` call (in
    the block or a helper it calls), an inline Drizzle select from `leads`, or an
    inline `SELECT ... FROM ... leads`."""
    if re.search(r"\blistLeads\s*\(", block):
        return True
    region = _post_region(src, block)
    if re.search(r"\blistLeads\s*\(", region):
        return True
    if re.search(r"\.select\s*\(\s*\)\s*\.from\s*\(\s*leads\s*\)", region):
        return True
    return re.search(r"\bSELECT\b[^;]*?\bFROM\b[^;]*?leads", region, re.IGNORECASE) is not None


def _lead_read_guarded(src: str, block: str) -> bool:
    """True if the lead-reading `block` is dominated by the auth guard (guard-first,
    early-returns a 401 / adminLoginPage denial before the read)."""

    def _denial_ok(guard_body: str) -> bool:
        if "adminLoginPage" in guard_body:
            return _admin_login_returns_401(src)
        return _returns_401(guard_body)

    return _read_block_gated(block, _denial_ok)


def _all_lead_reads_guarded(src: str) -> bool:
    """SEC-4-B: EVERY route handler that returns lead data must be auth-guarded — not
    just the two canonical read routes. An extra unauthenticated route that reads leads
    (e.g. `/debug-leads`) → False."""
    for block in _pathname_handler_blocks(src):
        if _block_reads_leads(src, block) and not _lead_read_guarded(src, block):
            return False
    return True


def inspect_worker(worker_ts: str, lead: Entity) -> tuple[bool, WorkerAuthVerdict, list[str]]:
    """STRUCTURALLY inspect the generated worker. Returns (post_contract_ok,
    auth_model, reasons) where `reasons` names every contract gap found. We parse the
    ACTUAL route-handler blocks and verify STRUCTURE (presence + ordering +
    parameterization + guard-first), not substring presence — but NOT runtime
    behaviour, which is deferred to Epic I's local CF emulation:

    * POST /api/leads is PUBLIC (no auth guard) and its in-region code (the handler
      block + any helper it calls) CONTAINS a Drizzle table insert
      (`db.insert(leads).values(...).run()`) in a non-dead position — never
      string-built SQL, never an insert that only exists in an unreached function, and never one
      after an early return / inside a dead branch (structural presence, not a proof
      runtime control reaches it);
    * GET /api/leads AND /admin each call the auth guard and EARLY-RETURN 401 as the
      FIRST statement BEFORE any DB read (an ignored auth result / a 200-regardless
      block FAILS);
    * `isAuthorized` validates the Bearer token against `env.ADMIN_TOKEN` and
      FAILS CLOSED (`return false`) when ADMIN_TOKEN is unset;
    * (SEC-4-A) `env.ADMIN_TOKEN` is read ONLY inside `isAuthorized` and never leaks
      (directly, via an alias, or via destructuring) into a response/serialization/log
      sink → `admin_token_safe`;
    * (SEC-4-B) EVERY route handler that returns lead data — not just the two canonical
      read routes — is dominated by the auth guard → `all_lead_reads_guarded`.

    The extracted `WorkerAuthVerdict` reflects the inspected STRUCTURE, so the
    deploy-free `local_api_roundtrip` models that structure (not CF-runtime
    execution; runtime reachability/behaviour is Epic I).
    """
    reasons: list[str] = []
    src = _strip_ts_comments(worker_ts)

    post_block = _route_handler(
        src, r'url\.pathname\s*===\s*"/api/leads"\s*&&\s*request\.method\s*===\s*"POST"'
    )
    has_post_route = post_block is not None
    post_public = has_post_route and re.search(r"isAuthorized\s*\(", post_block or "") is None
    # Region-scope the insert proof to the POST handler's in-region code (the block +
    # any helper it calls), so an insert that merely exists ELSEWHERE in the file — or
    # sits in a dead/unreachable position — does not satisfy the POST contract. This is
    # STRUCTURAL presence + non-dead position, NOT a runtime-reachability proof.
    post_region = _post_region(src, post_block) if post_block is not None else ""
    post_region_has_insert, insert_parameterized = _region_has_run_insert(post_region)
    if not has_post_route:
        reasons.append("no public POST /api/leads route")
    elif not post_public:
        reasons.append("POST /api/leads must be public — it is gated behind an auth check")
    if not post_region_has_insert:
        reasons.append(
            "the POST /api/leads handler region does not contain a Drizzle insert in a "
            "reachable (non-dead) position (db.insert(leads).values(...).run() in the "
            "POST handler or a helper it calls, not after an early return / inside a dead branch)"
        )
    elif not insert_parameterized:
        reasons.append(
            "the lead insert is not the generated Drizzle data plane "
            "(db.insert(leads).values(...).run(); raw SQL INSERT strings are not allowed)"
        )

    get_block = _route_handler(
        src, r'url\.pathname\s*===\s*"/api/leads"\s*&&\s*request\.method\s*===\s*"GET"'
    )
    admin_block = _route_handler(
        src, r'url\.pathname\s*===\s*"/admin"\s*&&\s*request\.method\s*===\s*"GET"'
    )

    auth_body = _isauthorized_body(src)
    bearer_checked = auth_body is not None and bool(
        re.search(r'"Bearer "', auth_body)
        and re.search(r"\.startsWith\s*\(", auth_body)
        and re.search(r"===\s*expected\b", auth_body)
    )
    # Fail-closed: the auth check denies when ADMIN_TOKEN (env → `expected`) is unset.
    fail_closed = auth_body is not None and bool(
        re.search(r"=\s*env\.ADMIN_TOKEN", auth_body)
        and re.search(r"if\s*\(\s*!\s*expected\s*\)\s*return\s+false", auth_body)
    )

    def _admin_denial_ok(guard_body: str) -> bool:
        if "adminLoginPage" in guard_body:
            return _admin_login_returns_401(src)
        return _returns_401(guard_body)

    leads_get_guarded = get_block is not None and _read_block_gated(get_block, _returns_401)
    admin_guarded = admin_block is not None and _read_block_gated(admin_block, _admin_denial_ok)
    # Reads are TRULY gated only if both blocks early-return a denial AND isAuthorized
    # actually validates the bearer token (an always-true guard is not a gate).
    reads_require_auth = leads_get_guarded and admin_guarded and bearer_checked

    if not leads_get_guarded:
        reasons.append(
            "GET /api/leads does not early-return 401 via the auth guard before reading leads"
        )
    if not admin_guarded:
        reasons.append(
            "/admin does not early-return 401 via the auth guard before reading leads"
        )
    if not bearer_checked:
        reasons.append(
            "isAuthorized does not validate the Authorization: Bearer token against ADMIN_TOKEN"
        )
    if not fail_closed:
        reasons.append(
            "the auth check does not fail closed (return false) when ADMIN_TOKEN is unset"
        )

    # SEC-4-A / SEC-4-B: make the worker-auth verdict non-bypassable.
    admin_token_safe = _admin_token_safe(src)
    all_lead_reads_guarded = _all_lead_reads_guarded(src)
    if not admin_token_safe:
        reasons.append(
            "env.ADMIN_TOKEN is read or leaked outside the isAuthorized check (a direct read, "
            "an alias `const x = env.ADMIN_TOKEN`, or `{ ADMIN_TOKEN } = env` destructuring "
            "flows into a response/serialization/log sink) — admin-token exfiltration risk"
        )
    if not all_lead_reads_guarded:
        reasons.append(
            "a route handler returns lead data without the auth guard "
            "(an unauthenticated lead-read route bypasses the admin gate)"
        )

    post_contract_ok = (
        has_post_route and post_public and post_region_has_insert and insert_parameterized
    )
    model = WorkerAuthVerdict(
        reads_require_auth=reads_require_auth,
        fail_closed_without_token=fail_closed,
        insert_parameterized=insert_parameterized,
        post_region_has_insert=post_region_has_insert,
        admin_token_safe=admin_token_safe,
        all_lead_reads_guarded=all_lead_reads_guarded,
    )
    return post_contract_ok, model, reasons


def _fetch_leads_in_call_position(code: str) -> bool:
    """True iff `fetch("/api/leads"...)` appears as an actual CALL in executable code:
    the `fetch` identifier sits in code context (NOT inside a string/template literal),
    is a bare/`await`ed call (not a `.member` access), is immediately applied to `(`,
    and its first argument is the string `/api/leads`. A `fetch("/api/leads")` buried
    inside a string or template literal — inert text that never runs — does NOT count
    (that is the false-PASS hole this closes)."""
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if ch in ("'", '"', "`"):
            _lit, end = _read_string_literal(code, i)
            if end > i:
                i = end  # skip the whole string/template literal — its text is not code
                continue
        if code.startswith("fetch", i) and (
            i == 0 or not (code[i - 1].isalnum() or code[i - 1] in "_$.")
        ):
            j = i + len("fetch")
            while j < n and code[j] in " \t\r\n":
                j += 1
            if j < n and code[j] == "(":
                j += 1
                while j < n and code[j] in " \t\r\n":
                    j += 1
                inner, _end = _read_string_literal(code, j)
                if inner is not None and inner.split("?", 1)[0] == "/api/leads":
                    return True
        i += 1
    return False


def inspect_lead_form(form_src: str, lead: Entity) -> tuple[bool, list[str]]:
    """Control-flow-inspect a generated form component for the lead-POST contract:
    a REAL (non-comment, non-string) `fetch("/api/leads")` CALL POSTing JSON, the whole
    form state serialized into the body (`JSON.stringify(form)`), and EVERY resolved
    lead field bound to that form state (a named input wired by `value={form[...]}` + an
    `onChange` that writes `[field]:`). A commented-out/dummy fetch, a `fetch("/api/leads")`
    that is only inert STRING content, an unbound field, or a field dropped from the
    POST body FAILS. Returns (ok, reasons)."""
    reasons: list[str] = []
    code = _strip_ts_comments(form_src)
    if not _fetch_leads_in_call_position(code):
        reasons.append(
            'the form does not fetch("/api/leads") in call position in live code '
            "(only in a comment or an inert string?)"
        )
    if not re.search(r'method:\s*"POST"', code):
        reasons.append("the form submit is not a POST")
    if not re.search(r'"Content-Type":\s*"application/json"', code):
        reasons.append("the form does not send a JSON Content-Type")
    if not re.search(r"body:\s*JSON\.stringify\(\s*form\s*\)", code):
        reasons.append(
            "the POST body does not serialize the form state (body: JSON.stringify(form)) — "
            "fields may be dropped from the request"
        )
    for field in lead.fields:
        name = re.escape(field.name)
        has_input = re.search(r'name="' + name + r'"', code) is not None
        value_bound = re.search(r'value=\{\s*form\[\s*"' + name + r'"\s*\]', code) is not None
        change_bound = re.search(r'\[\s*"' + name + r'"\s*\]\s*:', code) is not None
        if not has_input:
            reasons.append(f'no input named "{field.name}" for the lead field')
        elif not (value_bound and change_bound):
            reasons.append(
                f'the "{field.name}" input is not bound to form state (value/onChange)'
            )
    return (not reasons), reasons


# ---- EPIC N: directory-primitive static inspectors -----------------------------
#
# A directory site is STATIC: the Worker must be a pure asset passthrough (no lead
# API / D1 insert / admin read-back), and the distinguishing section is a SEARCHABLE
# listing (a controlled input that filters the entries client-side). These verify
# the generated SOURCE structurally, mirroring inspect_worker / inspect_lead_form.


def inspect_static_worker(worker_ts: str) -> tuple[bool, list[str]]:
    """STRUCTURALLY verify the directory primitive's STATIC Worker: it serves built
    assets (`env.ASSETS.fetch`) and has NO server-side data plane — no `/api/leads`
    route, no D1 prepared insert, no admin read-back. A static worker that grew a
    lead API / DB write is no longer a directory site and FAILS. Returns (ok, reasons)."""
    reasons: list[str] = []
    src = _strip_ts_comments(worker_ts)
    if re.search(r"env\.ASSETS\.fetch\s*\(", src) is None:
        reasons.append("the static worker does not serve built assets (env.ASSETS.fetch)")
    if "/api/leads" in src:
        reasons.append("a static directory worker must not expose a /api/leads route")
    if re.search(r"\.prepare\s*\(", src) is not None:
        reasons.append("a static directory worker must not run a D1 prepared statement")
    if re.search(r"\bD1Database\b", src) is not None:
        reasons.append("a static directory worker must not bind a D1 database")
    return (not reasons), reasons


def inspect_directory_listing(src: str) -> tuple[bool, list[str]]:
    """STRUCTURALLY verify a directory LISTING component: a controlled search input
    (`useState` + `<input value={…} onChange={…}>`) that FILTERS the entries
    (`.filter(`) which are then RENDERED (`.map(`). A listing with no search/filter
    UI, or one that never renders the entries, FAILS. Returns (ok, reasons)."""
    reasons: list[str] = []
    code = _strip_ts_comments(src)
    if "useState" not in code:
        reasons.append("the listing has no search state (useState)")
    if re.search(r"<input\b", code) is None:
        reasons.append("the listing has no search input element")
    elif re.search(r"value=\{", code) is None or re.search(r"onChange\s*=\s*\{", code) is None:
        reasons.append("the search input is not a controlled field (value + onChange)")
    if ".filter(" not in code:
        reasons.append("the listing does not filter entries by the search query")
    if ".map(" not in code:
        reasons.append("the listing does not render the entries (.map)")
    return (not reasons), reasons


# ---- verdict assembly ----------------------------------------------------------


def _check(name: str, passed: bool, evidence: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "evidence": evidence}


@dataclass(frozen=True)
class _PreviewProbe:
    status: int
    content_type: str
    body: str
    error: str = ""


@dataclass(frozen=True)
class _BuildResult:
    ok: bool
    evidence: str = ""


@dataclass(frozen=True)
class _PreparedPreview:
    url: str
    stop_name: str | None = None
    failure_evidence: str | None = None


def _is_vite_app_tree(package_json: str | None, has_vite_config: bool) -> bool:
    """True for the generated React/Vite app shape the browser verifier must build.

    The trigger is intentionally narrow: package.json must be valid JSON with a
    ``devDependencies.vite`` entry and the workspace must carry ``vite.config.ts``.
    """
    if not package_json or not has_vite_config:
        return False
    try:
        pkg = json.loads(package_json)
    except json.JSONDecodeError:
        return False
    if not isinstance(pkg, dict):
        return False
    dev_deps = pkg.get("devDependencies")
    return isinstance(dev_deps, dict) and "vite" in dev_deps


_UNBUILT_VITE_ENTRY_RE = re.compile(
    r"""<script\b(?=[^>]*\btype=["']module["'])(?=[^>]*\bsrc=["']/src/[^"']+)""",
    re.IGNORECASE,
)
_BUILT_VITE_BUNDLE_RE = re.compile(
    r"""<script\b(?=[^>]*\btype=["']module["'])(?=[^>]*\bsrc=["'][^"']*/assets/[^"']+\.js["'])""",
    re.IGNORECASE,
)


def _served_preview_uses_built_bundle(index_html: str) -> bool | None:
    """Classify a served Vite index page.

    ``False`` means the preview is definitely the source tree (for example,
    ``/src/main.tsx``). ``True`` means it is definitely built Vite output
    (hashed ``/assets/*.js`` bundle). ``None`` means the body is not recognizable
    enough to force a platform rebuild.
    """
    if _UNBUILT_VITE_ENTRY_RE.search(index_html) or "/src/main.tsx" in index_html:
        return False
    if _BUILT_VITE_BUNDLE_RE.search(index_html):
        return True
    return None


def _tail(text: str, *, limit: int = 2000) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return "..." + stripped[-limit:]


def _exec_failure_evidence(step: str, res: Any) -> str:
    reason = "timed out" if bool(getattr(res, "timed_out", False)) else (
        f"exit {getattr(res, 'exit_code', 'unknown')}"
    )
    stderr = _tail(str(getattr(res, "stderr", "") or ""))
    stdout = _tail(str(getattr(res, "stdout", "") or ""))
    tail = stderr or stdout or "(no stderr/stdout captured)"
    return f"platform Vite build failed during `{step}` ({reason}); stderr tail: {tail}"


def _composite_fingerprint(checks: list[dict[str, Any]], embedded_fp: str) -> str:
    """Stable hash over the FAILING check names + the embedded verify_web_app
    fingerprint. Identical failures → identical fingerprint (the finish-gate
    loop-breaker key); all-clean → a fixed 'clean' hash."""
    failing = sorted(c["name"] for c in checks if not c["passed"])
    raw = "|".join(failing) + "#" + (embedded_fp or "")
    if not failing:
        raw = "CLEAN#" + (embedded_fp or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_verdict(
    checks: list[dict[str, Any]], embedded: dict[str, Any] | None
) -> dict[str, Any]:
    """Assemble the W-45-compatible verdict from the per-check results + the
    embedded `verify_web_app` (structural) verdict."""
    emb = embedded or {}
    first_fail = next((c for c in checks if not c["passed"]), None)
    passed = first_fail is None
    n = len(checks)
    if first_fail is None:
        summary = (
            f"verify_appkit_app: all {n} STRUCTURAL checks passed — design-clean; "
            "schema+worker+form structure intact (presence/ordering/parameterization/"
            "guard-first); local lead/admin model round-trip consistent; routes + "
            "sections covered. STRUCTURE verified — runtime behaviour deferred to Epic I "
            "local CF emulation."
        )
        next_action = ""
    else:
        summary = (
            f"verify_appkit_app: {first_fail['name']} FAILED — {first_fail['evidence']}"
        )
        next_action = str(first_fail["evidence"])
    fp = _composite_fingerprint(checks, str(emb.get("failure_fingerprint") or ""))
    return {
        "ok": passed,
        "passed": passed,
        "verdict": "pass" if passed else "fail",
        "summary": summary,
        "next_action": next_action,
        "failure_fingerprint": fp,
        # W-45 surface (read by the finish gate's preview-binding + payload builder).
        "url": str(emb.get("url") or ""),
        "http_status": emb.get("http_status"),
        "console_errors": emb.get("console_errors") or [],
        "network_failures": emb.get("network_failures") or [],
        "screenshot_path": str(emb.get("screenshot_path") or ""),
        "checks": checks,
        "verify_web_app": emb,
    }


def _render(verdict: dict[str, Any]) -> str:
    """Stable, agent-facing text. Excludes screenshot path / sequence so an
    identical failure renders byte-identically (the no-progress breaker matches)."""
    state = "pass" if verdict["passed"] else "not passing"
    lines = [
        f"VERIFY_APPKIT_APP: {verdict['verdict'].upper()} ({state})",
        f"fingerprint: {verdict['failure_fingerprint']}",
        f"summary: {verdict['summary']}",
        "checks:",
    ]
    for c in verdict["checks"]:
        mark = "PASS" if c["passed"] else "FAIL"
        lines.append(f"  [{mark}] {c['name']} — {c['evidence']}")
    if verdict["next_action"]:
        lines.append(f"next_action: {verdict['next_action']}")
    return "\n".join(lines)


# ---- the tool ------------------------------------------------------------------


class VerifyAppKitAppArgs(BaseModel):
    url: str = Field(
        default="",
        description=(
            "Preview base URL of the running app (e.g. http://127.0.0.1:8000/). "
            "Leave empty to auto-detect the running preview server."
        ),
    )


class VerifyAppKitAppTool:
    """[CONTRACT boundary] Strict AppKit app verifier: design-lint + schema/worker/
    form contract + a local D1 lead/admin round-trip + route & section coverage.
    Returns a W-45-compatible PASS/FAIL verdict the finish gate consumes directly."""

    definition = ToolDef(
        name="verify_appkit_app",
        description=(
            "STRUCTURALLY verify the generated AppKit lead-gen app and return a STRUCTURED "
            "pass/fail verdict (presence + ordering + parameterization + guard-first; runtime "
            "behaviour is deferred to Epic I local CF emulation). Runs nine checks: "
            "design_lint clean, schema.sql valid (sqlite round-trip + NOT NULL), Drizzle "
            "schema valid (src/db/schema.ts matches schema.sql and package deps exist), worker "
            "contract (public POST /api/leads region contains a Drizzle insert in a "
            "non-dead position; GET /api/leads + /admin Bearer-gated, guard-first; fail-closed "
            "when ADMIN_TOKEN unset), the lead form POSTs JSON to /api/leads, a LOCAL D1 "
            "model round-trip (modelled post inserts, unauth reads 401, authed read returns "
            "the row, fail-closed), Cloudflare export readiness (owner guide + secret template "
            "present, wrangler binds DB + ASSETS with worker-first /api/* + /admin routing, no "
            "real secret shipped), every page route renders 2xx with no console/network "
            "errors, and every section's data-appkit-section marker is in the DOM. Embeds a "
            "verify_web_app verdict for the render part. Call ONCE when the app is built and "
            "the preview is running to decide if the structure is sound. When the verdict "
            "PASSES all checks, the build is DONE — call finish immediately; do not keep "
            "editing (further edits invalidate the verified state)."
        ),
        args_model=VerifyAppKitAppArgs,
        needs=frozenset(
            {Capability.NETWORK, Capability.DISPLAY, Capability.SHELL, Capability.FILESYSTEM}
        ),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=False,  # runs platform-owned npm build/preview work when Vite needs it
    )

    async def run(self, args: VerifyAppKitAppArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            app = await self._load_app(ctx)
            # Dispatch the PRIMITIVE-SPECIFIC checks on the resolved primitive (a
            # missing/unreadable app is treated as lead-gen so its checks fail cleanly
            # with the usual "run app_create first" evidence). design_lint + route +
            # section coverage are COMMON to every primitive.
            primitive_id = (
                resolve_primitive(app.app_kind).id if app is not None else None
            )

            checks: list[dict[str, Any]] = []

            # 1. design_lint_clean — COMMON; reuse the DesignLintTool scan verbatim.
            checks.append(await self._check_design_lint(ctx))

            # 2..N. primitive-specific contract checks.
            if primitive_id == DIRECTORY_PRIMITIVE_ID:
                checks.extend(await self._directory_checks(ctx, app))
            else:
                checks.extend(await self._lead_gen_checks(ctx, app))

            # last. route + section coverage + the embedded verify_web_app verdict — COMMON.
            # Vite SPAs need a platform-owned compiled preview: the model has no shell in
            # strict AppKit mode, but the browser checks must hit built JS, not /src/*.tsx.
            prepared = await self._prepare_vite_preview_for_browser_checks(ctx, args.url)
            try:
                if prepared.failure_evidence is not None:
                    embedded = {
                        "verdict": "fail",
                        "passed": False,
                        "url": prepared.url,
                        "summary": prepared.failure_evidence,
                        "failure_fingerprint": hashlib.sha256(
                            prepared.failure_evidence.encode("utf-8")
                        ).hexdigest()[:16],
                    }
                    route_check = _check(
                        "route_coverage", False, prepared.failure_evidence
                    )
                    section_check = _check(
                        "section_coverage", False, prepared.failure_evidence
                    )
                else:
                    embedded, route_check, section_check = await self._browser_checks(
                        ctx, app, prepared.url
                    )
            finally:
                if prepared.stop_name is not None:
                    await self._stop_prepared_preview(ctx, prepared.stop_name)
            checks.append(route_check)
            checks.append(section_check)

            verdict = build_verdict(checks, embedded)
            return ToolOutcome(success=True, content=_render(verdict), structured=verdict)
        except Exception as e:  # noqa: BLE001 — never crash the loop; report a verdict-shaped error
            return ToolOutcome(
                success=False, content="", error=f"verify_appkit_app error: {e}"
            )

    async def _lead_gen_checks(
        self, ctx: ToolContext, app: AppSpec | None
    ) -> list[dict[str, Any]]:
        """The LEAD-GEN contract checks (Epic E/G/I): schema_sql_valid,
        drizzle_schema_valid, worker_contract, lead_form_posts, local_api_roundtrip,
        cloudflare_export_ready — in that order."""
        lead = resolve_lead_entity(app) if app is not None else None
        checks: list[dict[str, Any]] = []

        schema_sql = await self._read_text(ctx, _SCHEMA_RELPATH)
        drizzle_schema = await self._read_text(ctx, _DRIZZLE_SCHEMA_RELPATH)
        package_json = await self._read_text(ctx, _PACKAGE_RELPATH)
        worker_ts = await self._read_text(ctx, _WORKER_RELPATH)
        auth_model: WorkerAuthModel | None = None

        if lead is None or schema_sql is None:
            checks.append(
                _check(
                    "schema_sql_valid",
                    False,
                    "missing .disco/appspec.json or schema.sql — run app_create first.",
                )
            )
        else:
            res = check_schema_sql(schema_sql, lead)
            checks.append(_check(res.name, res.passed, res.evidence))

        drizzle_res = check_drizzle_schema(
            {
                "schema.sql": schema_sql,
                "src/db/schema.ts": drizzle_schema,
                "package.json": package_json,
            }
        )
        checks.append(_check(drizzle_res.name, drizzle_res.passed, drizzle_res.evidence))

        if worker_ts is None:
            checks.append(
                _check("worker_contract", False, "no worker/index.ts in the workspace.")
            )
        elif lead is not None:
            post_ok, inspected_auth, reasons = inspect_worker(worker_ts, lead)
            auth_model = inspected_auth
            ok = (
                post_ok
                and inspected_auth.reads_require_auth
                and inspected_auth.fail_closed_without_token
                and inspected_auth.admin_token_safe
                and inspected_auth.all_lead_reads_guarded
            )
            evidence = (
                "STRUCTURE verified (presence + ordering + parameterization + "
                "guard-first): public POST /api/leads region contains a Drizzle "
                "insert in a non-dead position; GET /api/leads + /admin "
                "early-return 401 as the first guard statement before any read; "
                "Bearer-checked + fail-closed on missing ADMIN_TOKEN. Runtime "
                "reachability/behaviour deferred to Epic I local CF emulation."
                if ok
                else "; ".join(reasons)
            )
            checks.append(_check("worker_contract", ok, evidence))

        if lead is not None:
            checks.append(await self._check_lead_form(ctx, lead))

        if lead is not None and schema_sql is not None and auth_model is not None:
            res = local_api_roundtrip(schema_sql, lead, auth_model)
            checks.append(_check(res.name, res.passed, res.evidence))
        else:
            checks.append(
                _check(
                    "local_api_roundtrip",
                    False,
                    "cannot model the lead flow without a valid schema.sql + worker contract.",
                )
            )

        cf_files: dict[str, str | None] = {
            rel: await self._read_text(ctx, rel) for rel in CF_EXPORT_FILES
        }
        cf_files[".dev.vars"] = await self._read_text(ctx, ".dev.vars")
        cf_res = cloudflare_export_ready(cf_files)
        checks.append(_check(cf_res.name, cf_res.passed, cf_res.evidence))
        return checks

    async def _directory_checks(
        self, ctx: ToolContext, app: AppSpec | None
    ) -> list[dict[str, Any]]:
        """The DIRECTORY contract checks (Epic N): directory_listing (a non-home route
        + a searchable `list` section), static_worker_contract (asset passthrough, no
        lead API / D1), cloudflare_export_ready (static export shape)."""
        checks: list[dict[str, Any]] = []

        checks.append(await self._check_directory_listing(ctx, app))

        worker_ts = await self._read_text(ctx, _WORKER_RELPATH)
        if worker_ts is None:
            checks.append(
                _check("static_worker_contract", False, "no worker/index.ts in the workspace.")
            )
        else:
            ok, reasons = inspect_static_worker(worker_ts)
            evidence = (
                "STRUCTURE verified: the Worker is a static asset passthrough "
                "(env.ASSETS.fetch) with no lead API, no D1 insert, and no admin "
                "read-back — the directory site has no server data plane."
                if ok
                else "; ".join(reasons)
            )
            checks.append(_check("static_worker_contract", ok, evidence))

        cf_files: dict[str, str | None] = {
            rel: await self._read_text(ctx, rel) for rel in STATIC_CF_EXPORT_FILES
        }
        cf_files[".dev.vars"] = await self._read_text(ctx, ".dev.vars")
        cf_res = cloudflare_export_ready_static(cf_files)
        checks.append(_check(cf_res.name, cf_res.passed, cf_res.evidence))
        return checks

    async def _check_directory_listing(
        self, ctx: ToolContext, app: AppSpec | None
    ) -> dict[str, Any]:
        """directory_listing — the AppSpec declares a non-home route (the directory
        page) AND a `list` section, AND a generated component renders that listing with
        a controlled search input that filters the entries."""
        if app is None:
            return _check(
                "directory_listing",
                False,
                "no .disco/appspec.json — run app_create first.",
            )
        has_dir_route = any(p.route != "/" for p in app.pages)
        has_list_section = any(s.kind == "list" for p in app.pages for s in p.sections)
        if not has_dir_route:
            return _check(
                "directory_listing",
                False,
                "the directory site declares no page route other than '/' (no directory "
                "page to browse).",
            )
        if not has_list_section:
            return _check(
                "directory_listing",
                False,
                "the directory site declares no `list` section to render the listings.",
            )
        assert ctx.sandbox is not None
        try:
            names = await ctx.sandbox.list_dir(_COMPONENTS_DIR)
        except Exception:  # noqa: BLE001 — no components dir
            names = []
        listing_reasons: list[str] = []
        for name in sorted(names):
            if not name.endswith(".tsx"):
                continue
            src = await self._read_text(ctx, f"{_COMPONENTS_DIR}/{name}")
            if src is None:
                continue
            ok, reasons = inspect_directory_listing(src)
            if ok:
                return _check(
                    "directory_listing",
                    True,
                    "the directory listing renders entries behind a controlled search "
                    "input that filters them client-side.",
                )
            # remember the closest candidate's reasons (an input-bearing component)
            if "<input" in src and reasons:
                listing_reasons = reasons
        return _check(
            "directory_listing",
            False,
            "; ".join(listing_reasons)
            or "no listing component renders a searchable/filterable list of entries.",
        )

    # ---- helpers -------------------------------------------------------------

    async def _load_app(self, ctx: ToolContext) -> AppSpec | None:
        data = await self._read_bytes(ctx, APPSPEC_RELPATH)
        if data is None:
            return None
        try:
            return load_app_spec_from_bytes(data)
        except Exception:  # noqa: BLE001 — invalid spec → treated as absent (checks fail cleanly)
            return None

    async def _read_bytes(self, ctx: ToolContext, relpath: str) -> bytes | None:
        assert ctx.sandbox is not None
        try:
            if not await ctx.sandbox.file_exists(relpath):
                return None
            return await ctx.sandbox.read_file(relpath)
        except Exception:  # noqa: BLE001 — unreadable → absent
            return None

    async def _read_text(self, ctx: ToolContext, relpath: str) -> str | None:
        data = await self._read_bytes(ctx, relpath)
        return data.decode("utf-8", errors="replace") if data is not None else None

    async def _check_design_lint(self, ctx: ToolContext) -> dict[str, Any]:
        out = await DesignLintTool().run(DesignLintArgs(root="."), ctx)
        st = out.structured or {}
        if not out.success or not st:
            return _check("design_lint_clean", False, "design_lint could not scan the workspace.")
        if st.get("ok"):
            return _check(
                "design_lint_clean",
                True,
                f"no design slop across {st.get('scanned_files', 0)} scanned file(s).",
            )
        return _check("design_lint_clean", False, st.get("summary", "design slop found."))

    async def _check_lead_form(self, ctx: ToolContext, lead: Entity) -> dict[str, Any]:
        assert ctx.sandbox is not None
        try:
            names = await ctx.sandbox.list_dir(_COMPONENTS_DIR)
        except Exception:  # noqa: BLE001 — no components dir
            names = []
        form_srcs: list[str] = []
        for name in sorted(names):
            if not name.endswith(".tsx"):
                continue
            src = await self._read_text(ctx, f"{_COMPONENTS_DIR}/{name}")
            # Select form components by a REAL fetch("/api/leads") CALL (not inert
            # string/comment content), consistent with inspect_lead_form's proof.
            if src and _fetch_leads_in_call_position(_strip_ts_comments(src)):
                form_srcs.append(src)
        if not form_srcs:
            return _check(
                "lead_form_posts",
                False,
                'no form component POSTs to /api/leads — the lead form is missing or broken.',
            )
        for src in form_srcs:
            ok, reasons = inspect_lead_form(src, lead)
            if not ok:
                return _check("lead_form_posts", False, "; ".join(reasons))
        return _check(
            "lead_form_posts",
            True,
            f"the lead form POSTs JSON to /api/leads with an input per lead field "
            f"({', '.join(f.name for f in lead.fields)}).",
        )

    async def _prepare_vite_preview_for_browser_checks(
        self, ctx: ToolContext, requested_url: str
    ) -> _PreparedPreview:
        """If this is a Vite SPA and the current preview is source-served, build it
        and serve the compiled app through the platform PreviewManager.

        Only a preview whose index clearly references a built ``/assets/*.js`` bundle
        is accepted as already prepared. A missing, failing, source-served, or opaque
        probe is not proof of a built app, so the platform builds and starts a compiled
        preview before the browser checks.
        """
        assert ctx.sandbox is not None
        package_json = await self._read_text(ctx, _PACKAGE_RELPATH)
        if not _is_vite_app_tree(
            package_json, await ctx.sandbox.file_exists(_VITE_CONFIG_RELPATH)
        ):
            return _PreparedPreview(url=(requested_url or "").strip())

        vtool = VerifyWebAppTool()
        base = (requested_url or "").strip() or await vtool._detect_preview_url(ctx)
        base = base.rstrip("/") if base else ""
        should_build = not base
        if base:
            probe = await self._fetch_preview_index(ctx, base)
            if probe is None or probe.error:
                should_build = True
            else:
                built = _served_preview_uses_built_bundle(probe.body)
                if built is True:
                    return _PreparedPreview(url=base)
                should_build = True

        if not should_build:
            return _PreparedPreview(url=base)

        build = await self._ensure_vite_platform_build(ctx, package_json or "")
        if not build.ok:
            return _PreparedPreview(url=base, failure_evidence=build.evidence)
        return await self._start_built_vite_preview(ctx)

    async def _fetch_preview_index(
        self, ctx: ToolContext, base_url: str
    ) -> _PreviewProbe | None:
        """Fetch the current preview's root from inside the sandbox.

        Returns ``None`` only when the sandbox fake/output is not the JSON frame this
        helper emits; real network errors are framed as ``error`` so they remain loud
        enough to trigger a platform build.
        """
        assert ctx.sandbox is not None
        url = base_url.rstrip("/") + "/"
        script = (
            "import json, urllib.request as U\n"
            "try:\n"
            f"    r=U.urlopen({url!r},timeout=10)\n"
            "    body=r.read(200000).decode('utf-8','replace')\n"
            "    print(json.dumps({'status': getattr(r, 'status', None) or r.getcode(), "
            "'content_type': r.headers.get('content-type',''), 'body': body}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'status': 0, 'content_type': '', 'body': '', "
            "'error': str(e)}))\n"
        )
        try:
            res = await ctx.sandbox.exec_shell(
                f"python3 -c {shlex.quote(script)}", timeout_s=15
            )
        except Exception:  # noqa: BLE001 — let browser checks handle opaque fakes
            return None
        try:
            data = json.loads(str(getattr(res, "stdout", "") or ""))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return _PreviewProbe(
            status=int(data.get("status") or 0),
            content_type=str(data.get("content_type") or ""),
            body=str(data.get("body") or ""),
            error=str(data.get("error") or ""),
        )

    async def _ensure_vite_platform_build(
        self, ctx: ToolContext, package_json: str
    ) -> _BuildResult:
        """Run the bounded platform-owned Vite build inside the sandbox.

        ``npm ci`` is skipped only when a node_modules directory exists and the cache
        marker contains the exact current package.json SHA. ``npm run build`` always
        runs because the source tree may have changed while dependencies did not.
        """
        assert ctx.sandbox is not None
        package_sha = hashlib.sha256(package_json.encode("utf-8")).hexdigest()
        node_modules_exists = await self._sandbox_dir_exists(ctx, "node_modules")
        marker = (await self._read_text(ctx, _VITE_PACKAGE_SHA_RELPATH) or "").strip()
        need_ci = not (node_modules_exists and marker == package_sha)
        started = time.monotonic()

        if need_ci:
            # `npm ci` REQUIRES package-lock.json; the generator emits package.json
            # only (deterministic tree — a lockfile would pin the generate step to a
            # registry snapshot). Live-caught 2026-07-03: ci without a lock is EUSAGE.
            has_lock = await self._sandbox_file_exists(ctx, "package-lock.json")
            install_cmd = (
                "npm ci --no-audit --no-fund"
                if has_lock
                else "npm install --no-audit --no-fund"
            )
            try:
                ci = await ctx.sandbox.exec_shell(
                    install_cmd, timeout_s=_VITE_BUILD_TIMEOUT_S
                )
            except Exception as exc:  # noqa: BLE001 — loud verdict evidence
                return _BuildResult(
                    False, f"platform Vite build could not run `{install_cmd}`: {exc}"
                )
            if getattr(ci, "exit_code", 1) != 0 or bool(getattr(ci, "timed_out", False)):
                return _BuildResult(
                    False, _exec_failure_evidence(install_cmd, ci)
                )
            try:
                await ctx.sandbox.write_file(
                    _VITE_PACKAGE_SHA_RELPATH, (package_sha + "\n").encode("utf-8")
                )
            except Exception:  # noqa: BLE001 — cache marker failure must not hide build evidence
                pass

        elapsed = int(time.monotonic() - started)
        remaining = max(1, _VITE_BUILD_TIMEOUT_S - elapsed)
        try:
            build = await ctx.sandbox.exec_shell("npm run build", timeout_s=remaining)
        except Exception as exc:  # noqa: BLE001 — loud verdict evidence
            return _BuildResult(False, f"platform Vite build could not run `npm run build`: {exc}")
        if getattr(build, "exit_code", 1) != 0 or bool(getattr(build, "timed_out", False)):
            return _BuildResult(False, _exec_failure_evidence("npm run build", build))
        return _BuildResult(True)

    async def _start_built_vite_preview(self, ctx: ToolContext) -> _PreparedPreview:
        """Serve the compiled Vite app with Vite's preview server under platform port
        ownership, then return the in-sandbox URL the browser verifier can reach."""
        try:
            from .preview import _manager

            mgr = _manager(ctx)
            session = await mgr.start(
                command=_VITE_PREVIEW_COMMAND,
                name=_BUILT_PREVIEW_NAME,
                supervise=False,
            )
        except Exception as exc:  # noqa: BLE001 — route/section fail loudly
            return _PreparedPreview(
                url="",
                failure_evidence=(
                    "platform Vite build succeeded, but serving the built app failed "
                    f"while starting `{_VITE_PREVIEW_COMMAND}`: {exc}"
                ),
            )

        status = getattr(getattr(session, "status", ""), "value", str(getattr(session, "status", "")))
        port = getattr(session, "port", None)
        if status not in {"running", "unavailable"} or not isinstance(port, int):
            detail = str(getattr(session, "detail", "") or "preview did not become healthy")
            return _PreparedPreview(
                url="",
                failure_evidence=(
                    "platform Vite build succeeded, but the built-app preview failed "
                    f"to become healthy (status={status or 'unknown'}): {detail}"
                ),
            )
        return _PreparedPreview(
            url=f"http://127.0.0.1:{port}/",
            stop_name=_BUILT_PREVIEW_NAME,
        )

    async def _stop_prepared_preview(self, ctx: ToolContext, name: str) -> None:
        try:
            from .preview import _manager

            await _manager(ctx).stop(name)
        except Exception:  # noqa: BLE001 — cleanup must not mask the verifier result
            pass

    async def _sandbox_file_exists(self, ctx: ToolContext, relpath: str) -> bool:
        assert ctx.sandbox is not None
        try:
            return bool(await ctx.sandbox.file_exists(relpath))
        except Exception:  # noqa: BLE001 — absence probe must never raise
            return False

    async def _sandbox_dir_exists(self, ctx: ToolContext, relpath: str) -> bool:
        assert ctx.sandbox is not None
        try:
            await ctx.sandbox.list_dir(relpath)
            return True
        except Exception:  # noqa: BLE001 — absent or not a directory
            return False

    async def _browser_checks(
        self, ctx: ToolContext, app: AppSpec | None, url: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
        """Drive the W-45 browser path for route_coverage + section_coverage and
        return (embedded verify_web_app verdict, route_check, section_check)."""
        vtool = VerifyWebAppTool()
        base = (url or "").strip() or await vtool._detect_preview_url(ctx)
        base = base.rstrip("/") or base

        routes = ["/"]
        if app is not None:
            seen: set[str] = set()
            routes = []
            for page in app.pages:
                if page.route not in seen:
                    seen.add(page.route)
                    routes.append(page.route)
            routes = routes or ["/"]

        embedded: dict[str, Any] | None = None
        route_failures: list[str] = []
        for route in routes:
            route_url = base if route == "/" else base + "/" + route.lstrip("/")
            out = await vtool.run(VerifyWebAppArgs(url=route_url), ctx)
            rv = out.structured or {}
            if embedded is None:
                embedded = rv  # the first (base) route is the structural verdict
            if not rv.get("passed"):
                route_failures.append(f"{route}: {rv.get('summary', 'did not pass')}")

        if route_failures:
            route_check = _check(
                "route_coverage",
                False,
                "; ".join(route_failures[:3]),
            )
        else:
            route_check = _check(
                "route_coverage",
                True,
                f"all {len(routes)} route(s) rendered 2xx with no console/network errors.",
            )

        # section_coverage — every section's data-appkit-section marker in the DOM.
        section_check = await self._check_sections(ctx, app, base, routes)
        return embedded, route_check, section_check

    async def _check_sections(
        self, ctx: ToolContext, app: AppSpec | None, base: str, routes: list[str]
    ) -> dict[str, Any]:
        if app is None:
            return _check("section_coverage", False, "no AppSpec to enumerate sections from.")
        expected = {s.id for page in app.pages for s in page.sections}
        if not expected:
            return _check("section_coverage", True, "no sections declared.")
        found: set[str] = set()
        for route in routes:
            route_url = base if route == "/" else base + "/" + route.lstrip("/")
            out = await BrowserTool().run(
                BrowserArgs(action="navigate", url=route_url), ctx
            )
            if out.success and out.structured:
                for marker in out.structured.get("appkit_sections", []) or []:
                    if marker:
                        found.add(str(marker))
        missing = sorted(expected - found)
        if missing:
            return _check(
                "section_coverage",
                False,
                "section(s) declared in the AppSpec did NOT render (no data-appkit-section "
                f"marker in the DOM): {', '.join(missing)}.",
            )
        return _check(
            "section_coverage",
            True,
            f"all {len(expected)} AppSpec section(s) rendered "
            "(data-appkit-section markers present).",
        )


__all__ = [
    "VerifyAppKitAppArgs",
    "VerifyAppKitAppTool",
    "WorkerAuthVerdict",
    "build_verdict",
    "inspect_directory_listing",
    "inspect_lead_form",
    "inspect_static_worker",
    "inspect_worker",
]
