"""AppKit WO-A3 — the PURE Worker/TS STATIC-inspection kit.

The static text/TypeScript inspectors the strict app verifier
(`verify_appkit_app`, tools layer) runs against the GENERATED source: comment
stripping, brace/paren matching, dead-branch + early-exit analysis, the worker
contract inspection (`inspect_worker` → :class:`WorkerAuthVerdict`), the lead-form
/ submit-support inspections, and the directory-primitive inspectors. Moved here
VERBATIM from `disco.tools.builtin.verify_appkit_app` (WO-A3) so the per-primitive
`PrimitiveDefinition.verify` hooks (`primitive_verify`) can run them from core —
the function bodies are byte-identical to the tool-layer originals.

PURITY / LAYERING: stdlib (`re`) + the sibling `.spec` / `.local_verify` models
only — no sandbox, no browser, no IO. `disco.core` is the leaf package
(.importlinter); everything ToolContext/sandbox/browser-dependent stays in the
tools layer.

SCOPE (honest, unchanged): these verify the generated worker/form by CONTROL
FLOW, not substring presence — STRUCTURAL verification of the TypeScript *source*
(presence + ordering + parameterization + guard-first), NOT runtime execution.
The local runtime proof is packages/core/tests/test_workerd_persistence.py.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .local_verify import WorkerAuthModel
from .spec import Entity


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
# (the local runtime proof lives in packages/core/tests/test_workerd_persistence.py).
# A PASS here means "the handler's STRUCTURE enforces the lead/admin contract" — it
# does NOT prove the insert is reached or the guard runs at runtime. The flags it
# extracts then drive `local_api_roundtrip`, which runs a MODEL of that behaviour
# against a real in-memory sqlite DB — a structural-consistency check, NOT a runtime
# execution of the worker itself. Hosted Cloudflare deploy remains owner-gated.


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
    found here; local runtime reachability is proved separately by
    `packages/core/tests/test_workerd_persistence.py`."""
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
# proof. The local runtime proof is packages/core/tests/test_workerd_persistence.py.


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
    parameterization + guard-first), not substring presence. Runtime behaviour is
    proved separately by packages/core/tests/test_workerd_persistence.py:

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
    execution; local runtime reachability/behaviour is covered by
    packages/core/tests/test_workerd_persistence.py).
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


def _call_with_string_arg_in_position(code: str, callee: str, path: str) -> bool:
    """True iff `callee("path"...)` appears as an actual CALL in executable code.

    The identifier sits in code context (NOT inside a string/template literal), is a
    bare/`await`ed call (not a `.member` access), is immediately applied to `(`, and
    its first argument is the expected string. Inert mentions in strings/templates do
    not count.
    """
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if ch in ("'", '"', "`"):
            _lit, end = _read_string_literal(code, i)
            if end > i:
                i = end  # skip the whole string/template literal — its text is not code
                continue
        if code.startswith(callee, i) and (
            i == 0 or not (code[i - 1].isalnum() or code[i - 1] in "_$.")
        ):
            j = i + len(callee)
            while j < n and code[j] in " \t\r\n":
                j += 1
            if j < n and code[j] == "(":
                j += 1
                while j < n and code[j] in " \t\r\n":
                    j += 1
                inner, _end = _read_string_literal(code, j)
                if inner is not None and inner.split("?", 1)[0] == path:
                    return True
        i += 1
    return False


def _fetch_leads_in_call_position(code: str) -> bool:
    return _call_with_string_arg_in_position(code, "fetch", "/api/leads")


def _use_submit_leads_in_call_position(code: str) -> bool:
    return _call_with_string_arg_in_position(code, "useSubmit", "/api/leads")


def inspect_submit_support(client_src: str, hook_src: str) -> tuple[bool, list[str]]:
    """Inspect the generated API client + submit hook that back the reactive form."""
    reasons: list[str] = []
    client = _strip_ts_comments(client_src)
    hook = _strip_ts_comments(hook_src)
    if "export type ApiResult<T>" not in client:
        reasons.append("src/api/client.ts does not export the typed ApiResult<T> union")
    if re.search(r"\bany\b", client):
        reasons.append("src/api/client.ts uses `any`; the API boundary must be typed")
    if not re.search(r"\bfetch\s*\(\s*path\s*,", client):
        reasons.append("src/api/client.ts is not the fetch(path, ...) chokepoint")
    if not re.search(r'method:\s*"POST"', client):
        reasons.append("src/api/client.ts does not POST")
    if not re.search(r'"Content-Type":\s*"application/json"', client):
        reasons.append("src/api/client.ts does not set JSON Content-Type")
    if not re.search(r"body:\s*JSON\.stringify\(\s*body\s*\)", client):
        reasons.append("src/api/client.ts does not serialize the submitted body")
    if "errorFromBody(body)" not in client:
        reasons.append("src/api/client.ts does not surface the API {error} response body")

    if "export type SubmitState" not in hook:
        reasons.append("src/hooks/useSubmit.ts does not export the SubmitState union")
    for state in ("idle", "submitting", "success", "error"):
        if f'kind: "{state}"' not in hook:
            reasons.append(f"src/hooks/useSubmit.ts is missing the {state!r} state")
    if "useRef(false)" not in hook:
        reasons.append("src/hooks/useSubmit.ts lacks in-flight double-submit protection")
    if "setSubmitted((current) => [entry, ...current])" not in hook:
        reasons.append("src/hooks/useSubmit.ts does not optimistically add submissions")
    if "current.filter((item) => item.id !== entry.id)" not in hook:
        reasons.append("src/hooks/useSubmit.ts does not remove failed optimistic entries")
    if not re.search(r"postJson\s*<[^>]+>\s*\(\s*path\s*,\s*values\s*\)", hook):
        reasons.append("src/hooks/useSubmit.ts does not submit through postJson(path, values)")
    return (not reasons), reasons


def inspect_lead_form(form_src: str, lead: Entity) -> tuple[bool, list[str]]:
    """Control-flow-inspect a generated form component for the lead-POST contract:
    a REAL (non-comment, non-string) `useSubmit("/api/leads")` CALL, inline required
    validation, disabled submitting button, optimistic list/live region, and EVERY
    resolved lead field bound to form state (a named input wired by `value={form[...]}` +
    `updateField(field, value)`). Inert string/comment mentions do not count. Returns
    (ok, reasons)."""
    reasons: list[str] = []
    code = _strip_ts_comments(form_src)
    if not _use_submit_leads_in_call_position(code):
        reasons.append(
            'the form does not call useSubmit("/api/leads") in live code '
            "(only in a comment or an inert string?)"
        )
    if "function validateRequired(): boolean" not in code:
        reasons.append("the form does not run inline required-field validation")
    if 'disabled={state.kind === "submitting"}' not in code:
        reasons.append("the submit button is not disabled while submitting")
    if 'aria-live="polite"' not in code:
        reasons.append("the form feedback/list region is not aria-live")
    if "Recently submitted" not in code or "submitted.map((entry)" not in code:
        reasons.append("the form does not render the optimistic recently-submitted list")
    for field in lead.fields:
        name = re.escape(field.name)
        has_input = re.search(r'name="' + name + r'"', code) is not None
        value_bound = re.search(r'value=\{\s*form\[\s*"' + name + r'"\s*\]', code) is not None
        change_bound = (
            re.search(r'updateField\(\s*"' + name + r'"\s*,', code) is not None
        )
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


__all__ = [
    "WorkerAuthVerdict",
    "inspect_directory_listing",
    "inspect_lead_form",
    "inspect_static_worker",
    "inspect_submit_support",
    "inspect_worker",
]
